"""Read-only cohort report. No customer identifiers, API calls or goal exports."""
import json
from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from webhook_tg.models import UserTg, WhoUpdateFirstObservedValue, WhoUpdatePaymentOrder, WhoUpdateUserAcquisition
from webhook_tg.subscriptions import OWNER_TELEGRAM_ID


def _source(acquisition):
    source = acquisition.acquisition_funnel
    if (
        source is not None and source.telegram_started_at is not None
        and source.landing_viewed_at is not None
        and source.user_id == acquisition.user_id
        and source.telegram_started_at <= acquisition.first_connected_at
        and source.landing_viewed_at <= acquisition.first_connected_at
    ):
        if source.yclid or source.utm_source.lower() in ("yandex", "yandex_direct"):
            return "recorded_yandex_visit"
        return "other_identified_visit"
    first = acquisition.first_start_funnel
    if (
        first is not None and first.utm_source == "referral"
        and first.telegram_started_at is not None
        and first.telegram_started_at <= acquisition.first_connected_at
    ):
        return "recorded_referral"
    return "unattributed"


def _payment_summary(orders):
    userPkToCount = defaultdict(int)
    for order in orders:
        userPkToCount[order.user_id] += 1
    return {
        "orders": len(orders),
        "payers": len(userPkToCount),
        "repeat_payers": sum(count > 1 for count in userPkToCount.values()),
        "repeat_orders": sum(max(count - 1, 0) for count in userPkToCount.values()),
        "gross_rub": str(sum((order.amount for order in orders), Decimal("0.00"))),
    }


def _connection_cohort(acquisitions, userPkToPayments, as_of):
    dayToStats = {}
    for days in (14, 30):
        mature = [item for item in acquisitions if item.first_connected_at + timedelta(days=days) <= as_of]
        prepaid = 0
        first_payers = 0
        paid_in_window = []
        for item in mature:
            orders = userPkToPayments.get(item.user_id, [])
            first_payment = orders[0].paid_at if orders else None
            deadline = item.first_connected_at + timedelta(days=days)
            if first_payment is not None and first_payment < item.first_connected_at:
                prepaid += 1
            elif first_payment is not None and first_payment < deadline:
                first_payers += 1
            paid_in_window.extend(
                order for order in orders if item.first_connected_at <= order.paid_at < deadline
            )
        eligible = len(mature) - prepaid
        dayToStats[f"d{days}"] = {
            "mature_users": len(mature),
            "immature_users": len(acquisitions) - len(mature),
            "already_paying_before_connection": prepaid,
            "eligible_non_payers": eligible,
            "first_payers_within_days": first_payers,
            "first_payment_conversion_percent": round(100 * first_payers / eligible, 2) if eligible else None,
            "payments_from_connection_to_day": _payment_summary(paid_in_window),
        }
    return {"users": len(acquisitions), **dayToStats}


