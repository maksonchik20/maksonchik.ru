import csv
import io
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

import requests
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from .metrika_offline import (
    _csv_payload,
    queue_purchase_conversion,
    recover_failed_purchase_conversions,
    sync_conversion_queue,
    sync_purchase_conversions,
    upload_pending_conversions,
)
from .models import (
    UserTg,
    WhoUpdateMetrikaConversion,
    WhoUpdateOnboardingFunnel,
    WhoUpdatePaymentOrder,
)


class PurchaseAttributionTests(TestCase):
    counter_id = 112093587

    def setUp(self):
        self.now = timezone.now().replace(microsecond=0)
        self.paid_at = self.now - timedelta(hours=2)
        self.user = UserTg.objects.create(user_id=9911001, chat_id=9911001)
        self.funnel = WhoUpdateOnboardingFunnel.objects.create(
            user=self.user,
            metrika_counter_id=self.counter_id,
            metrika_client_id="1780000000111222333",
            yclid="1780000000444555666",
            landing_viewed_at=self.now - timedelta(days=2),
            telegram_started_at=self.now - timedelta(days=2) + timedelta(minutes=1),
        )
        self.post = self.enterContext(patch("webhook_tg.metrika_offline.METRIKA_SESSION.post"))
        self.get = self.enterContext(patch("webhook_tg.metrika_offline.METRIKA_SESSION.get"))
        self.enterContext(patch("webhook_tg.metrika_offline.observe_metric"))
        self.post.side_effect = AssertionError("Unexpected upload")
        self.get.side_effect = AssertionError("Unexpected status request")

    def make_order(self, **fieldToOverride):
        fieldToValue = {
            "user": self.user,
            "plan": WhoUpdatePaymentOrder.Plan.MONTH,
            "duration_days": 30,
            "amount": Decimal("99.00"),
            "status": WhoUpdatePaymentOrder.Status.PAID,
            "paid_at": self.paid_at,
        }
        fieldToValue.update(fieldToOverride)
        return WhoUpdatePaymentOrder.objects.create(**fieldToValue)

    @staticmethod
    def response(status="UPLOADED", upload_id=7711):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"uploading": {"id": upload_id, "status": status}}
        return response

    def make_legacy_failure(self, **fieldToOverride):
        order = self.make_order()
        fieldToValue = {
            "funnel": self.funnel,
            "payment_order": order,
            "event_type": WhoUpdateMetrikaConversion.EventType.PURCHASE,
            "target": "who_update_purchase",
            "counter_id": self.counter_id,
            "occurred_at": order.paid_at,
            "identifier_type": WhoUpdateMetrikaConversion.IdentifierType.YCLID,
            "identifier": self.funnel.yclid,
            "value": order.amount,
            "currency": "RUB",
            "status": WhoUpdateMetrikaConversion.Status.FAILED,
            "api_status": "LINKAGE_FAILURE",
            "api_upload_id": 7711,
            "attempts": 1,
            "processed_at": self.now - timedelta(hours=1),
        }
        fieldToValue.update(fieldToOverride)
        return WhoUpdateMetrikaConversion.objects.create(**fieldToValue)

    def test_queue_accepts_only_real_paid_orders_with_payment_time(self):
        for status in (
            WhoUpdatePaymentOrder.Status.PENDING,
            WhoUpdatePaymentOrder.Status.CANCELED,
            WhoUpdatePaymentOrder.Status.FAILED,
        ):
            with self.subTest(status=status):
                self.assertIsNone(queue_purchase_conversion(self.make_order(status=status)))
        self.assertIsNone(queue_purchase_conversion(self.make_order(is_test=True)))
        self.assertIsNone(queue_purchase_conversion(self.make_order(paid_at=None)))
        self.assertEqual(WhoUpdateMetrikaConversion.objects.count(), 0)

    def test_both_identifiers_are_sent_in_one_row_with_actual_paid_time_and_revenue(self):
        order = self.make_order(amount=Decimal("224.10"))
        conversion = queue_purchase_conversion(order, occurred_at=self.now)

        csv_kind, payload = _csv_payload([conversion])
        rows = list(csv.DictReader(io.StringIO(payload.decode("utf-8"))))

        self.assertEqual(csv_kind, "yclid")
        self.assertEqual(len(rows), 1)
        fieldToValue = rows[0]
        self.assertEqual(fieldToValue, {
            "Yclid": self.funnel.yclid,
            "ClientId": self.funnel.metrika_client_id,
            "Target": "who_update_purchase",
            "DateTime": str(int(order.paid_at.timestamp())),
            "Price": "224.10",
            "Currency": "RUB",
        })
        self.assertEqual(conversion.occurred_at, order.paid_at)

    def test_checkout_is_fallback_without_acquisition_and_keeps_its_counter(self):
        self.funnel.delete()
        checkout = WhoUpdateOnboardingFunnel.objects.create(
            user=self.user,
            metrika_client_id="1780000000777888999",
            metrika_counter_id=111680333,
            landing_viewed_at=self.paid_at - timedelta(minutes=1),
        )
        order = self.make_order(metrika_funnel=checkout)

        conversion = queue_purchase_conversion(order, counter_id=self.counter_id)

        self.assertEqual(conversion.funnel, checkout)
        self.assertEqual(conversion.counter_id, checkout.metrika_counter_id)
        self.assertEqual(conversion.identifier_type, WhoUpdateMetrikaConversion.IdentifierType.CLIENT_ID)
        self.assertEqual(conversion.identifier, checkout.metrika_client_id)
        _, payload = _csv_payload([conversion])
        self.assertEqual(payload.decode("utf-8").splitlines()[0], "ClientId,Target,DateTime,Price,Currency")

    def test_queue_is_idempotent_for_the_same_order(self):
        order = self.make_order()
        conversion = queue_purchase_conversion(order)

        repeated = queue_purchase_conversion(order, occurred_at=self.now, counter_id=111680333)

        self.assertEqual(repeated.pk, conversion.pk)
        self.assertEqual(WhoUpdateMetrikaConversion.objects.count(), 1)
        self.assertEqual(repeated.occurred_at, order.paid_at)
        self.assertEqual(repeated.counter_id, self.counter_id)

    def test_late_client_id_recovers_missing_queue_without_replaying_payment(self):
        self.funnel.delete()
        checkout = WhoUpdateOnboardingFunnel.objects.create(
            user=self.user,
            landing_viewed_at=self.paid_at - timedelta(minutes=1),
            metrika_counter_id=self.counter_id,
        )
        order = self.make_order(metrika_funnel=checkout)
        self.assertIsNone(queue_purchase_conversion(order))
        checkout.metrika_client_id = "1780000000777888999"
        checkout.save(update_fields=["metrika_client_id"])

        self.assertEqual(sync_purchase_conversions(counter_id=self.counter_id, now=self.now), 1)
        self.assertEqual(sync_purchase_conversions(counter_id=self.counter_id, now=self.now), 0)

        conversion = WhoUpdateMetrikaConversion.objects.get(payment_order=order)
        self.assertEqual(conversion.occurred_at, order.paid_at)
        self.assertEqual(conversion.identifier, checkout.metrika_client_id)
        order.refresh_from_db()
        self.assertEqual(order.status, WhoUpdatePaymentOrder.Status.PAID)
        self.assertEqual(order.paid_at, self.paid_at)
        self.post.assert_not_called()
        self.get.assert_not_called()

    def test_unbound_after_payment_and_old_funnels_are_not_used(self):
        self.funnel.delete()
        WhoUpdateOnboardingFunnel.objects.create(
            user=None,
            yclid="unbound",
            landing_viewed_at=self.paid_at - timedelta(minutes=1),
            telegram_started_at=self.paid_at - timedelta(minutes=1),
        )
        WhoUpdateOnboardingFunnel.objects.create(
            user=self.user,
            metrika_client_id="after-payment",
            landing_viewed_at=self.paid_at + timedelta(minutes=1),
            telegram_started_at=self.paid_at + timedelta(minutes=1),
        )
        WhoUpdateOnboardingFunnel.objects.create(
            user=self.user,
            metrika_client_id="too-old",
            landing_viewed_at=self.paid_at - timedelta(days=22),
            telegram_started_at=self.paid_at - timedelta(days=22),
        )

        self.assertIsNone(queue_purchase_conversion(self.make_order()))
        self.assertEqual(WhoUpdateMetrikaConversion.objects.count(), 0)

    def test_late_checkout_client_does_not_replace_eligible_ad_identifiers(self):
        for attempts in (0, 1):
            with self.subTest(attempts=attempts):
                checkout = WhoUpdateOnboardingFunnel.objects.create(
                    user=self.user,
                    landing_viewed_at=self.paid_at - timedelta(minutes=1),
                    metrika_counter_id=self.counter_id,
                )
                order = self.make_order(metrika_funnel=checkout)
                conversion = queue_purchase_conversion(order)
                self.assertEqual(conversion.funnel, self.funnel)
                conversion.attempts = attempts
                conversion.save(update_fields=["attempts"])
                original_payload = _csv_payload([conversion])
                checkout.metrika_client_id = "1780000000777888999"
                checkout.save(update_fields=["metrika_client_id"])

                sync_purchase_conversions(counter_id=self.counter_id, now=self.now)

                conversion.refresh_from_db()
                self.assertEqual(conversion.funnel, self.funnel)
                self.assertEqual(_csv_payload([conversion]), original_payload)
                self.assertEqual(conversion.occurred_at, order.paid_at)
                self.assertEqual(conversion.value, order.amount)

    def test_cross_browser_checkout_keeps_original_ad_pair_and_counter(self):
        checkout = WhoUpdateOnboardingFunnel.objects.create(
            user=self.user, landing_path="/bot/checkout/",
            landing_viewed_at=self.paid_at - timedelta(minutes=1),
            metrika_counter_id=111680333, metrika_client_id="2222222222222222222",
        )

        conversion = queue_purchase_conversion(self.make_order(metrika_funnel=checkout))
        fieldToValue = next(csv.DictReader(io.StringIO(_csv_payload([conversion])[1].decode())))

        self.assertEqual(conversion.funnel, self.funnel)
        self.assertEqual(conversion.counter_id, self.counter_id)
        self.assertEqual(fieldToValue["Yclid"], self.funnel.yclid)
        self.assertEqual(fieldToValue["ClientId"], self.funnel.metrika_client_id)
        self.assertNotIn(checkout.metrika_client_id, fieldToValue.values())

    def test_expired_at_upload_acquisition_uses_fresh_checkout(self):
        self.funnel.landing_viewed_at = self.now - timedelta(days=21, hours=1)
        self.funnel.telegram_started_at = self.funnel.landing_viewed_at
        self.funnel.save(update_fields=["landing_viewed_at", "telegram_started_at"])
        checkout = WhoUpdateOnboardingFunnel.objects.create(
            user=self.user, landing_path="/bot/checkout/",
            landing_viewed_at=self.paid_at - timedelta(minutes=1),
            metrika_counter_id=self.counter_id, metrika_client_id="2222222222222222222",
        )

        conversion = queue_purchase_conversion(self.make_order(metrika_funnel=checkout), now=self.now)

        self.assertEqual(conversion.funnel, checkout)
        self.assertEqual(conversion.identifier, checkout.metrika_client_id)

    def test_attempted_purchase_never_switches_when_original_visit_expires(self):
        checkout = WhoUpdateOnboardingFunnel.objects.create(
            user=self.user, landing_path="/bot/checkout/",
            landing_viewed_at=self.paid_at - timedelta(minutes=1),
            metrika_client_id="2222222222222222222", metrika_counter_id=self.counter_id,
        )
        conversion = queue_purchase_conversion(self.make_order(metrika_funnel=checkout))
        conversion.attempts = 1
        conversion.save(update_fields=["attempts"])
        original_payload = _csv_payload([conversion])

        sync_purchase_conversions(counter_id=self.counter_id, now=self.now + timedelta(days=20))

        conversion.refresh_from_db()
        self.assertEqual(_csv_payload([conversion]), original_payload)

    def test_process_death_after_post_keeps_retry_payload_identical(self):
        self.funnel.metrika_client_id = ""
        self.funnel.save(update_fields=["metrika_client_id"])
        conversion = queue_purchase_conversion(self.make_order())
        self.post.side_effect = SystemExit("simulated process death after provider acceptance")

        with self.assertRaises(SystemExit):
            upload_pending_conversions(token="test", now=self.now)

        original_payload = self.post.call_args.kwargs["files"]["file"][1]
        conversion.refresh_from_db()
        self.assertEqual(conversion.attempts, 1)
        self.assertGreater(conversion.next_attempt_at, self.now)
        self.assertIn("acknowledgement not saved", conversion.last_error)
        self.funnel.metrika_client_id = "late-client"
        self.funnel.save(update_fields=["metrika_client_id"])
        sync_purchase_conversions(counter_id=self.counter_id, now=self.now)
        self.assertEqual(upload_pending_conversions(token="test", now=self.now), 0)
        self.post.side_effect = None
        self.post.return_value = self.response()

        self.assertEqual(upload_pending_conversions(token="test", now=self.now + timedelta(minutes=6)), 1)

        self.assertEqual(self.post.call_args.kwargs["files"]["file"][1], original_payload)
        conversion.refresh_from_db()
        self.assertEqual(conversion.attempts, 2)

    def test_checkout_funnel_for_another_user_is_not_used(self):
        other_user = UserTg.objects.create(user_id=9911002, chat_id=9911002)
        self.funnel.user = other_user
        self.funnel.save(update_fields=["user"])

        self.assertIsNone(queue_purchase_conversion(self.make_order(metrika_funnel=self.funnel)))

    def test_client_id_is_enriched_before_first_attempt_only(self):
        self.funnel.metrika_client_id = ""
        self.funnel.save(update_fields=["metrika_client_id"])
        conversion = queue_purchase_conversion(self.make_order())
        self.funnel.metrika_client_id = "late-client"
        self.funnel.save(update_fields=["metrika_client_id"])

        sync_purchase_conversions(counter_id=self.counter_id, now=self.now)

        conversion.refresh_from_db()
        self.assertEqual(conversion.client_id, "late-client")
        self.assertEqual(conversion.attempts, 0)

    def test_pending_legacy_funnel_keeps_original_counter_when_provenance_unknown(self):
        self.funnel.metrika_counter_id = None
        self.funnel.save(update_fields=["metrika_counter_id"])
        conversion = queue_purchase_conversion(self.make_order(), counter_id=111680333)

        sync_purchase_conversions(counter_id=self.counter_id, now=self.now)

        conversion.refresh_from_db()
        self.assertEqual(conversion.counter_id, 111680333)

    def test_timeout_retry_keeps_identical_payload_after_late_client_id(self):
        self.funnel.metrika_client_id = ""
        self.funnel.save(update_fields=["metrika_client_id"])
        conversion = queue_purchase_conversion(self.make_order())
        self.post.side_effect = requests.Timeout("uncertain acceptance")
        self.assertEqual(upload_pending_conversions(token="test", now=self.now), 0)
        first_payload = self.post.call_args.kwargs["files"]["file"][1]
        self.funnel.metrika_client_id = "late-client"
        self.funnel.save(update_fields=["metrika_client_id"])
        sync_purchase_conversions(counter_id=self.counter_id, now=self.now)
        conversion.refresh_from_db()
        self.assertEqual(conversion.client_id, "")
        self.assertEqual(conversion.attempts, 1)
        self.post.side_effect = None
        self.post.return_value = self.response()

        self.assertEqual(upload_pending_conversions(token="test", now=self.now + timedelta(hours=1)), 1)

        self.assertEqual(self.post.call_args.kwargs["files"]["file"][1], first_payload)

    def test_purchases_have_separate_uploads_from_each_other_and_from_start(self):
        first = queue_purchase_conversion(self.make_order())
        second = queue_purchase_conversion(self.make_order(amount=Decimal("249.00")))
        sync_conversion_queue(counter_id=self.counter_id, now=self.now)
        self.post.side_effect = [self.response(upload_id=8101), self.response(upload_id=8102), self.response(upload_id=8103)]

        self.assertEqual(upload_pending_conversions(token="test", now=self.now), 3)

        self.assertEqual(self.post.call_count, 3)
        for call in self.post.call_args_list:
            rows = list(csv.DictReader(io.StringIO(call.kwargs["files"]["file"][1].decode("utf-8"))))
            self.assertEqual(len(rows), 1)
        first.refresh_from_db()
        second.refresh_from_db()
        start = WhoUpdateMetrikaConversion.objects.get(event_type=WhoUpdateMetrikaConversion.EventType.START)
        self.assertEqual(len({first.api_upload_id, second.api_upload_id, start.api_upload_id}), 3)

    def test_recovery_rechecks_remote_failure_and_preserves_payment_and_history(self):
        previous_history = [{"id": 7000, "status": "LINKAGE_FAILURE"}]
        conversion = self.make_legacy_failure(upload_history=previous_history)
        original_time = conversion.occurred_at
        self.get.side_effect = None
        self.get.return_value = self.response("LINKAGE_FAILURE")

        self.assertEqual(recover_failed_purchase_conversions(token="test", now=self.now), 1)
        self.assertEqual(recover_failed_purchase_conversions(token="test", now=self.now), 0)

        self.get.assert_called_once()
        self.assertIn(f"/{self.counter_id}/offline_conversions/uploading/7711", self.get.call_args.args[0])
        conversion.refresh_from_db()
        self.assertEqual(conversion.client_id, self.funnel.metrika_client_id)
        self.assertEqual(conversion.occurred_at, original_time)
        self.assertEqual(conversion.value, Decimal("99.00"))
        self.assertEqual(conversion.status, WhoUpdateMetrikaConversion.Status.PENDING)
        self.assertEqual(conversion.next_attempt_at, self.now)
        self.assertIsNone(conversion.api_upload_id)
        self.assertIsNone(conversion.processed_at)
        self.assertEqual(conversion.upload_history[:-1], previous_history)
        self.assertEqual(conversion.upload_history[-1], {
            "id": 7711, "status": "LINKAGE_FAILURE", "action": "add_client_id", "at": self.now.isoformat(),
        })
        self.post.assert_not_called()

    def test_recovery_does_not_replay_processed_or_ambiguous_remote_upload(self):
        conversion = self.make_legacy_failure()
        self.get.side_effect = None
        for status in ("PROCESSED", "MATCHED", "UPLOADED", "EXPORTED", "PREPARED"):
            with self.subTest(status=status):
                self.get.return_value = self.response(status)
                self.assertEqual(recover_failed_purchase_conversions(token="test", now=self.now), 0)
                conversion.refresh_from_db()
                self.assertEqual(conversion.client_id, "")
                self.assertEqual(conversion.api_upload_id, 7711)
                self.assertEqual(conversion.upload_history, [])
        self.post.assert_not_called()

    def test_recovery_does_not_replay_after_status_request_timeout_or_malformed_response(self):
        conversion = self.make_legacy_failure()
        self.get.side_effect = requests.Timeout("status unknown")
        self.assertEqual(recover_failed_purchase_conversions(token="test", now=self.now), 0)
        self.get.side_effect = None
        response = self.response()
        response.json.return_value = {"uploading": {}}
        self.get.return_value = response
        self.assertEqual(recover_failed_purchase_conversions(token="test", now=self.now), 0)
        conversion.refresh_from_db()
        self.assertEqual(conversion.status, WhoUpdateMetrikaConversion.Status.FAILED)
        self.assertEqual(conversion.api_upload_id, 7711)

    def test_recovery_excludes_local_processed_pending_and_already_enriched_rows(self):
        for status in (
            WhoUpdateMetrikaConversion.Status.PROCESSED,
            WhoUpdateMetrikaConversion.Status.SUBMITTED,
            WhoUpdateMetrikaConversion.Status.PENDING,
        ):
            self.make_legacy_failure(status=status)
        self.make_legacy_failure(client_id=self.funnel.metrika_client_id)
        self.make_legacy_failure(api_upload_id=None)

        self.assertEqual(recover_failed_purchase_conversions(token="test", now=self.now), 0)

        self.get.assert_not_called()
        self.post.assert_not_called()

    def test_recovery_excludes_missing_client_id(self):
        self.make_legacy_failure()
        self.funnel.metrika_client_id = ""
        self.funnel.save(update_fields=["metrika_client_id"])

        self.assertEqual(recover_failed_purchase_conversions(token="test", now=self.now), 0)
        self.get.assert_not_called()

    def test_recovery_excludes_expired_visit_even_when_purchase_is_recent(self):
        conversion = self.make_legacy_failure()
        self.funnel.landing_viewed_at = self.now - timedelta(days=22)
        self.funnel.save(update_fields=["landing_viewed_at"])

        self.assertEqual(recover_failed_purchase_conversions(token="test", now=self.now), 0)

        self.get.assert_not_called()
        conversion.refresh_from_db()
        self.assertEqual(conversion.occurred_at, self.paid_at)
        self.assertEqual(conversion.client_id, "")

    def test_recovery_does_not_mix_counters_or_override_original_payment_time(self):
        first = self.make_legacy_failure(counter_id=111680333)
        second = self.make_legacy_failure(occurred_at=self.paid_at + timedelta(seconds=1))
        self.get.side_effect = None
        self.get.return_value = self.response("LINKAGE_FAILURE")

        self.assertEqual(recover_failed_purchase_conversions(token="test", now=self.now), 0)

        for conversion in (first, second):
            conversion.refresh_from_db()
            self.assertEqual(conversion.status, WhoUpdateMetrikaConversion.Status.FAILED)
            self.assertEqual(conversion.client_id, "")


