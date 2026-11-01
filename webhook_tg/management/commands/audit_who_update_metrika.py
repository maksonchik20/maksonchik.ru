"""Read-only payment/measurement reconciliation; never exports or fulfils orders."""
import json
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, F, Q
from django.utils import timezone

from webhook_tg.metrika_offline import _purchase_funnel_for
from webhook_tg.models import WhoUpdateMetrikaConversion, WhoUpdatePaymentOrder
from webhook_tg.subscriptions import OWNER_TELEGRAM_ID


class Command(BaseCommand):
    help = "Read-only JSON reconciliation of real WhoUpdate payments and offline goals (no API calls)"

    def add_arguments(self, parser):
        parser.add_argument("--since", help="First local calendar date, YYYY-MM-DD; default: 30 days ago")
        parser.add_argument("--until", help="Exclusive local calendar date, YYYY-MM-DD; default: now")

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

        since = boundary(options["since"], now - timedelta(days=30))
        until = boundary(options["until"], now)
        if since >= until:
            raise CommandError("--since must be before --until")
        orders = WhoUpdatePaymentOrder.objects.filter(
            status=WhoUpdatePaymentOrder.Status.PAID,
            is_test=False,
            paid_at__gte=since,
            paid_at__lt=until,
        ).select_related("metrika_funnel", "metrika_conversion", "user").order_by("paid_at", "id")
        reasonToTotals = {}
        total = Decimal("0")
        order_count = 0
        user_ids = set()
        owner_order_count = 0
        owner_amount = Decimal("0")
        for order in orders.iterator(chunk_size=200):
            order_count += 1
            total += order.amount
            user_ids.add(order.user_id)
            if order.user.user_id == OWNER_TELEGRAM_ID:
                owner_order_count += 1
                owner_amount += order.amount
            try:
                conversion = order.metrika_conversion
            except WhoUpdateMetrikaConversion.DoesNotExist:
                conversion = None
            if conversion is not None:
                reason = f"queued_{conversion.status}"
            elif order.paid_at < now - timedelta(days=20):
                reason = "missing_outside_automatic_backfill_window"
            elif _purchase_funnel_for(order, now=now) is not None:
                reason = "missing_eligible_for_queue"
            else:
                # Absence of identifiers can mean organic traffic, privacy
                # settings or missing instrumentation; do not label it organic.
                reason = "missing_no_eligible_visit_identifiers"
            if reason not in reasonToTotals:
                reasonToTotals[reason] = {"orders": 0, "gross_rub": Decimal("0")}
            reasonToTotals[reason]["orders"] += 1
            reasonToTotals[reason]["gross_rub"] += order.amount
        onboarding = WhoUpdateMetrikaConversion.objects.filter(
            event_type__in=("start", "connected"),
            occurred_at__gte=since,
            occurred_at__lt=until,
        )
        onboardingStatusToCount = dict(
            onboarding.values_list("status").annotate(count=Count("id")).order_by("status")
        )
        recoverable = onboarding.filter(
            Q(funnel__metrika_counter_id__isnull=True) | Q(funnel__metrika_counter_id=F("counter_id")),
            status="failed", api_status="LINKAGE_FAILURE", api_upload_id__isnull=False,
            identifier_type="yclid", identifier=F("funnel__yclid"), client_id="",
            funnel__metrika_client_id__gt="",
            funnel__landing_viewed_at__gte=now - timedelta(days=20),
            funnel__landing_viewed_at__lte=F("occurred_at"),
            occurred_at__lte=now,
        ).count()
        self.stdout.write(json.dumps({
            "since_inclusive": since.isoformat(),
            "until_exclusive": until.isoformat(),
            "as_of": now.isoformat(),
            "orders": order_count,
            "distinct_payers": len(user_ids),
            "gross_rub": total,
            "owner_orders_included": owner_order_count,
            "owner_gross_rub_included": owner_amount,
            "reasonToTotals": reasonToTotals,
            "onboardingStatusToCount": onboardingStatusToCount,
            "onboarding_recovery_candidates_pending_remote_confirmation": recoverable,
            "notes": [
                "Read-only: no conversion creation, upload, payment fulfilment or notifications.",
                "Only local PAID non-test orders with paid_at; refunds require provider reconciliation.",
                "PROCESSED is an upload status, not a guarantee of Direct attribution or credited revenue.",
                "Missing identifiers do not prove organic traffic; no advertising source is invented.",
                "Recovery candidate count is local only; remote LINKAGE_FAILURE must be confirmed before replay.",
            ],
        }, default=str, ensure_ascii=False, indent=2))
