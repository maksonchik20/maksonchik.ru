from __future__ import annotations

import base64
import binascii
import hashlib
import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from env import OWNER_CHAT_ID

from .models import TelegramOutbox
from .metrics import OUTBOX_EVENTS, observe_metric
from .telegram import dispatch_document_bytes, dispatch_telegram_request, tg_send_message

logger = logging.getLogger(__name__)

MAX_BACKOFF_SECONDS = 3600
INITIAL_BACKOFF_SECONDS = 5
DELIVERY_LEASE_SECONDS = 120
OWNER_ALERT_AFTER_ATTEMPTS = 6

PERMANENT_SEND_ERRORS = (
    "bot can't initiate conversation with a user",
    "bot was blocked by the user",
    "user is deactivated",
    "chat not found",
    "invalid outbox payload",
)


def _is_permanent_send_error(error: str) -> bool:
    lower = (error or "").lower()
    return any(phrase in lower for phrase in PERMANENT_SEND_ERRORS)


def edit_notification_idempotency_key(msg) -> str | None:
    fr = msg.get("from") or {}
    editor_id = fr.get("id")
    edit_date = msg.get("edit_date")
    if editor_id is None or edit_date is None:
        return None

    text_hash = hashlib.sha256((msg.get("text") or "").encode()).hexdigest()[:16]
    return f"edit:{editor_id}:{edit_date}:{text_hash}"


def _next_attempt_at(attempts: int):
    delay = min(
        INITIAL_BACKOFF_SECONDS * (2 ** max(attempts - 1, 0)),
        MAX_BACKOFF_SECONDS,
    )
    return timezone.now() + timedelta(seconds=delay)


def _notify_owner_outbox_failed(item: TelegramOutbox, error: str) -> None:
    text = (
        "⚠️ <b>Outbox:</b> не удалось отправить сообщение после "
        f"{OWNER_ALERT_AFTER_ATTEMPTS} попыток\n\n"
        f"<b>id:</b> {item.pk}\n"
        f"<b>method:</b> {item.method}\n"
        f"<b>chat_id:</b> {item.chat_id}\n"
        f"<b>idempotency_key:</b> {item.idempotency_key or '—'}\n"
        f"<b>error:</b> {(error or 'unknown error')[:500]}"
    )
    tg_send_message(OWNER_CHAT_ID, text)


def enqueue_outbox(
    *,
    chat_id,
    method: str,
    payload: dict,
    idempotency_key: str | None = None,
) -> TelegramOutbox | None:
    if not chat_id:
        return None

    defaults = {
        "chat_id": chat_id,
        "method": method,
        "payload": payload,
        "next_attempt_at": timezone.now(),
    }

    try:
        with transaction.atomic():
            if idempotency_key:
                item, _ = TelegramOutbox.objects.get_or_create(
                    idempotency_key=idempotency_key,
                    defaults=defaults,
                )
                return item
            return TelegramOutbox.objects.create(idempotency_key=None, **defaults)
    except IntegrityError:
        logger.debug("Outbox idempotency skip: %s", idempotency_key)
        if idempotency_key:
            return TelegramOutbox.objects.filter(idempotency_key=idempotency_key).first()
        return None


