from __future__ import annotations

from django.db.models import Count, Max, Min, Q
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
    ONBOARDING_CONNECTIONS_TOTAL,
    ONBOARDING_FUNNEL_TOTAL,
    ONBOARDING_MILESTONES_TOTAL,
    METRIKA_OFFLINE_QUEUE_TOTAL,
    STARTED_USERS,
    SYSTEM_MEMORY_AVAILABLE,
    SYSTEM_MEMORY_USED,
    set_gauges,
)
from .models import (
    TelegramIncomingUpdate,
    TelegramOutbox,
    UserTg,
    WhoUpdateOnboardingFunnel,
    WhoUpdateMetrikaConversion,
)
from .resource_metrics import collect_resource_snapshot


def _age_seconds(created_at, now) -> float:
    return max(0, (now - created_at).total_seconds()) if created_at else 0


def collect_database_gauges() -> None:
    now = timezone.now()
    observations = []

    def add_gauge(metric_name, value, labelToValue=None):
        observations.append((metric_name, value, labelToValue))

    pending_outbox = TelegramOutbox.objects.filter(
        status=TelegramOutbox.Status.PENDING,
    )
    fieldToOutbox = pending_outbox.aggregate(
        size=Count("id"), due=Count("id", filter=Q(next_attempt_at__lte=now)),
        oldest=Min("created_at"), max_attempts=Max("attempts")
    )
    add_gauge(OUTBOX_SIZE, fieldToOutbox["size"])
    add_gauge(OUTBOX_DUE, fieldToOutbox["due"])
    add_gauge(OUTBOX_OLDEST_AGE, _age_seconds(fieldToOutbox["oldest"], now))
    add_gauge(OUTBOX_MAX_ATTEMPTS, fieldToOutbox["max_attempts"] or 0)

    active_statuses = [
        TelegramIncomingUpdate.Status.PENDING,
        TelegramIncomingUpdate.Status.PROCESSING,
    ]
    for queue in TelegramIncomingUpdate.Queue.values:
        pending = TelegramIncomingUpdate.objects.filter(queue=queue, status__in=active_statuses)
        fieldToState = pending.aggregate(size=Count("id"), oldest=Min("created_at"), max_attempts=Max("attempts"))
        labelToValue = {"queue": queue}
        add_gauge(INCOMING_BACKLOG, fieldToState["size"], labelToValue)
        add_gauge(INCOMING_OLDEST_AGE, _age_seconds(fieldToState["oldest"], now), labelToValue)
        add_gauge(INCOMING_MAX_ATTEMPTS, fieldToState["max_attempts"] or 0, labelToValue)

    fieldToUserCount = UserTg.objects.aggregate(
        started=Count("id", filter=Q(last_start_at__isnull=False)),
        connected=Count("id", filter=Q(business_is_connected=True)),
        expired=Count("id", filter=Q(access_unlimited=False, access_expires_at__lte=now)),
        active=Count("id", filter=Q(access_unlimited=False, access_expires_at__gt=now)),
    )
    started = fieldToUserCount["started"]
    connected = fieldToUserCount["connected"]
    add_gauge(STARTED_USERS, started)
    add_gauge(ACTIVE_BUSINESS_CONNECTIONS, connected)
    add_gauge(CONNECTION_CONVERSION, connected * 100 / started if started else 0)
    add_gauge(EXPIRED_SUBSCRIPTIONS, fieldToUserCount["expired"])
    add_gauge(ACTIVE_LIMITED_SUBSCRIPTIONS, fieldToUserCount["active"])

    stageToFilter = {
        "landing_only": Q(landing_viewed_at__isnull=False, telegram_started_at__isnull=True) & ~Q(landing_path="/bot/checkout/"),
        "started": Q(telegram_started_at__isnull=False, first_reminder_sent_at__isnull=True, connected_at__isnull=True),
        "after_first_reminder": Q(first_reminder_sent_at__isnull=False, second_reminder_sent_at__isnull=True, connected_at__isnull=True),
        "after_second_reminder": Q(second_reminder_sent_at__isnull=False, connected_at__isnull=True),
        "connected": Q(connected_at__isnull=False),
    }
    milestoneToFilter = {
        "landing": Q(landing_viewed_at__isnull=False) & ~Q(landing_path="/bot/checkout/"),
        "telegram_start": Q(telegram_started_at__isnull=False),
        "demo_opened": Q(demo_opened_at__isnull=False),
        "reminder_1_sent": Q(first_reminder_sent_at__isnull=False),
        "reminder_2_sent": Q(second_reminder_sent_at__isnull=False),
        "connected": Q(connected_at__isnull=False),
    }
    keyToAggregate = {f"stage_{key}": Count("id", filter=value) for key, value in stageToFilter.items()}
    keyToAggregate.update({f"milestone_{key}": Count("id", filter=value) for key, value in milestoneToFilter.items()})
    for stage in WhoUpdateOnboardingFunnel.ConnectionStage.values:
        keyToAggregate[f"connection_{stage}"] = Count("id", filter=Q(connection_stage=stage))
    keyToCount = WhoUpdateOnboardingFunnel.objects.aggregate(**keyToAggregate)
    for stage in stageToFilter:
        add_gauge(ONBOARDING_FUNNEL_TOTAL, keyToCount[f"stage_{stage}"], {"stage": stage})
    for milestone in milestoneToFilter:
        add_gauge(ONBOARDING_MILESTONES_TOTAL, keyToCount[f"milestone_{milestone}"], {"milestone": milestone})
    for stage in WhoUpdateOnboardingFunnel.ConnectionStage.values:
        add_gauge(ONBOARDING_CONNECTIONS_TOTAL, keyToCount[f"connection_{stage}"], {"stage": stage})

    statusToCount = dict(WhoUpdateMetrikaConversion.objects.values_list("status").annotate(total=Count("id")))
    for status in WhoUpdateMetrikaConversion.Status.values:
        add_gauge(METRIKA_OFFLINE_QUEUE_TOTAL, statusToCount.get(status, 0), {"status": status})
    set_gauges(observations)


def collect_system_gauges() -> None:
    snapshot = collect_resource_snapshot(cpu_interval=0)
    set_gauges([
        (SYSTEM_MEMORY_USED, snapshot.memory_used_pct, None),
        (SYSTEM_MEMORY_AVAILABLE, snapshot.memory_available, None),
    ])