class Command(BaseCommand):
    help = "Read-only JSON: unique first starts/connections, mature D14/D30 cohorts and actual payments. No PII."

    def add_arguments(self, parser):
        parser.add_argument("--since", help="Inclusive cohort date, YYYY-MM-DD; default: 60 days before as-of")
        parser.add_argument("--until", help="Exclusive cohort date, YYYY-MM-DD; default: as-of")
        parser.add_argument("--as-of", help="Exclusive observation cutoff, YYYY-MM-DD (local midnight); default: now")

    def handle(self, *args, **options):
        now = timezone.now()

        def boundary(value, default):
            if not value:
                return default
            try:
                day = datetime.strptime(value, "%Y-%m-%d").date()
            except ValueError as exc:
                raise CommandError("Dates must be YYYY-MM-DD") from exc
            return timezone.make_aware(datetime.combine(day, time.min))

        as_of = boundary(options["as_of"], now)
        until = boundary(options["until"], as_of)
        since = boundary(options["since"], as_of - timedelta(days=60))
        if as_of > now or not since < until <= as_of:
            raise CommandError("Require since < until <= as-of <= now")

        users = UserTg.objects.exclude(user_id=OWNER_TELEGRAM_ID)
        acquisitions = list(
            WhoUpdateUserAcquisition.objects.filter(user__in=users)
            .select_related("first_start_funnel", "first_connection_funnel", "acquisition_funnel")
            .order_by("user_id")
        )
        orders = list(
            WhoUpdatePaymentOrder.objects.filter(
                user__in=users, status=WhoUpdatePaymentOrder.Status.PAID,
                is_test=False, paid_at__lt=as_of,
            ).only("user_id", "paid_at", "amount").order_by("paid_at", "pk")
        )
        userPkToPayments = defaultdict(list)
        for order in orders:
            userPkToPayments[order.user_id].append(order)
        starts = [item for item in acquisitions if item.first_start_at and since <= item.first_start_at < until]
        connections = [item for item in acquisitions if item.first_connected_at and since <= item.first_connected_at < until]
        sourceToAcquisitions = defaultdict(list)
        dateToAcquisitions = defaultdict(list)
        stageToCount = defaultdict(int)
        for item in connections:
            sourceToAcquisitions[_source(item)].append(item)
            dateToAcquisitions[timezone.localdate(item.first_connected_at).isoformat()].append(item)
            stage = item.first_connection_funnel.connection_stage if item.first_connection_funnel else ""
            stageToCount[stage or "unknown"] += 1
        connection_user_pks = {item.user_id for item in connections}
        cohort_orders = [order for order in orders if order.user_id in connection_user_pks]
        period_orders = [order for order in orders if since <= order.paid_at < until]
        renewed_in_period = sum(
            len([order for order in user_orders if order.paid_at < since]) > 0
            and any(since <= order.paid_at < until for order in user_orders)
            for user_orders in userPkToPayments.values()
        )
        current_expired = users.filter(access_unlimited=False, access_expires_at__lte=now)
        observed_values = WhoUpdateFirstObservedValue.objects.filter(user__in=users, observed_at__lt=as_of)
        metricToValue = {
            "generated_at": now.isoformat(),
            "timezone": str(timezone.get_current_timezone()),
            "cohort_since_inclusive": since.isoformat(),
            "cohort_until_exclusive": until.isoformat(),
            "observation_as_of_exclusive": as_of.isoformat(),
            "owner_and_test_payments_excluded": True,
            "data_quality_current": {
                "users_without_acquisition_backfill": users.count() - len(acquisitions),
                "acquisition_rows_without_known_first_start_date": sum(item.first_start_at is None for item in acquisitions),
                "connected_history_without_known_first_connection_date": sum(
                    item.has_connection_history and item.first_connected_at is None for item in acquisitions
                ),
                "paid_orders_without_payment_date": WhoUpdatePaymentOrder.objects.filter(
                    user__in=users, is_test=False, status=WhoUpdatePaymentOrder.Status.PAID, paid_at__isnull=True,
                ).count(),
            },
            "first_starts_in_period": len(starts),
            "first_connections_in_period": len(connections),
            "start_cohort_known_connected_by_as_of": sum(
                item.first_connected_at is not None and item.first_connected_at < as_of for item in starts
            ),
            "connection_stage_counts": dict(stageToCount),
            "connection_cohort": _connection_cohort(connections, userPkToPayments, as_of),
            "connection_cohort_by_source": {
                source: _connection_cohort(items, userPkToPayments, as_of)
                for source, items in sorted(sourceToAcquisitions.items())
            },
            "connection_cohort_by_day": {
                day: _connection_cohort(items, userPkToPayments, as_of)
                for day, items in sorted(dateToAcquisitions.items())
            },
            "connection_cohort_payments_before_as_of": _payment_summary(cohort_orders),
            "calendar_period_cash": {
                **_payment_summary(period_orders),
                "returning_payers_with_prior_payment_before_period": renewed_in_period,
            },
            "first_observed_value_since_instrumentation": {
                "observed_users_before_as_of": observed_values.count(),
                "first_observed_in_calendar_period": observed_values.filter(observed_at__gte=since, observed_at__lt=until).count(),
                "connection_cohort_users_observed_before_as_of": observed_values.filter(user_id__in=connection_user_pks).count(),
                "coverage": "Successful full edit notifications via Outbox and known plain-text deletion copies accepted by Telegram. Not a read receipt; no historical backfill, no media/archive coverage.",
            },
            "access_snapshot_now_not_historical": {
                "as_of": now.isoformat(),
                "expired_users": current_expired.count(),
                "expired_and_connected_users": current_expired.filter(business_is_connected=True).count(),
                "expiry_still_set_to_today_users": current_expired.filter(
                    access_expires_at__gte=timezone.make_aware(datetime.combine(timezone.localdate(now), time.min)),
                ).count(),
            },
            "limitations": [
                "First dates are earliest trustworthy stored observations, not reconstructed missing history.",
                "D14/D30 denominators exclude immature users and people already paying before connection; unknown first dates are reported separately.",
                "Payment windows are [connection, connection + N days); payment totals are gross recorded PAID orders, not bank payout or verified refunds.",
                "Repeat payment is not a proven renewal. Past expiry or lost subscribers cannot be reconstructed from mutable current access_expires_at.",
                "repeat_payers/repeat_orders in each payment summary count repeats inside that summary's window, not all lifetime repeats; returning calendar payers are separate.",
                "Current access snapshot is evaluated at generation time even for an earlier --as-of; today's expiry count excludes people whose deadline has since been extended.",
                "Reminder stages describe preceding exposure, not causal uplift; immediate means no recorded reminder, not a latency threshold.",
                "Recorded source is not proof of incremental advertising effect. No estimated LTV or inferred advertising identifiers are used.",
            ],
        }
        self.stdout.write(json.dumps(metricToValue, ensure_ascii=False, indent=2))
