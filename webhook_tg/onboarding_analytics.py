from __future__ import annotations

from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from main.context_processors import site
from .acquisition import attribution_funnel, ensure_acquisition, is_first_connection_funnel
from .metrics import ONBOARDING_EVENTS, observe_metric
from .models import UserTg, WhoUpdateOnboardingFunnel, WhoUpdateTelegramStart


TRACKING_PREFIX = "trk_"
TRACKING_QUERY_FIELDS = (
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
    "utm_device",
    "utm_region",
    "yclid",
)


def _query_value(request, name: str) -> str:
    return str(request.GET.get(name) or "").strip()[:255]


def capture_landing_view(request) -> WhoUpdateOnboardingFunnel:
    funnel = WhoUpdateOnboardingFunnel.objects.create(
        landing_path=str(request.path or "/bot/")[:255],
        landing_viewed_at=timezone.now(),
        metrika_counter_id=site(request)["site"]["metrika_id"],
        **{name: _query_value(request, name) for name in TRACKING_QUERY_FIELDS},
    )
    observe_metric(
        ONBOARDING_EVENTS,
        1,
        {
            "event": "landing_view",
            "source": funnel.utm_source or "direct",
        },
    )
    return funnel


def tracked_telegram_url(funnel: WhoUpdateOnboardingFunnel) -> str:
    return f"https://t.me/who_update_bot?start={TRACKING_PREFIX}{funnel.tracking_code}"


@transaction.atomic
def register_telegram_start(
    bot_user: UserTg,
    start_payload: str,
    *,
    update_id: int,
    started_at,
) -> WhoUpdateOnboardingFunnel:
    bot_user = UserTg.objects.select_for_update().get(pk=bot_user.pk)
    receipt = WhoUpdateTelegramStart.objects.select_related("funnel").filter(update_id=update_id).first()
    if receipt is not None:
        if receipt.user_id != bot_user.pk:
            raise ValueError("Telegram update belongs to a different user")
        return receipt.funnel
    existing = (
        WhoUpdateOnboardingFunnel.objects.select_for_update()
        .filter(start_update_id=update_id)
        .first()
    )
    if existing is not None:
        if existing.user_id != bot_user.pk:
            raise ValueError("Telegram update belongs to a different user")
        return existing

    acquisition = ensure_acquisition(bot_user, before=started_at)
    payload = str(start_payload or "").strip()
    funnel = None
    if payload.startswith(TRACKING_PREFIX):
        tracking_code = payload[len(TRACKING_PREFIX) :]
        funnel = (
            WhoUpdateOnboardingFunnel.objects.select_for_update()
            .filter(tracking_code=tracking_code)
            .first()
        )
        if funnel is not None and funnel.user_id not in (None, bot_user.pk):
            # A forwarded deep link must not transfer somebody else's visit.
            funnel = None

    if funnel is not None and funnel.user_id == bot_user.pk:
        # Keep the original visit/start timestamp and update ID. Record this
        # command independently so a retry is still idempotent.
        WhoUpdateTelegramStart.objects.create(
            user=bot_user, funnel=funnel, update_id=update_id, started_at=started_at,
        )
        return funnel

    if funnel is None:
        source = "referral" if payload.lower().startswith("ref_") else "telegram"
        funnel = WhoUpdateOnboardingFunnel.objects.create(utm_source=source)

    funnel.user = bot_user
    funnel.telegram_started_at = started_at
    funnel.start_update_id = update_id
    funnel.save(update_fields=["user", "telegram_started_at", "start_update_id", "updated_at"])
    WhoUpdateTelegramStart.objects.create(
        user=bot_user, funnel=funnel, update_id=update_id, started_at=started_at,
    )
    fields = []
    if acquisition.first_start_funnel_id is None:
        acquisition.first_start_funnel = funnel
        acquisition.first_start_at = started_at
        fields.extend(["first_start_funnel", "first_start_at"])
    if (
        acquisition.acquisition_funnel_id is None and funnel.landing_viewed_at
        and funnel.landing_viewed_at <= started_at
        and (funnel.yclid or funnel.metrika_client_id)
        and funnel.landing_path != "/bot/checkout/"
    ):
        acquisition.acquisition_funnel = funnel
        fields.append("acquisition_funnel")
    if fields:
        acquisition.save(update_fields=fields)
    observe_metric(
        ONBOARDING_EVENTS,
        1,
        {"event": "telegram_start", "source": funnel.utm_source or "direct"},
    )
    return funnel


