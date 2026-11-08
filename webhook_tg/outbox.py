from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import json
import logging
import os
import re
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from env import OWNER_CHAT_ID

from .models import TelegramOutbox
from .metrics import OUTBOX_EVENTS, observe_metric
from .telegram import dispatch_document_bytes, dispatch_document_file, dispatch_telegram_request, dispatch_media_bytes, download_telegram_file_bytes, tg_send_message
from .telegram_text import split_message_text

logger = logging.getLogger(__name__)

MAX_BACKOFF_SECONDS = 3600
INITIAL_BACKOFF_SECONDS = 5
DELIVERY_LEASE_SECONDS = 120
OWNER_ALERT_AFTER_ATTEMPTS = 6
VOICE_MESSAGES_FORBIDDEN = "VOICE_MESSAGES_FORBIDDEN"
VIDEO_NOTE_PRIVACY_NOTICE = (
    "Копию кружка не удалось отправить: в ваших настройках конфиденциальности "
    "Telegram запрещены голосовые и видеосообщения."
)

PERMANENT_SEND_ERRORS = (
    "bot can't initiate conversation with a user",
    "bot was blocked by the user",
    "user is deactivated",
    "chat not found",
    "invalid outbox payload",
    "message is too long",
    "voice_messages_forbidden",
    "20 mb download limit",
)


def _is_voice_messages_forbidden(error: str) -> bool:
    return VOICE_MESSAGES_FORBIDDEN.lower() in (error or "").lower()


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
    if msg.get("message_id") is not None and (msg.get("chat") or {}).get("id") is not None:
        identity = [msg.get("business_connection_id"), msg["chat"]["id"], msg["message_id"], editor_id, edit_date, msg.get("text") or msg.get("caption") or ""]
        return "edit:v2:" + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
    return f"edit:{editor_id}:{edit_date}:{text_hash}"


