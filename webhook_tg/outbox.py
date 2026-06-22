import hashlib
import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from env import OWNER_CHAT_ID

from .models import TelegramOutbox
from .telegram import dispatch_telegram_request, tg_send_message

logger = logging.getLogger(__name__)

MAX_BACKOFF_SECONDS = 3600
INITIAL_BACKOFF_SECONDS = 30
OWNER_ALERT_AFTER_ATTEMPTS = 6

PERMANENT_SEND_ERRORS = (
    "bot can't initiate conversation with a user",
    "bot was blocked by the user",
    "user is deactivated",
    "chat not found",
)


def _is_permanent_send_error(error: str) -> bool:
    lower = (error or "").lower()
    return any(phrase in lower for phrase in PERMANENT_SEND_ERRORS)


def edit_notification_dedup_key(msg) -> str | None:
    fr = msg.get("from") or {}
    editor_id = fr.get("id")
    edit_date = msg.get("edit_date")
    if editor_id is None or edit_date is None:
        return None

    text_hash = hashlib.sha256((msg.get("text") or "").encode()).hexdigest()[:16]
    return f"edit:{editor_id}:{edit_date}:{text_hash}"


def _next_attempt_at(attempts: int):
    delay = min(INITIAL_BACKOFF_SECONDS * (2 ** attempts), MAX_BACKOFF_SECONDS)
    return timezone.now() + timedelta(seconds=delay)


def _notify_owner_outbox_failed(item: TelegramOutbox, error: str) -> None:
    text = (
        "⚠️ <b>Outbox:</b> не удалось отправить сообщение после "
        f"{OWNER_ALERT_AFTER_ATTEMPTS} попыток\n\n"
        f"<b>id:</b> {item.pk}\n"
        f"<b>method:</b> {item.method}\n"
        f"<b>chat_id:</b> {item.chat_id}\n"
        f"<b>dedup_key:</b> {item.dedup_key or '—'}\n"
        f"<b>error:</b> {(error or 'unknown error')[:500]}"
    )
    tg_send_message(OWNER_CHAT_ID, text)


def enqueue_outbox(
    *,
    chat_id,
    method: str,
    payload: dict,
    dedup_key: str | None = None,
) -> None:
    if not chat_id:
        return

    defaults = {
        "chat_id": chat_id,
        "method": method,
        "payload": payload,
        "next_attempt_at": timezone.now(),
    }

    try:
        with transaction.atomic():
            if dedup_key:
                TelegramOutbox.objects.get_or_create(
                    dedup_key=dedup_key,
                    defaults=defaults,
                )
                return
            TelegramOutbox.objects.create(dedup_key=None, **defaults)
    except IntegrityError:
        logger.debug("Outbox dedup skip: %s", dedup_key)


def process_outbox(*, limit: int = 50) -> dict:
    now = timezone.now()
    stats = {"processed": 0, "sent": 0, "failed": 0, "pending": 0}

    pending_ids = list(
        TelegramOutbox.objects.filter(next_attempt_at__lte=now)
        .order_by("created_at")
        .values_list("pk", flat=True)[:limit]
    )
    stats["pending"] = TelegramOutbox.objects.filter(next_attempt_at__lte=now).count()

    for pk in pending_ids:
        item = TelegramOutbox.objects.filter(pk=pk).first()
        if item is None or item.next_attempt_at > now:
            continue

        stats["processed"] += 1
        ok, error = dispatch_telegram_request(item.method, item.chat_id, item.payload)
        if ok:
            item.delete()
            stats["sent"] += 1
            logger.info("Outbox sent id=%s method=%s chat_id=%s", pk, item.method, item.chat_id)
            continue

        if _is_permanent_send_error(error):
            logger.warning(
                "Outbox dropped id=%s chat_id=%s permanent error=%s",
                pk,
                item.chat_id,
                error,
            )
            item.delete()
            stats["failed"] += 1
            continue

        new_attempts = item.attempts + 1
        TelegramOutbox.objects.filter(pk=pk).update(
            attempts=F("attempts") + 1,
            last_error=(error or "unknown error")[:1000],
            next_attempt_at=_next_attempt_at(new_attempts),
        )
        stats["failed"] += 1
        if new_attempts == OWNER_ALERT_AFTER_ATTEMPTS:
            _notify_owner_outbox_failed(item, error)
        logger.warning(
            "Outbox retry scheduled id=%s method=%s chat_id=%s error=%s",
            pk,
            item.method,
            item.chat_id,
            error,
        )

    return stats
