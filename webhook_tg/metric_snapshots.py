from __future__ import annotations

from django.db.models import Max, Min
from django.utils import timezone

from .metrics import (
    ACTIVE_BUSINESS_CONNECTIONS,
    ACTIVE_LIMITED_SUBSCRIPTIONS,
    CONNECTION_CONVERSION,
    EXPIRED_SUBSCRIPTIONS,
    INCOMING_BACKLOG,
    INCOMING_MAX_ATTEMPTS,
    INCOMING_OLDEST_AGE,
    OUTBOX_DUE,
    OUTBOX_MAX_ATTEMPTS,
    OUTBOX_OLDEST_AGE,
    OUTBOX_SIZE,
    STARTED_USERS,
    set_gauge,
)
from .models import TelegramIncomingUpdate, TelegramOutbox, UserTg


def _age_seconds(created_at, now) -> float:
    return max(0, (now - created_at).total_seconds()) if created_at else 0


def collect_database_gauges() -> None:
    now = timezone.now()

    outbox = TelegramOutbox.objects.aggregate(
        size=Max("id"), oldest=Min("created_at"), max_attempts=Max("attempts")
    )
    set_gauge(OUTBOX_SIZE, TelegramOutbox.objects.count())
    set_gauge(OUTBOX_DUE, TelegramOutbox.objects.filter(next_attempt_at__lte=now).count())
    set_gauge(OUTBOX_OLDEST_AGE, _age_seconds(outbox["oldest"], now))
    set_gauge(OUTBOX_MAX_ATTEMPTS, outbox["max_attempts"] or 0)

    active_statuses = [
        TelegramIncomingUpdate.Status.PENDING,
        TelegramIncomingUpdate.Status.PROCESSING,
    ]
    for queue in TelegramIncomingUpdate.Queue.values:
        pending = TelegramIncomingUpdate.objects.filter(queue=queue, status__in=active_statuses)
        state = pending.aggregate(oldest=Min("created_at"), max_attempts=Max("attempts"))
        labels = {"queue": queue}
        set_gauge(INCOMING_BACKLOG, pending.count(), labels)
        set_gauge(INCOMING_OLDEST_AGE, _age_seconds(state["oldest"], now), labels)
        set_gauge(INCOMING_MAX_ATTEMPTS, state["max_attempts"] or 0, labels)

    started = UserTg.objects.filter(last_start_at__isnull=False).count()
    connected = UserTg.objects.filter(business_is_connected=True).count()
    set_gauge(STARTED_USERS, started)
    set_gauge(ACTIVE_BUSINESS_CONNECTIONS, connected)
    set_gauge(CONNECTION_CONVERSION, connected * 100 / started if started else 0)
    set_gauge(
        EXPIRED_SUBSCRIPTIONS,
        UserTg.objects.filter(
            access_unlimited=False,
            access_expires_at__isnull=False,
            access_expires_at__lte=now,
        ).count(),
    )
    set_gauge(
        ACTIVE_LIMITED_SUBSCRIPTIONS,
        UserTg.objects.filter(
            access_unlimited=False,
            access_expires_at__gt=now,
        ).count(),
    )