def _dispatch_outbox_item(item: TelegramOutbox) -> tuple[bool, str]:
    if item.method != TelegramOutbox.Method.SEND_DOCUMENT_BYTES:
        return dispatch_telegram_request(item.method, item.chat_id, item.payload)

    try:
        document = base64.b64decode(
            str(item.payload.get("document_b64") or ""),
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        return False, f"invalid outbox payload: {exc}"

    return dispatch_document_bytes(
        item.chat_id,
        document,
        filename=str(item.payload.get("filename") or "messages.txt"),
        caption=str(item.payload.get("caption") or ""),
        content_type=str(
            item.payload.get("content_type") or "text/plain; charset=utf-8"
        ),
    )


def deliver_outbox_item(pk: int, *, now=None) -> str:
    """Атомарно забирает одну запись и выполняет fast-path либо retry."""
    now = now or timezone.now()
    lease_until = now + timedelta(seconds=DELIVERY_LEASE_SECONDS)
    claimed = TelegramOutbox.objects.filter(
        pk=pk,
        status=TelegramOutbox.Status.PENDING,
        next_attempt_at__lte=now,
    ).update(next_attempt_at=lease_until)
    if not claimed:
        return "skipped"

    item = TelegramOutbox.objects.filter(pk=pk).first()
    if item is None:
        return "skipped"

    ok, error = _dispatch_outbox_item(item)
    if ok:
        TelegramOutbox.objects.filter(
            pk=pk,
            status=TelegramOutbox.Status.PENDING,
        ).update(
            status=TelegramOutbox.Status.SENT,
            sent_at=timezone.now(),
            last_error="",
        )
        observe_metric(OUTBOX_EVENTS, 1, {"status": "sent", "method": item.method})
        logger.info("Outbox sent id=%s method=%s chat_id=%s", pk, item.method, item.chat_id)
        return "sent"

    if _is_permanent_send_error(error):
        TelegramOutbox.objects.filter(
            pk=pk,
            status=TelegramOutbox.Status.PENDING,
        ).update(
            status=TelegramOutbox.Status.DROPPED,
            last_error=(error or "unknown error")[:1000],
        )
        logger.warning(
            "Outbox dropped id=%s chat_id=%s permanent error=%s",
            pk,
            item.chat_id,
            error,
        )
        observe_metric(OUTBOX_EVENTS, 1, {"status": "dropped", "method": item.method})
        return "dropped"

    new_attempts = item.attempts + 1
    TelegramOutbox.objects.filter(
        pk=pk,
        status=TelegramOutbox.Status.PENDING,
    ).update(
        attempts=F("attempts") + 1,
        last_error=(error or "unknown error")[:1000],
        next_attempt_at=_next_attempt_at(new_attempts),
    )
    observe_metric(OUTBOX_EVENTS, 1, {"status": "retry", "method": item.method})
    if new_attempts == OWNER_ALERT_AFTER_ATTEMPTS:
        _notify_owner_outbox_failed(item, error)
    logger.warning(
        "Outbox retry scheduled id=%s method=%s chat_id=%s error=%s",
        pk,
        item.method,
        item.chat_id,
        error,
    )
    return "failed"


def enqueue_and_deliver(
    *,
    chat_id,
    method: str,
    payload: dict,
    idempotency_key: str,
) -> TelegramOutbox:
    """Сначала надёжно сохраняет ответ, затем без ожидания таймера отправляет его."""
    item = enqueue_outbox(
        chat_id=chat_id,
        method=method,
        payload=payload,
        idempotency_key=idempotency_key,
    )
    if item is None:
        raise RuntimeError(f"Failed to persist outbox item: {idempotency_key}")
    deliver_outbox_item(item.pk)
    return item


def send_message_reliably(
    chat_id,
    text: str,
    *,
    idempotency_key: str,
    reply_markup: dict | None = None,
) -> TelegramOutbox:
    payload = {
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return enqueue_and_deliver(
        chat_id=chat_id,
        method=TelegramOutbox.Method.SEND_MESSAGE,
        payload=payload,
        idempotency_key=idempotency_key,
    )


def send_document_bytes_reliably(
    chat_id,
    document: bytes,
    *,
    filename: str,
    caption: str,
    idempotency_key: str,
    content_type: str = "text/plain; charset=utf-8",
) -> TelegramOutbox:
    return enqueue_and_deliver(
        chat_id=chat_id,
        method=TelegramOutbox.Method.SEND_DOCUMENT_BYTES,
        payload={
            "document_b64": base64.b64encode(document).decode("ascii"),
            "filename": filename,
            "caption": caption,
            "content_type": content_type,
        },
        idempotency_key=idempotency_key,
    )


def process_outbox(*, limit: int = 50) -> dict:
    now = timezone.now()
    stats = {"processed": 0, "sent": 0, "failed": 0, "pending": 0}

    pending_ids = list(
        TelegramOutbox.objects.filter(
            status=TelegramOutbox.Status.PENDING,
            next_attempt_at__lte=now,
        )
        .order_by("created_at")
        .values_list("pk", flat=True)[:limit]
    )
    stats["pending"] = TelegramOutbox.objects.filter(
        status=TelegramOutbox.Status.PENDING,
        next_attempt_at__lte=now,
    ).count()

    for pk in pending_ids:
        result = deliver_outbox_item(pk, now=now)
        if result == "skipped":
            continue
        stats["processed"] += 1
        if result == "sent":
            stats["sent"] += 1
        elif result == "dropped":
            stats["failed"] += 1
        elif result == "failed":
            stats["failed"] += 1

    return stats
