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
    ONBOARDING_CONNECTIONS_TOTAL,
    ONBOARDING_FUNNEL_TOTAL,
    ONBOARDING_MILESTONES_TOTAL,
    STARTED_USERS,
    SYSTEM_MEMORY_AVAILABLE,
    SYSTEM_MEMORY_USED,
    set_gauge,
)
from .models import (
    TelegramIncomingUpdate,
    TelegramOutbox,
    UserTg,
    WhoUpdateOnboardingFunnel,
)
from .resource_metrics import collect_resource_snapshot


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

    funnel_stages = {
        "landing_only": WhoUpdateOnboardingFunnel.objects.filter(
            landing_viewed_at__isnull=False,
            telegram_started_at__isnull=True,
        ).count(),
        "started": WhoUpdateOnboardingFunnel.objects.filter(
            telegram_started_at__isnull=False,
            first_reminder_sent_at__isnull=True,
            connected_at__isnull=True,
        ).count(),
        "after_first_reminder": WhoUpdateOnboardingFunnel.objects.filter(
            first_reminder_sent_at__isnull=False,
            second_reminder_sent_at__isnull=True,
            connected_at__isnull=True,
        ).count(),
        "after_second_reminder": WhoUpdateOnboardingFunnel.objects.filter(
            second_reminder_sent_at__isnull=False,
            connected_at__isnull=True,
        ).count(),
        "connected": WhoUpdateOnboardingFunnel.objects.filter(
            connected_at__isnull=False,
        ).count(),
    }
    for stage, count in funnel_stages.items():
        set_gauge(ONBOARDING_FUNNEL_TOTAL, count, {"stage": stage})

    milestones = {
        "landing": WhoUpdateOnboardingFunnel.objects.filter(
            landing_viewed_at__isnull=False,
        ).count(),
        "telegram_start": WhoUpdateOnboardingFunnel.objects.filter(
            telegram_started_at__isnull=False,
        ).count(),
        "demo_opened": WhoUpdateOnboardingFunnel.objects.filter(
            demo_opened_at__isnull=False,
        ).count(),
        "reminder_1_sent": WhoUpdateOnboardingFunnel.objects.filter(
            first_reminder_sent_at__isnull=False,
        ).count(),
        "reminder_2_sent": WhoUpdateOnboardingFunnel.objects.filter(
            second_reminder_sent_at__isnull=False,
        ).count(),
        "connected": WhoUpdateOnboardingFunnel.objects.filter(
            connected_at__isnull=False,
        ).count(),
    }
    for milestone, count in milestones.items():
        set_gauge(ONBOARDING_MILESTONES_TOTAL, count, {"milestone": milestone})

    for stage in WhoUpdateOnboardingFunnel.ConnectionStage.values:
        set_gauge(
            ONBOARDING_CONNECTIONS_TOTAL,
            WhoUpdateOnboardingFunnel.objects.filter(connection_stage=stage).count(),
            {"stage": stage},
        )


def collect_system_gauges() -> None:
    snapshot = collect_resource_snapshot(cpu_interval=0)
    set_gauge(SYSTEM_MEMORY_USED, snapshot.memory_used_pct)
    set_gauge(SYSTEM_MEMORY_AVAILABLE, snapshot.memory_available)
