import csv
import io
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

import requests
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from .metrika_offline import (
    _csv_payload,
    reconcile_failed_conversion_statuses,
    recover_failed_onboarding_conversions,
    recover_onboarding_linkage,
    sync_conversion_queue,
    upload_pending_conversions,
)
from .models import (
    UserTg,
    WhoUpdateMetrikaConversion,
    WhoUpdateOnboardingFunnel,
    WhoUpdatePaymentOrder,
    WhoUpdateUserAcquisition,
)


class OnboardingMetrikaRepairTests(TestCase):
    old_counter = 111680333
    current_counter = 112093587

    def setUp(self):
        self.now = timezone.now().replace(microsecond=0)
        self.user = UserTg.objects.create(user_id=9918001, chat_id=9918001)
        self.source = WhoUpdateOnboardingFunnel.objects.create(
            user=self.user,
            yclid="ad-click-source",
            metrika_client_id="same-visit-client",
            metrika_counter_id=self.old_counter,
            landing_viewed_at=self.now - timedelta(days=2),
            telegram_started_at=self.now - timedelta(days=2) + timedelta(minutes=1),
        )
        WhoUpdateUserAcquisition.objects.create(
            user=self.user,
            first_start_at=self.source.telegram_started_at,
            first_start_funnel=self.source,
            acquisition_funnel=self.source,
        )
        self.enterContext(patch("webhook_tg.metrika_offline.observe_metric"))
        self.get = self.enterContext(patch("webhook_tg.metrika_offline.METRIKA_SESSION.get"))
        self.post = self.enterContext(patch("webhook_tg.metrika_offline.METRIKA_SESSION.post"))
        self.get.side_effect = AssertionError("Unexpected Metrika status request")
        self.post.side_effect = AssertionError("Unexpected Metrika upload")

    @staticmethod
    def response(status="UPLOADED", upload_id=88001):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"uploading": {"id": upload_id, "status": status}}
        return response

    def sync(self):
        return sync_conversion_queue(counter_id=self.current_counter, now=self.now)

    def legacy_failure(self, **fieldToOverride):
        fieldToValue = {
            "funnel": self.source,
            "event_type": WhoUpdateMetrikaConversion.EventType.START,
            "target": "who_update_start",
            "counter_id": self.old_counter,
            "occurred_at": self.source.telegram_started_at,
            "identifier_type": WhoUpdateMetrikaConversion.IdentifierType.YCLID,
            "identifier": self.source.yclid,
            "status": WhoUpdateMetrikaConversion.Status.FAILED,
            "api_upload_id": 88001,
            "api_status": "LINKAGE_FAILURE",
            "attempts": 1,
        }
        fieldToValue.update(fieldToOverride)
        return WhoUpdateMetrikaConversion.objects.create(**fieldToValue)

    def test_start_and_connected_use_one_source_pair_and_original_counter(self):
        attempt = WhoUpdateOnboardingFunnel.objects.create(
            user=self.user,
            utm_source="telegram",
            telegram_started_at=self.now - timedelta(minutes=20),
            connected_at=self.now - timedelta(minutes=10),
        )

        self.assertEqual(self.sync(), 3)

        conversions = list(WhoUpdateMetrikaConversion.objects.filter(funnel=attempt))
        self.assertEqual(len(conversions), 2)
        for conversion in conversions:
            self.assertEqual(conversion.counter_id, self.old_counter)
            self.assertEqual(conversion.identifier, self.source.yclid)
            self.assertEqual(conversion.client_id, self.source.metrika_client_id)
            self.assertEqual(conversion.funnel_id, attempt.pk)
        fieldToValue = next(csv.DictReader(io.StringIO(_csv_payload(conversions)[1].decode())))
        self.assertEqual(fieldToValue["Yclid"], "ad-click-source")
        self.assertEqual(fieldToValue["ClientId"], "same-visit-client")
        attempt.refresh_from_db()
        self.assertEqual(attempt.yclid, "")
        self.assertEqual(attempt.metrika_client_id, "")

    def test_fresh_own_visit_keeps_its_pair_instead_of_mixing_acquisition(self):
        attempt = WhoUpdateOnboardingFunnel.objects.create(
            user=self.user,
            yclid="second-click",
            metrika_client_id="second-client",
            metrika_counter_id=self.current_counter,
            landing_viewed_at=self.now - timedelta(minutes=30),
            telegram_started_at=self.now - timedelta(minutes=20),
        )

        self.sync()

        conversion = WhoUpdateMetrikaConversion.objects.get(funnel=attempt)
        self.assertEqual(conversion.identifier, "second-click")
        self.assertEqual(conversion.client_id, "second-client")
        self.assertEqual(conversion.counter_id, self.current_counter)

    def test_pending_unattempted_legacy_payload_is_enriched_and_counter_corrected(self):
        conversion = self.legacy_failure(
            status=WhoUpdateMetrikaConversion.Status.PENDING,
            attempts=0, api_upload_id=None, api_status="", counter_id=self.current_counter,
        )

        self.assertEqual(self.sync(), 0)

        conversion.refresh_from_db()
        self.assertEqual(conversion.client_id, self.source.metrika_client_id)
        self.assertEqual(conversion.counter_id, self.old_counter)

    def test_unknown_counter_on_legacy_pending_payload_is_preserved(self):
        self.source.metrika_counter_id = None
        self.source.save(update_fields=["metrika_counter_id"])
        conversion = self.legacy_failure(
            status=WhoUpdateMetrikaConversion.Status.PENDING, attempts=0,
            api_upload_id=None, api_status="",
        )

        self.sync()

        conversion.refresh_from_db()
        self.assertEqual(conversion.counter_id, self.old_counter)

    def test_attempted_submitted_and_processed_payloads_are_never_rewritten(self):
        for status in (
            WhoUpdateMetrikaConversion.Status.PENDING,
            WhoUpdateMetrikaConversion.Status.SUBMITTED,
            WhoUpdateMetrikaConversion.Status.PROCESSED,
            WhoUpdateMetrikaConversion.Status.FAILED,
        ):
            with self.subTest(status=status):
                conversion = self.legacy_failure(status=status)
                original = _csv_payload([conversion])

                self.sync()

                conversion.refresh_from_db()
                self.assertEqual(_csv_payload([conversion]), original)
                self.assertEqual(conversion.attempts, 1)
                conversion.delete()

    def test_organic_and_referral_without_any_real_visit_are_not_invented(self):
        self.source.delete()
        for source in ("telegram", "referral"):
            WhoUpdateOnboardingFunnel.objects.create(
                user=self.user, utm_source=source,
                telegram_started_at=self.now - timedelta(minutes=5),
                connected_at=self.now - timedelta(minutes=3),
            )

        self.assertEqual(self.sync(), 0)
        self.assertEqual(WhoUpdateMetrikaConversion.objects.count(), 0)

    def test_future_event_is_not_backfilled(self):
        self.source.telegram_started_at = self.now + timedelta(hours=1)
        self.source.save(update_fields=["telegram_started_at"])

        self.assertEqual(self.sync(), 0)

    def test_expired_acquisition_does_not_queue_recent_start_or_connection(self):
        self.source.landing_viewed_at = self.now - timedelta(days=30)
        self.source.telegram_started_at = self.source.landing_viewed_at + timedelta(minutes=1)
        self.source.save(update_fields=["landing_viewed_at", "telegram_started_at"])
        WhoUpdateOnboardingFunnel.objects.create(
            user=self.user, utm_source="telegram",
            telegram_started_at=self.now - timedelta(hours=1),
            connected_at=self.now - timedelta(minutes=30),
        )

        self.assertEqual(self.sync(), 0)
        self.assertEqual(WhoUpdateMetrikaConversion.objects.count(), 0)

    def test_own_visit_window_is_measured_from_upload_time(self):
        # The visit was only 19 days old at /start, but is expired at upload.
        self.source.landing_viewed_at = self.now - timedelta(days=21, seconds=1)
        self.source.save(update_fields=["landing_viewed_at"])

        self.assertEqual(self.sync(), 0)

        self.source.landing_viewed_at = self.now - timedelta(days=21)
        self.source.save(update_fields=["landing_viewed_at"])
        self.assertEqual(self.sync(), 1)
        self.assertEqual(self.sync(), 0)

    def test_fresh_own_visit_is_eligible_when_first_acquisition_expired(self):
        self.source.landing_viewed_at = self.now - timedelta(days=30)
        self.source.telegram_started_at = self.source.landing_viewed_at + timedelta(minutes=1)
        self.source.save(update_fields=["landing_viewed_at", "telegram_started_at"])
        attempt = WhoUpdateOnboardingFunnel.objects.create(
            user=self.user, yclid="fresh-click", metrika_client_id="fresh-client",
            metrika_counter_id=self.current_counter,
            landing_viewed_at=self.now - timedelta(hours=2),
            telegram_started_at=self.now - timedelta(hours=1),
            connected_at=self.now - timedelta(minutes=30),
        )

        self.assertEqual(self.sync(), 2)

        for conversion in WhoUpdateMetrikaConversion.objects.all():
            self.assertEqual(conversion.funnel_id, attempt.pk)
            self.assertEqual(conversion.identifier, "fresh-click")
            self.assertEqual(conversion.client_id, "fresh-client")
            self.assertEqual(conversion.counter_id, self.current_counter)

    def test_expired_source_does_not_rewrite_an_attempted_payload(self):
        conversion = self.legacy_failure(
            status=WhoUpdateMetrikaConversion.Status.PENDING,
            api_upload_id=None, api_status="", next_attempt_at=self.now,
        )
        original_payload = _csv_payload([conversion])
        self.source.landing_viewed_at = self.now - timedelta(days=30)
        self.source.metrika_client_id = "late-client"
        self.source.save(update_fields=["landing_viewed_at", "metrika_client_id"])

        self.assertEqual(self.sync(), 0)

        conversion.refresh_from_db()
        self.assertEqual(_csv_payload([conversion]), original_payload)
        self.assertEqual(conversion.status, WhoUpdateMetrikaConversion.Status.PENDING)
        self.assertEqual(conversion.attempts, 1)

    def test_legacy_timeout_retry_is_not_merged_with_paired_id_payload(self):
        legacy = self.legacy_failure(
            status=WhoUpdateMetrikaConversion.Status.PENDING,
            api_upload_id=None, api_status="", next_attempt_at=self.now,
        )
        original = _csv_payload([legacy])[1]
        self.source.connected_at = self.now - timedelta(minutes=2)
        self.source.save(update_fields=["connected_at"])
        self.sync()
        self.post.side_effect = [self.response(upload_id=88002), self.response(upload_id=88003)]

        self.assertEqual(upload_pending_conversions(token="test", now=self.now), 2)

        payloads = [call.kwargs["files"]["file"][1] for call in self.post.call_args_list]
        self.assertEqual(len(payloads), 2)
        self.assertIn(original, payloads)

    def test_confirmed_failed_onboarding_upload_adds_client_id_once(self):
        conversion = self.legacy_failure()
        original_time = conversion.occurred_at
        self.get.side_effect = None
        self.get.return_value = self.response("LINKAGE_FAILURE")

        self.assertEqual(recover_failed_onboarding_conversions(token="test", now=self.now), 1)
        self.assertEqual(recover_failed_onboarding_conversions(token="test", now=self.now), 0)

        conversion.refresh_from_db()
        self.assertEqual(conversion.client_id, self.source.metrika_client_id)
        self.assertEqual(conversion.occurred_at, original_time)
        self.assertEqual(conversion.counter_id, self.old_counter)
        self.assertEqual(conversion.status, WhoUpdateMetrikaConversion.Status.PENDING)
        self.assertIsNone(conversion.api_upload_id)
        self.assertEqual(conversion.upload_history[0]["id"], 88001)
        self.get.assert_called_once()
        self.post.assert_not_called()

    def test_ambiguous_or_successful_remote_status_is_not_replayed(self):
        conversion = self.legacy_failure()
        self.get.side_effect = None
        for status in ("PROCESSED", "UPLOADED", "EXPORTED", "MATCHED", "PREPARED"):
            with self.subTest(status=status):
                self.get.return_value = self.response(status)
                self.assertEqual(recover_failed_onboarding_conversions(token="test", now=self.now), 0)
        conversion.refresh_from_db()
        self.assertEqual(conversion.api_upload_id, 88001)
        self.assertEqual(conversion.client_id, "")
        self.assertEqual(conversion.upload_history, [])

    def test_status_timeout_does_not_allow_recovery(self):
        conversion = self.legacy_failure()
        self.get.side_effect = requests.Timeout("unknown")

        self.assertEqual(recover_failed_onboarding_conversions(token="test", now=self.now), 0)

        conversion.refresh_from_db()
        self.assertEqual(conversion.client_id, "")
        self.assertEqual(conversion.api_upload_id, 88001)

    def test_recovery_rechecks_upload_identity_under_lock(self):
        conversion = self.legacy_failure()

        self.assertFalse(recover_onboarding_linkage(
            conversion.pk, confirmed_upload_id=12345, now=self.now,
        ))

        conversion.refresh_from_db()
        self.assertEqual(conversion.api_upload_id, 88001)
        self.assertEqual(conversion.client_id, "")

    def test_expired_visits_and_wrong_counters_are_not_replayed(self):
        conversion = self.legacy_failure(counter_id=self.current_counter)
        self.assertEqual(recover_failed_onboarding_conversions(token="test", now=self.now), 0)
        conversion.counter_id = self.old_counter
        conversion.save(update_fields=["counter_id"])
        self.source.landing_viewed_at = self.now - timedelta(days=22)
        self.source.save(update_fields=["landing_viewed_at"])

        self.assertEqual(recover_failed_onboarding_conversions(token="test", now=self.now), 0)
        self.get.assert_not_called()

    def test_status_is_queried_once_for_failed_batch_and_recovery_is_bounded(self):
        self.legacy_failure()
        self.legacy_failure(
            event_type=WhoUpdateMetrikaConversion.EventType.CONNECTED,
            target="who_update_connected", occurred_at=self.now - timedelta(hours=1),
        )
        self.get.side_effect = None
        self.get.return_value = self.response("LINKAGE_FAILURE")

        self.assertEqual(recover_failed_onboarding_conversions(token="test", now=self.now, limit=1), 1)
        self.assertEqual(WhoUpdateMetrikaConversion.objects.filter(status="failed").count(), 1)
        self.assertEqual(self.get.call_count, 1)

    def test_failed_upload_later_processed_updates_status_without_reuploading(self):
        first = self.legacy_failure(next_attempt_at=self.now, last_error="linkage failed")
        second = self.legacy_failure(
            event_type=WhoUpdateMetrikaConversion.EventType.CONNECTED,
            target="who_update_connected", occurred_at=self.now - timedelta(hours=1),
            next_attempt_at=self.now, last_error="linkage failed",
        )
        original_payload = _csv_payload([first, second])
        self.get.side_effect = None
        self.get.return_value = self.response("PROCESSED")

        self.assertEqual(reconcile_failed_conversion_statuses(token="test", now=self.now), 2)
        self.assertEqual(reconcile_failed_conversion_statuses(token="test", now=self.now), 0)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(_csv_payload([first, second]), original_payload)
        for conversion in (first, second):
            self.assertEqual(conversion.status, WhoUpdateMetrikaConversion.Status.PROCESSED)
            self.assertEqual(conversion.api_status, "PROCESSED")
            self.assertEqual(conversion.api_upload_id, 88001)
            self.assertEqual(conversion.processed_at, self.now)
            self.assertEqual(conversion.last_error, "")
            self.assertEqual(conversion.attempts, 1)
            self.assertEqual(conversion.upload_history, [])
        self.get.assert_called_once()
        self.post.assert_not_called()

    def test_failed_status_rechecks_are_throttled_for_six_hours(self):
        conversion = self.legacy_failure(next_attempt_at=self.now, last_error="linkage failed")
        self.get.side_effect = None
        self.get.return_value = self.response("LINKAGE_FAILURE")

        self.assertEqual(reconcile_failed_conversion_statuses(token="test", now=self.now), 0)
        self.assertEqual(reconcile_failed_conversion_statuses(token="test", now=self.now + timedelta(hours=5)), 0)

        self.get.assert_called_once()
        conversion.refresh_from_db()
        self.assertEqual(conversion.status, WhoUpdateMetrikaConversion.Status.FAILED)
        self.assertEqual(conversion.api_status, "LINKAGE_FAILURE")
        self.assertEqual(conversion.last_error, "linkage failed")
        self.assertEqual(conversion.next_attempt_at, self.now + timedelta(hours=6))
        self.assertEqual(reconcile_failed_conversion_statuses(token="test", now=self.now + timedelta(hours=6)), 0)
        self.assertEqual(self.get.call_count, 2)
        self.post.assert_not_called()

    def test_failed_status_timeout_preserves_failure_and_delays_next_check(self):
        conversion = self.legacy_failure(next_attempt_at=self.now, last_error="linkage failed")
        self.get.side_effect = requests.Timeout("unknown")

        self.assertEqual(reconcile_failed_conversion_statuses(token="test", now=self.now), 0)
        self.assertEqual(reconcile_failed_conversion_statuses(token="test", now=self.now), 0)

        conversion.refresh_from_db()
        self.assertEqual(conversion.status, WhoUpdateMetrikaConversion.Status.FAILED)
        self.assertEqual(conversion.api_status, "LINKAGE_FAILURE")
        self.assertEqual(conversion.api_upload_id, 88001)
        self.assertEqual(conversion.last_error, "linkage failed")
        self.assertEqual(conversion.attempts, 1)
        self.assertEqual(conversion.next_attempt_at, self.now + timedelta(hours=6))
        self.get.assert_called_once()
        self.post.assert_not_called()

    def test_failed_status_check_skips_ineligible_rows(self):
        cases = (
            ("expired", {"occurred_at": self.now - timedelta(days=20, seconds=1)}),
            ("future", {"occurred_at": self.now + timedelta(seconds=1)}),
            ("not_due", {"next_attempt_at": self.now + timedelta(seconds=1)}),
            ("no_upload", {"api_upload_id": None}),
            ("submitted", {"status": WhoUpdateMetrikaConversion.Status.SUBMITTED}),
            ("processed", {"status": WhoUpdateMetrikaConversion.Status.PROCESSED}),
            ("pending", {"status": WhoUpdateMetrikaConversion.Status.PENDING}),
            ("unknown_failure", {"api_status": "UNKNOWN"}),
        )
        for label, fieldToOverride in cases:
            with self.subTest(row=label):
                conversion = self.legacy_failure(**{"next_attempt_at": self.now, **fieldToOverride})
                self.assertEqual(reconcile_failed_conversion_statuses(token="test", now=self.now), 0)
                conversion.delete()
        self.get.assert_not_called()
        self.post.assert_not_called()

    def test_failed_status_check_is_bounded_and_scoped_to_counter_and_upload(self):
        first = self.legacy_failure(next_attempt_at=self.now)
        second = self.legacy_failure(
            event_type=WhoUpdateMetrikaConversion.EventType.CONNECTED,
            target="who_update_connected", counter_id=self.current_counter,
            next_attempt_at=self.now,
        )
        attempt = WhoUpdateOnboardingFunnel.objects.create(user=self.user)
        third = self.legacy_failure(funnel=attempt, api_upload_id=88002, next_attempt_at=self.now)
        self.get.side_effect = lambda url, **fieldToArgument: self.response("PROCESSED", int(url.rsplit("/", 1)[1]))

        self.assertEqual(reconcile_failed_conversion_statuses(token="test", now=self.now, limit=1), 1)

        first.refresh_from_db()
        second.refresh_from_db()
        third.refresh_from_db()
        self.assertEqual(first.status, WhoUpdateMetrikaConversion.Status.PROCESSED)
        self.assertEqual(second.status, WhoUpdateMetrikaConversion.Status.FAILED)
        self.assertEqual(third.status, WhoUpdateMetrikaConversion.Status.FAILED)
        self.assertEqual(reconcile_failed_conversion_statuses(token="test", now=self.now, limit=1), 1)
        self.assertEqual(reconcile_failed_conversion_statuses(token="test", now=self.now, limit=1), 1)
        self.assertEqual(self.get.call_count, 3)
        self.assertEqual(
            {call.args[0] for call in self.get.call_args_list},
            {
                f"https://api-metrika.yandex.net/management/v1/counter/{self.old_counter}/offline_conversions/uploading/88001",
                f"https://api-metrika.yandex.net/management/v1/counter/{self.old_counter}/offline_conversions/uploading/88002",
                f"https://api-metrika.yandex.net/management/v1/counter/{self.current_counter}/offline_conversions/uploading/88001",
            },
        )
        self.post.assert_not_called()

    def test_failed_status_check_does_not_apply_stale_response_after_concurrent_change(self):
        conversion = self.legacy_failure(next_attempt_at=self.now)
        cases = (
            ("recovered", {"status": WhoUpdateMetrikaConversion.Status.PENDING, "api_upload_id": None}),
            ("different_counter", {"counter_id": self.current_counter}),
            ("different_upload", {"api_upload_id": 88002}),
            ("different_status", {"api_status": "MATCHED"}),
            ("different_check", {"next_attempt_at": self.now + timedelta(hours=7)}),
        )
        for label, fieldToValue in cases:
            with self.subTest(change=label):
                WhoUpdateMetrikaConversion.objects.filter(pk=conversion.pk).update(
                    status=WhoUpdateMetrikaConversion.Status.FAILED, api_status="LINKAGE_FAILURE",
                    counter_id=self.old_counter, api_upload_id=88001, next_attempt_at=self.now,
                )

                def concurrent_change(*args, **fieldToArgument):
                    WhoUpdateMetrikaConversion.objects.filter(pk=conversion.pk).update(**fieldToValue)
                    return self.response("PROCESSED")

                self.get.side_effect = concurrent_change
                self.assertEqual(reconcile_failed_conversion_statuses(token="test", now=self.now), 0)
                conversion.refresh_from_db()
                self.assertNotEqual(conversion.status, WhoUpdateMetrikaConversion.Status.PROCESSED)
                for field, value in fieldToValue.items():
                    self.assertEqual(getattr(conversion, field), value)
        self.post.assert_not_called()

    def test_failed_status_backoff_allows_next_upload_under_limit(self):
        first = self.legacy_failure(next_attempt_at=self.now)
        second = self.legacy_failure(
            event_type=WhoUpdateMetrikaConversion.EventType.CONNECTED,
            target="who_update_connected", api_upload_id=88002, next_attempt_at=self.now,
        )
        self.get.side_effect = [self.response("LINKAGE_FAILURE"), self.response("PROCESSED", 88002)]

        self.assertEqual(reconcile_failed_conversion_statuses(token="test", now=self.now, limit=1), 0)
        self.assertEqual(reconcile_failed_conversion_statuses(token="test", now=self.now, limit=1), 1)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, WhoUpdateMetrikaConversion.Status.FAILED)
        self.assertEqual(first.next_attempt_at, self.now + timedelta(hours=6))
        self.assertEqual(second.status, WhoUpdateMetrikaConversion.Status.PROCESSED)
        self.assertEqual(self.get.call_count, 2)
        self.post.assert_not_called()

    def test_failed_status_check_rejects_other_upload_or_ambiguous_remote_status(self):
        conversion = self.legacy_failure(next_attempt_at=self.now)
        self.get.side_effect = None
        for status, upload_id in (("PROCESSED", 88002), ("UPLOADED", 88001), ("MATCHED", 88001)):
            with self.subTest(status=status, upload_id=upload_id):
                WhoUpdateMetrikaConversion.objects.filter(pk=conversion.pk).update(next_attempt_at=self.now)
                self.get.return_value = self.response(status, upload_id)
                self.assertEqual(reconcile_failed_conversion_statuses(token="test", now=self.now), 0)
                conversion.refresh_from_db()
                self.assertEqual(conversion.status, WhoUpdateMetrikaConversion.Status.FAILED)
                self.assertEqual(conversion.api_status, "LINKAGE_FAILURE")
                self.assertEqual(conversion.api_upload_id, 88001)
        self.post.assert_not_called()

    def test_command_rechecks_failed_uploads_with_separate_limit(self):
        conversion = self.legacy_failure(next_attempt_at=self.now)
        self.get.side_effect = None
        self.get.return_value = self.response("PROCESSED")
        command_module = "webhook_tg.management.commands.upload_who_update_metrika_conversions"
        for name in (
            "sync_conversion_queue", "sync_purchase_conversions",
            "recover_failed_purchase_conversions", "upload_pending_conversions",
        ):
            self.enterContext(patch(f"{command_module}.{name}", return_value=0))
        submitted_check = self.enterContext(patch(f"{command_module}.reconcile_submitted_conversions", return_value=0))

        with self.settings(YANDEX_METRIKA_OAUTH_TOKEN="test"):
            call_command("upload_who_update_metrika_conversions", status_limit=0, failed_status_limit=1, stdout=io.StringIO())

        conversion.refresh_from_db()
        self.assertEqual(conversion.status, WhoUpdateMetrikaConversion.Status.PROCESSED)
        submitted_check.assert_called_once_with(token="test", limit=0)
        self.get.assert_called_once()
        self.post.assert_not_called()

    def audit_recovery_candidates(self):
        output = io.StringIO()
        with patch("webhook_tg.management.commands.audit_who_update_metrika.timezone.now", return_value=self.now):
            call_command("audit_who_update_metrika", stdout=output)
        fieldToValue = json.loads(output.getvalue())
        return fieldToValue["onboarding_recovery_candidates_pending_remote_confirmation"]

    def test_read_only_audit_counts_late_client_id_on_acquisition_fallback(self):
        attempt = WhoUpdateOnboardingFunnel.objects.create(
            user=self.user, utm_source="telegram",
            telegram_started_at=self.now - timedelta(hours=1),
            connected_at=self.now - timedelta(minutes=30),
        )
        self.legacy_failure(funnel=attempt, occurred_at=attempt.telegram_started_at)
        self.legacy_failure(
            funnel=attempt, event_type=WhoUpdateMetrikaConversion.EventType.CONNECTED,
            target="who_update_connected", occurred_at=attempt.connected_at,
        )
        WhoUpdateOnboardingFunnel.objects.filter(pk=self.source.pk).update(metrika_client_id="")
        self.assertEqual(self.audit_recovery_candidates(), 0)
        WhoUpdateOnboardingFunnel.objects.filter(pk=self.source.pk).update(metrika_client_id="late-client")
        original_conversions = list(WhoUpdateMetrikaConversion.objects.order_by("pk").values())

        self.assertEqual(self.audit_recovery_candidates(), 2)

        self.assertEqual(list(WhoUpdateMetrikaConversion.objects.order_by("pk").values()), original_conversions)
        attempt.refresh_from_db()
        self.assertEqual(attempt.yclid, "")
        self.assertEqual(attempt.metrika_client_id, "")
        self.get.assert_not_called()
        self.post.assert_not_called()

    def test_read_only_audit_rejects_invalid_and_expired_acquisition_sources(self):
        attempt = WhoUpdateOnboardingFunnel.objects.create(
            user=self.user, utm_source="telegram",
            telegram_started_at=self.now - timedelta(hours=1),
        )
        self.legacy_failure(funnel=attempt, occurred_at=attempt.telegram_started_at)
        other_user = UserTg.objects.create(user_id=9918002, chat_id=9918002)
        fieldToOriginalValue = {
            "metrika_counter_id": self.source.metrika_counter_id,
            "landing_viewed_at": self.source.landing_viewed_at,
            "telegram_started_at": self.source.telegram_started_at,
            "yclid": self.source.yclid,
            "metrika_client_id": self.source.metrika_client_id,
            "user_id": self.source.user_id,
        }
        cases = (
            ("wrong_counter", {"metrika_counter_id": self.current_counter}),
            ("expired_visit", {"landing_viewed_at": self.now - timedelta(days=21)}),
            ("missing_visit", {"landing_viewed_at": None}),
            ("visit_after_event", {"landing_viewed_at": self.now - timedelta(minutes=30)}),
            ("start_after_event", {"telegram_started_at": self.now - timedelta(minutes=30)}),
            ("different_click", {"yclid": "different-click"}),
            ("missing_client_id", {"metrika_client_id": ""}),
            ("different_user", {"user_id": other_user.pk}),
        )
        for label, fieldToValue in cases:
            with self.subTest(source=label):
                WhoUpdateOnboardingFunnel.objects.filter(pk=self.source.pk).update(**fieldToValue)
                self.assertEqual(self.audit_recovery_candidates(), 0)
                WhoUpdateOnboardingFunnel.objects.filter(pk=self.source.pk).update(**fieldToOriginalValue)
        self.assertEqual(self.audit_recovery_candidates(), 1)
        self.get.assert_not_called()
        self.post.assert_not_called()

    def test_read_only_audit_reconciles_cash_and_missing_identifiers_without_side_effects(self):
        self.source.delete()
        order = WhoUpdatePaymentOrder.objects.create(
            user=self.user, plan="month", duration_days=30,
            amount=Decimal("129.00"), status="paid", paid_at=self.now - timedelta(hours=1),
        )
        WhoUpdatePaymentOrder.objects.create(
            user=self.user, plan="month", duration_days=30,
            amount=Decimal("1.00"), status="paid", is_test=True,
            paid_at=self.now - timedelta(hours=1),
        )
        output = io.StringIO()

        call_command("audit_who_update_metrika", stdout=output)

        fieldToValue = json.loads(output.getvalue())
        self.assertEqual(fieldToValue["orders"], 1)
        self.assertEqual(fieldToValue["gross_rub"], "129.00")
        self.assertEqual(fieldToValue["reasonToTotals"]["missing_no_eligible_visit_identifiers"]["orders"], 1)
        self.assertEqual(WhoUpdateMetrikaConversion.objects.count(), 0)
        order.refresh_from_db()
        self.assertEqual(order.amount, Decimal("129.00"))
        self.assertEqual(order.status, "paid")
        self.get.assert_not_called()
        self.post.assert_not_called()