class UploadAttemptCommitTests(TransactionTestCase):
    def setUp(self):
        self.now = timezone.now()
        user = UserTg.objects.create(user_id=9911991, chat_id=9911991)
        WhoUpdateOnboardingFunnel.objects.create(
            user=user, yclid="11111111111111", metrika_client_id="1111111111111111111",
            landing_viewed_at=self.now - timedelta(days=1),
            telegram_started_at=self.now - timedelta(days=1),
        )
        order = WhoUpdatePaymentOrder.objects.create(
            user=user, plan="month", duration_days=30, amount=Decimal("99.00"),
            status="paid", paid_at=self.now - timedelta(hours=1),
        )
        self.conversion = queue_purchase_conversion(order)

    @patch("webhook_tg.metrika_offline.METRIKA_SESSION.post")
    def test_attempt_is_committed_before_network_request(self, post):
        def accepted_then_killed(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            self.conversion.refresh_from_db()
            self.assertEqual(self.conversion.attempts, 1)
            raise SystemExit("killed")
        post.side_effect = accepted_then_killed

        with self.assertRaises(SystemExit):
            upload_pending_conversions(token="test", now=self.now)

        self.conversion.refresh_from_db()
        self.assertEqual(self.conversion.attempts, 1)
        self.assertGreater(self.conversion.next_attempt_at, self.now)

    @patch("webhook_tg.metrika_offline.METRIKA_SESSION.post")
    def test_upload_refuses_outer_transaction_that_could_rollback_marker(self, post):
        with transaction.atomic():
            with self.assertRaises(RuntimeError):
                upload_pending_conversions(token="test", now=self.now)
        post.assert_not_called()
        self.conversion.refresh_from_db()
        self.assertEqual(self.conversion.attempts, 0)
