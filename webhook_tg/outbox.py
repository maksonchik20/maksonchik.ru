import hashlib
import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from .models import TelegramOutbox
from .telegram import dispatch_telegram_request

logger = logging.getLogger(__name__)

MAX_BACKOFF_SECONDS = 3600
INITIAL_BACKOFF_SECONDS = 30


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

        TelegramOutbox.objects.filter(pk=pk).update(
            attempts=F("attempts") + 1,
            last_error=(error or "unknown error")[:1000],
            next_attempt_at=_next_attempt_at(item.attempts + 1),
        )
        stats["failed"] += 1
        logger.warning(
            "Outbox retry scheduled id=%s method=%s chat_id=%s error=%s",
            pk,
            item.method,
            item.chat_id,
            error,
        )

    return stats
