from __future__ import annotations

import logging
import time
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from requests import RequestException

from .idempotency import mark_webhook_update_processed
from .incoming_notifications import notify_incoming_queue
from .models import TelegramIncomingUpdate, WebhookUpdate
from .metrics import (
    INCOMING_END_TO_END_DURATION,
    INCOMING_PROCESSING_DURATION,
    INCOMING_UPDATES_PROCESSED,
    INCOMING_UPDATES_RECEIVED,
    observe_metric,
    observe_sqlite_lock,
)

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 8
MAX_BACKOFF_SECONDS = 60
STALE_AFTER = timedelta(minutes=5)
NETWORK_RETRY_SECONDS = 30


def classify_update(data: dict) -> str:
    """Команды пользователя не конкурируют с потоком Telegram Business."""
    if (
        data.get("message")
        or data.get("edited_message")
        or data.get("callback_query")
        or data.get("pre_checkout_query")
        or data.get("business_connection")
    ):
        return TelegramIncomingUpdate.Queue.PRIORITY
    return TelegramIncomingUpdate.Queue.BUSINESS


def enqueue_incoming_update(data: dict, *, source: str = "unknown") -> tuple[TelegramIncomingUpdate | None, bool]:
    update_id = data.get("update_id")
    if update_id is None:
        raise ValueError("Telegram update without update_id")

    try:
        with transaction.atomic():
            item, created = TelegramIncomingUpdate.objects.get_or_create(
                update_id=update_id,
                defaults={
                    "payload": data,
                    "queue": classify_update(data),
                    "next_attempt_at": timezone.now(),
                },
            )
            if created:
                observe_metric(
                    INCOMING_UPDATES_RECEIVED,
                    1,
                    {"queue": item.queue, "source": source},
                )
                transaction.on_commit(
                    lambda queue=item.queue, using=item._state.db: notify_incoming_queue(queue, using=using),
                    using=item._state.db,
                )
            return item, created
    except IntegrityError:
        return TelegramIncomingUpdate.objects.filter(update_id=update_id).first(), False


def recover_stale_updates(queue: str) -> int:
    now = timezone.now()
    stale_before = now - STALE_AFTER
    return TelegramIncomingUpdate.objects.filter(
        queue=queue,
        status=TelegramIncomingUpdate.Status.PROCESSING,
    ).filter(
        Q(started_at__lt=stale_before)
        | Q(started_at__isnull=True, created_at__lt=stale_before),
    ).update(
        status=TelegramIncomingUpdate.Status.PENDING,
        started_at=None,
        next_attempt_at=now,
        last_error="Recovered after stale processing lease",
    )


def claim_next_update(queue: str) -> TelegramIncomingUpdate | None:
    now = timezone.now()
    candidate = (
        TelegramIncomingUpdate.objects.filter(
            queue=queue,
            status=TelegramIncomingUpdate.Status.PENDING,
            next_attempt_at__lte=now,
        )
        .order_by("created_at")
        .first()
    )
    if candidate is None:
        return None

    claimed = TelegramIncomingUpdate.objects.filter(
        pk=candidate.pk,
        status=TelegramIncomingUpdate.Status.PENDING,
    ).update(
        status=TelegramIncomingUpdate.Status.PROCESSING,
        started_at=now,
    )
    if not claimed:
        return None
    candidate.status = TelegramIncomingUpdate.Status.PROCESSING
    candidate.started_at = now
    return candidate


def process_claimed_update(item: TelegramIncomingUpdate) -> bool:
    started = time.monotonic()
    if WebhookUpdate.objects.filter(update_id=item.update_id).exists():
        TelegramIncomingUpdate.objects.filter(pk=item.pk).update(
            status=TelegramIncomingUpdate.Status.DONE,
            processed_at=timezone.now(),
            last_error="",
        )
        observe_metric(INCOMING_UPDATES_PROCESSED, 1, {"queue": item.queue, "status": "duplicate"})
        return True

    try:
        # Импорт здесь не создаёт цикл views -> incoming -> views.
        from .views import process_telegram_update

        process_telegram_update(item.payload, use_idempotency=False)
        mark_webhook_update_processed(item.update_id)
    except Exception as exc:
        observe_sqlite_lock(exc, "incoming_worker")
        # A transport outage says nothing about whether the update is valid.
        # Keep it queued until Telegram is reachable instead of exhausting
        # the handler-error budget while the circuit breaker is open.
        network_error = isinstance(exc, RequestException)
        attempts = item.attempts if network_error else item.attempts + 1
        terminal = not network_error and attempts >= MAX_ATTEMPTS
        delay = NETWORK_RETRY_SECONDS if network_error else min(
            2 ** max(attempts - 1, 0), MAX_BACKOFF_SECONDS,
        )
        TelegramIncomingUpdate.objects.filter(pk=item.pk).update(
            status=(
                TelegramIncomingUpdate.Status.FAILED
                if terminal
                else TelegramIncomingUpdate.Status.PENDING
            ),
            attempts=attempts,
            started_at=None,
            next_attempt_at=timezone.now() + timedelta(seconds=delay),
            last_error=str(exc)[:2000],
        )
        logger.exception(
            "Incoming Telegram update failed update_id=%s queue=%s attempt=%s",
            item.update_id,
            item.queue,
            attempts,
        )
        observe_metric(
            INCOMING_PROCESSING_DURATION,
            (time.monotonic() - started) * 1000,
            {"queue": item.queue, "status": "error"},
        )
        observe_metric(INCOMING_UPDATES_PROCESSED, 1, {"queue": item.queue, "status": "error"})
        return False

    processed_at = timezone.now()
    TelegramIncomingUpdate.objects.filter(pk=item.pk).update(
        status=TelegramIncomingUpdate.Status.DONE,
        processed_at=processed_at,
        last_error="",
    )
    observe_metric(
        INCOMING_PROCESSING_DURATION,
        (time.monotonic() - started) * 1000,
        {"queue": item.queue, "status": "success"},
    )
    observe_metric(
        INCOMING_END_TO_END_DURATION,
        max(0, (processed_at - item.created_at).total_seconds() * 1000),
        {"queue": item.queue},
    )
    observe_metric(INCOMING_UPDATES_PROCESSED, 1, {"queue": item.queue, "status": "success"})
    return True