def record_demo_opened(bot_user: UserTg | None) -> None:
    if bot_user is None:
        return
    funnel = _latest_funnel(bot_user)
    if funnel is not None and funnel.demo_opened_at is None:
        funnel.demo_opened_at = timezone.now()
        funnel.save(update_fields=["demo_opened_at", "updated_at"])
    observe_metric(ONBOARDING_EVENTS, 1, {"event": "demo_opened"})


def _latest_funnel(bot_user: UserTg):
    return (
        WhoUpdateOnboardingFunnel.objects.filter(
            user=bot_user,
            telegram_started_at__isnull=False,
            connected_at__isnull=True,
        )
        .order_by("-telegram_started_at", "-id")
        .first()
    )


def record_reminder_sent(
    bot_user: UserTg,
    reminder_number: int,
    *,
    sent_at=None,
    funnel_pk: int | None = None,
) -> None:
    sent_at = sent_at or timezone.now()
    funnel = None
    if funnel_pk:
        funnel = WhoUpdateOnboardingFunnel.objects.filter(
            pk=funnel_pk,
            user=bot_user,
            connected_at__isnull=True,
        ).first()
    funnel = funnel or _latest_funnel(bot_user)
    if funnel is None:
        return

    reminder_number = int(reminder_number)
    if reminder_number == 1:
        field = "first_reminder_sent_at"
        event = "reminder_1_sent"
    elif reminder_number == 2:
        field = "second_reminder_sent_at"
        event = "reminder_2_sent"
    else:
        return
    if getattr(funnel, field) is not None:
        return
    setattr(funnel, field, sent_at)
    funnel.save(update_fields=[field, "updated_at"])
    observe_metric(ONBOARDING_EVENTS, 1, {"event": event})


@transaction.atomic
def record_connection(bot_user: UserTg, *, connected_at=None, had_connection_history=False):
    connected_at = connected_at or timezone.now()
    bot_user = UserTg.objects.select_for_update().get(pk=bot_user.pk)
    acquisition = ensure_acquisition(bot_user, before=connected_at)
    if had_connection_history and not acquisition.has_connection_history:
        acquisition.has_connection_history = True
        acquisition.save(update_fields=["has_connection_history"])
    funnel = (
        WhoUpdateOnboardingFunnel.objects.select_for_update()
        .filter(
            user=bot_user,
            telegram_started_at__isnull=False,
            connected_at__isnull=True,
        )
        .order_by("-telegram_started_at", "-id")
        .first()
    )
    if funnel is None:
        funnel = WhoUpdateOnboardingFunnel.objects.create(
            user=bot_user,
            utm_source="unknown",
            telegram_started_at=bot_user.last_start_at,
        )

    first_reminder_at = funnel.first_reminder_sent_at
    second_reminder_at = funnel.second_reminder_sent_at
    if not acquisition.has_connection_history:
        # A repeated /start is not a new person and cannot erase reminder
        # exposure. This describes sequence, not causal reminder uplift.
        fieldToLatestSentAt = WhoUpdateOnboardingFunnel.objects.filter(user=bot_user).aggregate(
            first=Max("first_reminder_sent_at", filter=Q(first_reminder_sent_at__lte=connected_at)),
            second=Max("second_reminder_sent_at", filter=Q(second_reminder_sent_at__lte=connected_at)),
        )
        first_reminder_at = fieldToLatestSentAt["first"]
        second_reminder_at = fieldToLatestSentAt["second"]

    if second_reminder_at and second_reminder_at <= connected_at:
        stage = WhoUpdateOnboardingFunnel.ConnectionStage.AFTER_SECOND_REMINDER
    elif first_reminder_at and first_reminder_at <= connected_at:
        stage = WhoUpdateOnboardingFunnel.ConnectionStage.AFTER_FIRST_REMINDER
    elif funnel.telegram_started_at:
        stage = WhoUpdateOnboardingFunnel.ConnectionStage.IMMEDIATE
    else:
        stage = WhoUpdateOnboardingFunnel.ConnectionStage.UNKNOWN

    funnel.connected_at = connected_at
    funnel.connection_stage = stage
    funnel.save(update_fields=["connected_at", "connection_stage", "updated_at"])
    if not acquisition.has_connection_history:
        acquisition.first_connected_at = connected_at
        acquisition.first_connection_funnel = funnel
        acquisition.has_connection_history = True
        acquisition.save(update_fields=[
            "first_connected_at", "first_connection_funnel", "has_connection_history",
        ])
        observe_metric(ONBOARDING_EVENTS, 1, {"event": "first_connected", "stage": stage})
    observe_metric(ONBOARDING_EVENTS, 1, {"event": "connected", "stage": stage})
    return funnel
