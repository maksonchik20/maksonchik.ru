import json
from datetime import datetime, timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from .models import UserTg, WhoUpdateOnboardingFunnel, WhoUpdatePaymentOrder, WhoUpdateUserAcquisition
from .subscriptions import OWNER_TELEGRAM_ID


def at(day, hour=12):
    return timezone.make_aware(datetime(2026, 10, day, hour))


class CohortReportTests(TestCase):
    def user(self, telegram_id, *, connected_at=None, source="yandex", stage="immediate"):
        user = UserTg.objects.create(user_id=telegram_id, chat_id=telegram_id, username=f"PRIVATE_{telegram_id}")
        started_at = connected_at - timedelta(minutes=5) if connected_at else at(1)
        funnel = WhoUpdateOnboardingFunnel.objects.create(
            user=user, telegram_started_at=started_at, connected_at=connected_at,
            landing_viewed_at=started_at - timedelta(minutes=1),
            utm_source=source, yclid=f"private-click-{telegram_id}" if source == "yandex" else "",
            connection_stage=stage if connected_at else "",
        )
        WhoUpdateUserAcquisition.objects.create(
            user=user, first_start_at=started_at, first_start_funnel=funnel,
            acquisition_funnel=funnel if source == "yandex" else None,
            first_connected_at=connected_at, first_connection_funnel=funnel if connected_at else None,
            has_connection_history=connected_at is not None,
        )
        return user

    def pay(self, user, paid_at, amount="129.00", *, is_test=False, status="paid"):
        return WhoUpdatePaymentOrder.objects.create(
            user=user, paid_at=paid_at, amount=Decimal(amount),
            plan="month", duration_days=30, status=status, is_test=is_test,
        )

    def report(self, *args):
        output = StringIO()
        with patch(
            "webhook_tg.management.commands.report_who_update_cohorts.timezone.now",
            return_value=timezone.make_aware(datetime(2026, 11, 2, 12)),
        ):
            call_command(
                "report_who_update_cohorts", "--since", "2026-10-01", "--until", "2026-11-01",
                "--as-of", "2026-11-01", *args, stdout=output,
            )
        return json.loads(output.getvalue())

    def test_mature_denominators_prepaid_repeat_and_unknown_history_are_separate(self):
        mature = self.user(101, connected_at=at(1), stage="after_first")
        self.pay(mature, at(5))
        self.pay(mature, at(20), "249.00")
        immature = self.user(102, connected_at=at(25))
        self.pay(immature, at(26))
        self.user(103, connected_at=at(10), source="referral", stage="after_second")
        prepaid = self.user(104, connected_at=at(1))
        self.pay(prepaid, at(1, 10), "800.00")
        legacy = self.user(105)
        WhoUpdateUserAcquisition.objects.filter(user=legacy).update(has_connection_history=True)
        UserTg.objects.create(user_id=106, chat_id=106)
        owner = self.user(OWNER_TELEGRAM_ID, connected_at=at(1))
        self.pay(owner, at(2), "1.00")
        self.pay(mature, at(3), "999.00", is_test=True)

        metricToValue = self.report()

        self.assertEqual(metricToValue["first_starts_in_period"], 5)
        self.assertEqual(metricToValue["first_connections_in_period"], 4)
        d14MetricToValue = metricToValue["connection_cohort"]["d14"]
        self.assertEqual(d14MetricToValue["mature_users"], 3)
        self.assertEqual(d14MetricToValue["immature_users"], 1)
        self.assertEqual(d14MetricToValue["already_paying_before_connection"], 1)
        self.assertEqual(d14MetricToValue["eligible_non_payers"], 2)
        self.assertEqual(d14MetricToValue["first_payers_within_days"], 1)
        self.assertEqual(d14MetricToValue["first_payment_conversion_percent"], 50.0)
        self.assertEqual(metricToValue["connection_cohort"]["d30"]["eligible_non_payers"], 1)
        self.assertEqual(metricToValue["connection_cohort"]["d30"]["first_payment_conversion_percent"], 100.0)
        self.assertEqual(metricToValue["calendar_period_cash"]["orders"], 4)
        self.assertEqual(metricToValue["calendar_period_cash"]["gross_rub"], "1307.00")
        self.assertEqual(metricToValue["calendar_period_cash"]["repeat_payers"], 1)
        self.assertEqual(metricToValue["data_quality_current"]["users_without_acquisition_backfill"], 1)
        self.assertEqual(metricToValue["data_quality_current"]["connected_history_without_known_first_connection_date"], 1)
        self.assertEqual(metricToValue["connection_stage_counts"]["after_first"], 1)
        self.assertEqual(metricToValue["connection_cohort_by_source"]["recorded_referral"]["users"], 1)

    def test_exclusive_window_and_nonpaid_undated_test_orders(self):
        user = self.user(101, connected_at=at(1))
        self.pay(user, at(15), "129.00")  # Exactly D14 is outside [0, 14).
        self.pay(user, None, "99.00")
        self.pay(user, at(3), "999.00", status="pending")
        self.pay(user, at(3), "999.00", is_test=True)
        self.pay(user, timezone.make_aware(datetime(2026, 11, 1)), "800.00")

        metricToValue = self.report()
        self.assertEqual(metricToValue["connection_cohort"]["d14"]["first_payers_within_days"], 0)
        self.assertEqual(metricToValue["connection_cohort"]["d30"]["first_payers_within_days"], 1)
        self.assertEqual(metricToValue["calendar_period_cash"]["orders"], 1)
        self.assertEqual(metricToValue["data_quality_current"]["paid_orders_without_payment_date"], 1)

    def test_report_is_read_only_and_contains_no_customer_identifiers(self):
        user = self.user(123456789, connected_at=at(1))
        self.pay(user, at(2))
        with CaptureQueriesContext(connection) as queries:
            metricToValue = self.report()
        modifying = [fieldToQueryValue["sql"] for fieldToQueryValue in queries if fieldToQueryValue["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
        self.assertEqual(modifying, [])
        serialized = json.dumps(metricToValue)
        self.assertNotIn("PRIVATE_", serialized)
        self.assertNotIn("123456789", serialized)
        self.assertNotIn("private-click-", serialized)

    def test_current_expiry_snapshot_is_not_reported_as_historical_churn(self):
        user = self.user(101, connected_at=at(1))
        user.access_unlimited = False
        user.business_is_connected = True
        user.access_expires_at = timezone.make_aware(datetime(2026, 11, 2, 10))
        user.save()
        metricToValue = self.report()
        self.assertEqual(metricToValue["access_snapshot_now_not_historical"]["expired_and_connected_users"], 1)
        self.assertEqual(metricToValue["access_snapshot_now_not_historical"]["expiry_still_set_to_today_users"], 1)
        self.assertNotIn("renewal_rate", metricToValue)

    def test_empty_denominator_has_no_made_up_conversion(self):
        self.user(101, connected_at=at(31))
        metricToValue = self.report()
        self.assertIsNone(metricToValue["connection_cohort"]["d14"]["first_payment_conversion_percent"])

    def test_later_ad_visit_cannot_relabel_earlier_connection(self):
        user = self.user(101, connected_at=at(1))
        source = WhoUpdateOnboardingFunnel.objects.create(
            user=user, utm_source="yandex", yclid="later-click", landing_viewed_at=at(10),
            telegram_started_at=at(10) + timedelta(minutes=1),
        )
        WhoUpdateUserAcquisition.objects.filter(user=user).update(acquisition_funnel=source)
        metricToValue = self.report()
        self.assertEqual(metricToValue["connection_cohort_by_source"]["unattributed"]["users"], 1)

    def test_invalid_dates_are_rejected(self):
        with self.assertRaises(CommandError):
            self.report("--since", "2026-11-02")
        with self.assertRaises(CommandError):
            self.report("--as-of", "invalid")