def _next_attempt_at(attempts: int, error: str = ""):
    retry_after = re.search(r"retry_after=(\d+)", error or "")
    if retry_after:
        return timezone.now() + timedelta(seconds=min(max(int(retry_after.group(1)), 1), 86400))
    delay = min(
        INITIAL_BACKOFF_SECONDS * (2 ** max(attempts - 1, 0)),
        MAX_BACKOFF_SECONDS,
    )
    if any(term in (error or "").lower() for term in ("timeout", "timed out", "connection", "circuit breaker", "upstream http")):
        delay = min(delay, 10 if "circuit breaker" in error.lower() else 30)
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
    delivery_context: dict | None = None,
) -> TelegramOutbox | None:
    if not chat_id:
        return None

    defaults = {
        "chat_id": chat_id,
        "method": method,
        "payload": payload,
        "next_attempt_at": timezone.now(),
        "delivery_context": delivery_context or {},
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
    if item.method == TelegramOutbox.Method.SEND_PROTECTED_MEDIA:
        return _dispatch_protected_media(item)
    if item.method == TelegramOutbox.Method.SEND_DOCUMENT_FILE:
        from .history_files import open_history_document, validate_history_path
        from .history_exports import restore_history_document

        try:
            path = validate_history_path(item.payload.get("document_path", ""))
            descriptor = os.open(path.with_suffix(".delivery.lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            item._history_delivery_lock = os.fdopen(descriptor, "a+")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if not TelegramOutbox.objects.filter(pk=item.pk, status=TelegramOutbox.Status.PENDING).exists():
                raise BlockingIOError("History delivery already completed")
            try:
                document = open_history_document(item.payload.get("document_path", ""))
            except FileNotFoundError:
                document = restore_history_document(item)
            with document:
                return dispatch_document_file(
                    item.chat_id, document, filename=str(item.payload.get("filename") or "history.txt"),
                    caption=str(item.payload.get("caption") or ""),
                    content_type=str(item.payload.get("content_type") or "text/plain; charset=utf-8"),
                )
        except BlockingIOError:
            raise
        except ValueError:
            return False, "invalid outbox payload: history file validation failed"
        except Exception:
            # A missing/unreadable file is recoverable, not a reason to drop a
            # user's pending export. Keep the durable task and outbox row.
            logger.exception("History file unavailable outbox_id=%s", item.pk)
            return False, "History file unavailable; export recovery will be retried"
    if item.method == TelegramOutbox.Method.SEND_MESSAGE:
        return _dispatch_text_message(item, item.payload)
    if item.method in (
        TelegramOutbox.Method.SEND_VIDEO_NOTE_WITH_TEXT,
        TelegramOutbox.Method.SEND_MEDIA_WITH_TEXT,
    ):
        return _dispatch_video_note_with_text(item)
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


def _dispatch_protected_media(item):
    """Persist downloaded bytes before upload; resume without re-downloading."""
    payload = item.payload
    kind = payload.get("media_kind")
    if kind not in ("photo", "video", "video_note") or not payload.get("file_id"):
        return False, "invalid outbox payload: unsupported protected media"
    text = str(payload.get("text") or "")
    if not payload.get("_media_sent"):
        caption = text if kind != "video_note" and len(text.encode("utf-16-le")) // 2 <= 1024 else ""
        try:
            saved_photo = None
            if kind == "photo":
                from .models import Message
                saved_photo = (Message.objects.filter(file_id=payload["file_id"], saved_photo_source_file_id=payload["file_id"])
                               .exclude(saved_photo_file_id="").values_list("saved_photo_file_id", flat=True).first())
            if saved_photo:
                ok, error = dispatch_telegram_request("sendPhoto", item.chat_id, {"photo": saved_photo, "caption": caption, "parse_mode": "HTML"})
            elif not payload.get("media_b64"):
                content, path = download_telegram_file_bytes(payload["file_id"], timeout=15)
                payload = {**payload, "media_b64": base64.b64encode(content).decode("ascii")}
                TelegramOutbox.objects.filter(pk=item.pk).update(payload=payload)
                item.payload = payload
            else:
                content = base64.b64decode(payload["media_b64"], validate=True)
            if not saved_photo and kind == "video_note" and payload.get("_as_document"):
                caption = text if len(text.encode("utf-16-le")) // 2 <= 1024 else ""
                ok, error = dispatch_document_bytes(item.chat_id, content, filename="video-note.mp4", caption=caption, content_type="video/mp4", timeout=20)
            elif not saved_photo:
                ok, error = dispatch_media_bytes(item.chat_id, content, kind=kind, caption=caption, source_file_id=payload["file_id"])
            if not ok and kind == "video_note" and _is_voice_messages_forbidden(error):
                payload = {**payload, "_as_document": True}
                TelegramOutbox.objects.filter(pk=item.pk).update(payload=payload)
                item.payload = payload
                return False, "Retry protected video note as a document"
            if not ok:
                return False, error
        except Exception as exc:
            from .telegram import _safe_telegram_error

            return False, _safe_telegram_error(exc)
        payload = {**payload, "_media_sent": True, "_caption_sent": bool(caption)}
        payload.pop("media_b64", None)
        TelegramOutbox.objects.filter(pk=item.pk).update(payload=payload)
        item.payload = payload
    if payload.get("_caption_sent") or not text:
        return True, ""
    return _dispatch_text_message(item, {"text": text, "parse_mode": "HTML", "disable_web_page_preview": True})


def _dispatch_text_message(item: TelegramOutbox, message_payload: dict) -> tuple[bool, str]:
    # Persist the exact parts and progress so a retry cannot resend the parts
    # whose successful delivery was already recorded, even after a restart.
    payload = {k: v for k, v in message_payload.items() if k != "_text_delivery"}
    text = payload.get("text")
    if not isinstance(text, str):
        return False, "invalid outbox payload: text must be a string"
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    state = item.payload.get("_text_delivery")
    if state is None:
        try:
            parts = (
                [text] if payload.get("entities")
                else split_message_text(text, payload.get("parse_mode"))
            )
        except ValueError as exc:
            return False, f"invalid outbox payload: {exc}"
        if len(parts) == 1:
            return dispatch_telegram_request("sendMessage", item.chat_id, payload)
        state = {
            "fingerprint": fingerprint,
            "parts": [f"Часть {index + 1}/{len(parts)}\n\n{part}" for index, part in enumerate(parts)],
            "next": 0,
        }
        item.payload = {**item.payload, "_text_delivery": state}
        TelegramOutbox.objects.filter(pk=item.pk).update(payload=item.payload)
    if (
        not isinstance(state, dict)
        or state.get("fingerprint") != fingerprint
        or not isinstance(state.get("parts"), list)
        or not state["parts"]
        or not all(isinstance(part, str) for part in state["parts"])
        or type(state.get("next")) is not int
        or not 0 <= state["next"] <= len(state["parts"])
    ):
        return False, "invalid outbox payload: text delivery progress does not match payload"
    for index in range(state["next"], len(state["parts"])):
        lease_until = timezone.now() + timedelta(seconds=DELIVERY_LEASE_SECONDS)
        TelegramOutbox.objects.filter(pk=item.pk).update(next_attempt_at=lease_until)
        part_payload = {**payload, "text": state["parts"][index]}
        if index < len(state["parts"]) - 1:
            part_payload.pop("reply_markup", None)
        ok, error = dispatch_telegram_request("sendMessage", item.chat_id, part_payload)
        if not ok:
            return False, error
        state = {**state, "next": index + 1}
        item.payload = {**item.payload, "_text_delivery": state}
        TelegramOutbox.objects.filter(pk=item.pk).update(payload=item.payload)
    return True, ""


def _dispatch_video_note_with_text(item: TelegramOutbox) -> tuple[bool, str]:
    """Keep the two sends ordered and resume at the text after a retry."""
    payload = item.payload
    legacy = item.method == TelegramOutbox.Method.SEND_VIDEO_NOTE_WITH_TEXT
    kind = "video_note" if legacy else payload.get("media_kind")
    file_id = payload.get("video_note") if legacy else payload.get("file_id")
    progress_key = "_video_note_sent" if legacy else "_media_sent"
    kindToMethod = {
        "video_note": "sendVideoNote", "photo": "sendPhoto", "video": "sendVideo",
        "animation": "sendAnimation", "document": "sendDocument", "audio": "sendAudio", "sticker": "sendSticker", "voice": "sendVoice",
    }
    if kind not in kindToMethod or not file_id or not payload.get("text"):
        return False, "invalid outbox payload: supported media and text are required"
    privacy_skipped = (
        kind == "video_note"
        and payload.get("_media_skipped_reason") == VOICE_MESSAGES_FORBIDDEN
    )
    if not payload.get(progress_key) and not privacy_skipped:
        # Pending rows may already have a definitive Telegram privacy refusal.
        # Resume at the text without trying the forbidden media again.
        if kind == "video_note" and _is_voice_messages_forbidden(item.last_error):
            ok, error = False, item.last_error
        else:
            ok, error = dispatch_telegram_request(
                kindToMethod[kind], item.chat_id, {kind: file_id}
            )
        if not ok:
            if kind != "video_note" or not _is_voice_messages_forbidden(error):
                return False, error
            # Record a skipped copy separately from media Telegram accepted.
            payload = {**payload, "_media_skipped_reason": VOICE_MESSAGES_FORBIDDEN}
            privacy_skipped = True
        else:
            payload = {**payload, progress_key: True}
        # Persist media progress before sending text, including on a retry.
        TelegramOutbox.objects.filter(pk=item.pk).update(payload=payload)
        item.payload = payload
    text = payload["text"]
    if privacy_skipped:
        text += "\n\n" + VIDEO_NOTE_PRIVACY_NOTICE
    return _dispatch_text_message(
        item,
        {
            "text": text,
            "parse_mode": payload.get("parse_mode", "HTML"),
            "disable_web_page_preview": True,
        },
    )


def _record_delivery_result(item: TelegramOutbox, **fieldToValue) -> None:
    with transaction.atomic():
        updated = TelegramOutbox.objects.filter(
            pk=item.pk,
            status=TelegramOutbox.Status.PENDING,
        ).update(**fieldToValue)
        if updated and (item.idempotency_key or "").startswith("lead:"):
            from main.models import Lead

            Lead.objects.filter(notification_outbox_id=item.pk).update(
                notification_sent=fieldToValue.get("status") == TelegramOutbox.Status.SENT,
                notification_error=fieldToValue.get("last_error", ""),
            )
        if updated and fieldToValue.get("status") == TelegramOutbox.Status.SENT:
            try:
                # Analytics failure must not roll back the successful delivery
                # marker and cause an already delivered message to be resent.
                with transaction.atomic():
                    from .useful_delivery import record_first_observed_value

                    record_first_observed_value(item, sent_at=fieldToValue.get("sent_at"))
            except Exception:
                logger.exception("Could not record useful delivery outbox_id=%s", item.pk)
            try:
                with transaction.atomic():
                    from .connection_check import record_check_delivery

                    record_check_delivery(item, sent_at=fieldToValue["sent_at"])
            except Exception:
                logger.exception("Could not record connection check delivery outbox_id=%s", item.pk)


def _cleanup_delivered_history_file(item):
    if item.method != TelegramOutbox.Method.SEND_DOCUMENT_FILE:
        return
    def cleanup():
        # Never remove a pending file, including after an outer rollback.
        if not TelegramOutbox.objects.filter(pk=item.pk, status__in=[TelegramOutbox.Status.SENT, TelegramOutbox.Status.DROPPED]).exists():
            return
        from .history_files import remove_history_document

        try:
            remove_history_document(item.payload.get("document_path", ""))
        except (OSError, ValueError):
            logger.warning("Could not clean delivered history file outbox_id=%s", item.pk)

    transaction.on_commit(cleanup)


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

    try:
        return _deliver_outbox_record(item)
    finally:
        lock_file = getattr(item, "_history_delivery_lock", None)
        if lock_file is not None:
            lock_file.close()


def _deliver_outbox_record(item):
    pk = item.pk
    try:
        ok, error = _dispatch_outbox_item(item)
    except BlockingIOError:
        return "skipped"
    if ok:
        _record_delivery_result(
            item,
            status=TelegramOutbox.Status.SENT,
            sent_at=timezone.now(),
            last_error="",
        )
        _cleanup_delivered_history_file(item)
        observe_metric(OUTBOX_EVENTS, 1, {"status": "sent", "method": item.method})
        logger.info("Outbox sent id=%s method=%s chat_id=%s", pk, item.method, item.chat_id)
        return "sent"

    if _is_permanent_send_error(error):
        if item.method == TelegramOutbox.Method.SEND_PROTECTED_MEDIA and "download limit" in (error or "").lower():
            notice = enqueue_outbox(chat_id=item.chat_id, method=TelegramOutbox.Method.SEND_MESSAGE,
                                    payload={"text": "Не удалось сохранить вложение: Bot API Telegram позволяет скачать файл размером до 20 МБ. Попробуйте файл меньшего размера."},
                                    idempotency_key=f"media-error:{item.pk}")
            if notice is None:
                raise RuntimeError("Could not persist media failure notice")
        _record_delivery_result(
            item,
            status=TelegramOutbox.Status.DROPPED,
            last_error=(error or "unknown error")[:1000],
        )
        _cleanup_delivered_history_file(item)
        logger.warning(
            "Outbox dropped id=%s chat_id=%s permanent error=%s",
            pk,
            item.chat_id,
            error,
        )
        observe_metric(OUTBOX_EVENTS, 1, {"status": "dropped", "method": item.method})
        return "dropped"

    new_attempts = item.attempts + 1
    _record_delivery_result(
        item,
        attempts=F("attempts") + 1,
        last_error=(error or "unknown error")[:1000],
        next_attempt_at=_next_attempt_at(new_attempts, error),
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
    delivery_context: dict | None = None,
) -> TelegramOutbox:
    """Сначала надёжно сохраняет ответ, затем без ожидания таймера отправляет его."""
    item = enqueue_outbox(
        chat_id=chat_id,
        method=method,
        payload=payload,
        idempotency_key=idempotency_key,
        delivery_context=delivery_context,
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
