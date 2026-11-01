from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from .models import (
    UserTg,
    WhoUpdateMetrikaConversion,
    WhoUpdateOnboardingFunnel,
    WhoUpdateTelegramStart,
    WhoUpdateUserAcquisition,
)
from .onboarding_analytics import (
    attribution_funnel,
    is_first_connection_funnel,
    record_connection,
    record_reminder_sent,
    register_telegram_start,
)


class DurableAcquisitionTests(TestCase):
    def setUp(self):
        self.user = UserTg.objects.create(user_id=101, chat_id=101)
        self.started_at = timezone.now() - timedelta(hours=1)

    def tracked_start(self, *, user=None, update_id=1):
        funnel = WhoUpdateOnboardingFunnel.objects.create(
            utm_source="yandex", yclid="click-a", metrika_client_id="client-a",
            metrika_counter_id=112093587, landing_path="/",
            landing_viewed_at=self.started_at - timedelta(minutes=1),
        )
        return register_telegram_start(
            user or self.user, f"trk_{funnel.tracking_code}",
            update_id=update_id, started_at=self.started_at,
        )

    def plain_start(self, *, update_id=2, at=None):
        return register_telegram_start(
            self.user, "", update_id=update_id,
            started_at=at or self.started_at + timedelta(minutes=1),
        )

    def test_plain_start_preserves_first_acquisition_and_connection_source(self):
        original = self.tracked_start()
        attempt = self.plain_start()
        connected = record_connection(self.user)

        acquisition = WhoUpdateUserAcquisition.objects.get(user=self.user)
        self.assertEqual(connected.pk, attempt.pk)
        self.assertEqual(acquisition.acquisition_funnel_id, original.pk)
        self.assertEqual(acquisition.first_start_funnel_id, original.pk)
        self.assertEqual(attribution_funnel(connected).pk, original.pk)
        self.assertEqual(connected.yclid, "")
        self.assertTrue(is_first_connection_funnel(connected))

    def test_repeated_tracking_link_preserves_original_and_each_update_is_idempotent(self):
        original = self.tracked_start()
        repeated = register_telegram_start(
            self.user, f"trk_{original.tracking_code}", update_id=2,
            started_at=self.started_at + timedelta(minutes=1),
        )
        replay = self.plain_start(update_id=2)

        self.assertEqual(repeated.pk, original.pk)
        self.assertEqual(replay.pk, original.pk)
        original.refresh_from_db()
        self.assertEqual(original.telegram_started_at, self.started_at)
        self.assertEqual(original.start_update_id, 1)
        self.assertEqual(WhoUpdateOnboardingFunnel.objects.count(), 1)
        self.assertEqual(WhoUpdateTelegramStart.objects.count(), 2)

    def test_forwarded_tracking_link_cannot_claim_another_users_acquisition(self):
        original = self.tracked_start()
        other = UserTg.objects.create(user_id=202, chat_id=202)
        attempt = register_telegram_start(
            other, f"trk_{original.tracking_code}", update_id=2,
            started_at=self.started_at,
        )

        original.refresh_from_db()
        self.assertEqual(original.user_id, self.user.pk)
        self.assertEqual(attempt.user_id, other.pk)
        self.assertIsNone(attribution_funnel(attempt))
        with self.assertRaises(ValueError):
            register_telegram_start(other, "", update_id=1, started_at=self.started_at)

    def test_reconnection_does_not_replace_first_connection(self):
        self.tracked_start()
        first = record_connection(self.user)
        self.plain_start(update_id=2, at=timezone.now())
        second = record_connection(self.user, connected_at=timezone.now() + timedelta(minutes=1))

        self.assertTrue(is_first_connection_funnel(first))
        self.assertFalse(is_first_connection_funnel(second))
        self.assertEqual(WhoUpdateUserAcquisition.objects.get(user=self.user).first_connection_funnel_id, first.pk)

    def test_acquisition_is_not_replaced_by_a_later_ad_click(self):
        original = self.tracked_start()
        later = self.tracked_start(update_id=2)
        self.assertEqual(attribution_funnel(later).pk, later.pk)
        attempt = self.plain_start(update_id=3)
        self.assertEqual(attribution_funnel(attempt).pk, original.pk)

    def test_plain_start_does_not_erase_prior_reminder_exposure(self):
        original = self.tracked_start()
        record_reminder_sent(
            self.user, 1, sent_at=self.started_at + timedelta(minutes=30), funnel_pk=original.pk,
        )
        self.plain_start(at=self.started_at + timedelta(minutes=40))
        connected = record_connection(self.user)
        self.assertEqual(connected.connection_stage, "after_first")

    def test_reminder_from_future_is_not_counted(self):
        original = self.tracked_start()
        record_reminder_sent(
            self.user, 2, sent_at=timezone.now() + timedelta(days=1), funnel_pk=original.pk,
        )
        connected = record_connection(self.user)
        self.assertEqual(connected.connection_stage, "immediate")

    def test_source_from_future_or_different_user_is_never_used(self):
        source = self.tracked_start()
        early = self.plain_start(at=self.started_at - timedelta(minutes=2))
        self.assertIsNone(attribution_funnel(early))
        later = self.plain_start(update_id=3)
        other = UserTg.objects.create(user_id=202, chat_id=202)
        WhoUpdateOnboardingFunnel.objects.filter(pk=source.pk).update(user=other)
        self.assertIsNone(attribution_funnel(later))

    def test_delayed_client_id_can_populate_acquisition_without_rewriting_attempt(self):
        original = WhoUpdateOnboardingFunnel.objects.create(
            landing_path="/", landing_viewed_at=self.started_at - timedelta(minutes=1),
        )
        register_telegram_start(
            self.user, f"trk_{original.tracking_code}", update_id=1, started_at=self.started_at,
        )
        WhoUpdateOnboardingFunnel.objects.filter(pk=original.pk).update(metrika_client_id="late-client")
        self.plain_start()
        connected = record_connection(self.user)
        self.assertEqual(attribution_funnel(connected).metrika_client_id, "late-client")
        self.assertEqual(connected.metrika_client_id, "")

    def test_legacy_state_without_first_date_is_not_new_connection(self):
        self.user.business_connected_at = self.started_at - timedelta(days=3)
        self.user.business_disconnected_at = self.started_at - timedelta(days=2)
        self.user.save()
        self.tracked_start()
        connected = record_connection(self.user)
        self.plain_start(update_id=3, at=timezone.now())
        reconnected = record_connection(self.user, connected_at=timezone.now() + timedelta(minutes=1))

        acquisition = WhoUpdateUserAcquisition.objects.get(user=self.user)
        self.assertTrue(acquisition.has_connection_history)
        self.assertIsNone(acquisition.first_connected_at)
        self.assertFalse(is_first_connection_funnel(connected))
        self.assertFalse(is_first_connection_funnel(reconnected))

    def test_explicit_previous_connection_cannot_become_a_first_connection(self):
        self.tracked_start()
        connected = record_connection(self.user, had_connection_history=True)
        acquisition = WhoUpdateUserAcquisition.objects.get(user=self.user)
        self.assertTrue(acquisition.has_connection_history)
        self.assertIsNone(acquisition.first_connected_at)
        self.assertFalse(is_first_connection_funnel(connected))

    def test_backfill_is_dry_run_by_default_and_never_rewrites_conversions(self):
        source = WhoUpdateOnboardingFunnel.objects.create(
            user=self.user, yclid="old-click", landing_path="/",
            landing_viewed_at=self.started_at - timedelta(minutes=1),
            telegram_started_at=self.started_at, start_update_id=100,
        )
        connected = WhoUpdateOnboardingFunnel.objects.create(
            user=self.user, telegram_started_at=self.started_at + timedelta(minutes=1),
            connected_at=self.started_at + timedelta(minutes=2),
        )
        conversion = WhoUpdateMetrikaConversion.objects.create(
            funnel=source, event_type="start", target="who_update_start",
            occurred_at=self.started_at, identifier_type="yclid", identifier="old-click",
            status="processed", attempts=1, api_upload_id=987,
        )
        fieldToOriginalValue = WhoUpdateMetrikaConversion.objects.values().get(pk=conversion.pk)
        output = StringIO()
        call_command("backfill_who_update_acquisition", stdout=output)
        self.assertIn("Dry run (no writes)", output.getvalue())
        self.assertFalse(WhoUpdateUserAcquisition.objects.exists())
        call_command("backfill_who_update_acquisition", "--apply", stdout=StringIO())
        acquisition = WhoUpdateUserAcquisition.objects.get(user=self.user)
        self.assertEqual(acquisition.acquisition_funnel_id, source.pk)
        self.assertEqual(acquisition.first_connection_funnel_id, connected.pk)
        self.assertEqual(WhoUpdateMetrikaConversion.objects.values().get(pk=conversion.pk), fieldToOriginalValue)
        second = StringIO()
        call_command("backfill_who_update_acquisition", "--apply", stdout=second)
        self.assertIn("unchanged=1", second.getvalue())

    def test_checkout_cannot_be_relabelled_as_acquisition(self):
        WhoUpdateOnboardingFunnel.objects.create(
            user=self.user, yclid="checkout-click", landing_path="/bot/checkout/",
            landing_viewed_at=self.started_at - timedelta(minutes=1),
            telegram_started_at=self.started_at,
        )
        attempt = self.plain_start()
        self.assertIsNone(attribution_funnel(attempt))

    def test_backfill_does_not_import_events_dated_in_the_future(self):
        future = timezone.now() + timedelta(days=1)
        WhoUpdateOnboardingFunnel.objects.create(
            user=self.user, yclid="future", landing_path="/",
            landing_viewed_at=future, telegram_started_at=future, connected_at=future,
        )
        call_command("backfill_who_update_acquisition", "--apply", stdout=StringIO())
        acquisition = WhoUpdateUserAcquisition.objects.get(user=self.user)
        self.assertIsNone(acquisition.first_start_at)
        self.assertIsNone(acquisition.first_connected_at)
        self.assertIsNone(acquisition.acquisition_funnel_id)
        self.assertFalse(acquisition.has_connection_history)

    def test_backfill_does_not_promote_a_funnel_after_older_legacy_history(self):
        for field in ("business_connected_at", "business_disconnected_at"):
            with self.subTest(field=field):
                user = UserTg.objects.create(user_id=1000 + len(field), chat_id=1000 + len(field))
                setattr(user, field, self.started_at - timedelta(days=10))
                user.save(update_fields=[field])
                WhoUpdateOnboardingFunnel.objects.create(
                    user=user, telegram_started_at=self.started_at,
                    connected_at=self.started_at + timedelta(minutes=1),
                )
                call_command("backfill_who_update_acquisition", "--apply", stdout=StringIO())
                acquisition = WhoUpdateUserAcquisition.objects.get(user=user)
                self.assertTrue(acquisition.has_connection_history)
                self.assertIsNone(acquisition.first_connected_at)
                self.assertIsNone(acquisition.first_connection_funnel_id)
