"""Durable first-touch and first-connection facts, separate from funnel attempts."""
from django.db.models import F, Q
from django.utils import timezone

from .models import WhoUpdateOnboardingFunnel, WhoUpdateUserAcquisition


def acquisition_history(bot_user, *, before=None):
    """Only use existing facts; never infer a visit from a connection/payment."""
    funnels = WhoUpdateOnboardingFunnel.objects.filter(user=bot_user)
    cutoff = before or timezone.now()
    starts = funnels.filter(telegram_started_at__lte=cutoff)
    connections = funnels.filter(connected_at__lt=cutoff)
    first_start = starts.order_by("telegram_started_at", "pk").first()
    acquisition = (
        starts.filter(
            Q(yclid__gt="") | Q(metrika_client_id__gt=""),
            landing_viewed_at__isnull=False,
            landing_viewed_at__lte=F("telegram_started_at"),
        )
        .exclude(landing_path="/bot/checkout/")
        .order_by("telegram_started_at", "pk")
        .first()
    )
    first_connection = connections.order_by("connected_at", "pk").first()
    # business_connected_at is the LAST connection, not a trustworthy first
    # connection date. Retain only the fact of legacy history in that case.
    legacy_connected = bool(
        bot_user.business_connected_at
        and bot_user.business_connected_at < cutoff
    )
    legacy_disconnected = bool(
        bot_user.business_disconnected_at
        and bot_user.business_disconnected_at < cutoff
    )
    if first_connection is not None and any(
        timestamp is not None and timestamp < first_connection.connected_at
        for timestamp in (bot_user.business_connected_at, bot_user.business_disconnected_at)
    ):
        # An older known connection/disconnection proves the earliest retained
        # funnel is a reconnect. Its date is not the user's first connection.
        first_connection = None
    history_exists = bool(
        first_connection or legacy_connected or legacy_disconnected
        or (before is None and (bot_user.business_is_connected or bot_user.business_connection_id))
    )
    return {
        "first_start_funnel": first_start,
        "first_start_at": first_start.telegram_started_at if first_start else None,
        "acquisition_funnel": acquisition,
        "first_connection_funnel": first_connection,
        "first_connected_at": first_connection.connected_at if first_connection else None,
        "has_connection_history": history_exists,
    }


def acquisition_changes(acquisition, fieldToProposedValue):
    """Backfills fill gaps only; a later attempt cannot replace first facts."""
    fieldToValue = {}
    for field, value in fieldToProposedValue.items():
        if (
            field in ("first_connected_at", "first_connection_funnel")
            and acquisition.has_connection_history
        ):
            # An old connection with unknown date must never turn into a new
            # customer's first conversion when they reconnect after rollout.
            continue
        if value is not None and value is not False and not getattr(acquisition, field):
            fieldToValue[field] = value
    return fieldToValue


def ensure_acquisition(bot_user, *, before=None):
    """Caller holds the user's row lock, shared by starts/connections/backfill."""
    acquisition, created = WhoUpdateUserAcquisition.objects.get_or_create(user=bot_user)
    if created or not acquisition.acquisition_funnel_id or not acquisition.has_connection_history:
        fieldToValue = acquisition_changes(
            acquisition, acquisition_history(bot_user, before=before),
        )
        if fieldToValue:
            for field, value in fieldToValue.items():
                setattr(acquisition, field, value)
            acquisition.save(update_fields=list(fieldToValue))
    return acquisition


def attribution_funnel(funnel, *, occurred_at=None):
    """Return one real visit, never splice identifiers from different browsers."""
    occurred_at = occurred_at or funnel.connected_at or funnel.telegram_started_at
    if funnel.yclid or funnel.metrika_client_id:
        if occurred_at is not None and (
            (funnel.landing_viewed_at and funnel.landing_viewed_at > occurred_at)
            or (funnel.telegram_started_at and funnel.telegram_started_at > occurred_at)
        ):
            return None
        return funnel
    if not funnel.user_id:
        return None
    try:
        acquisition = funnel.user.acquisition
    except WhoUpdateUserAcquisition.DoesNotExist:
        acquisition = None
    source = acquisition.acquisition_funnel if acquisition else None
    if source is None or source.user_id != funnel.user_id:
        return None
    if not (source.yclid or source.metrika_client_id):
        return None
    if (
        occurred_at is None or source.telegram_started_at is None
        or source.landing_viewed_at is None
        or source.telegram_started_at > occurred_at
        or source.landing_viewed_at > occurred_at
    ):
        return None
    return source


def is_first_connection_funnel(funnel):
    if not funnel.user_id or funnel.connected_at is None:
        return False
    return WhoUpdateUserAcquisition.objects.filter(
        user_id=funnel.user_id,
        first_connection_funnel_id=funnel.pk,
        first_connected_at=funnel.connected_at,
    ).exists()
