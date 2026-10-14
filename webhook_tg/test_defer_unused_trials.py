from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from .background_tasks import ACCESS_EXPIRY_REMINDER_TASK, _send_access_expiry_reminder
from .models import (
    BackgroundTask,
    TelegramIncomingUpdate,
    UserTg,
    WhoUpdateMetrikaConversion,
    WhoUpdateOnboardingFunnel,
    WhoUpdatePaymentOrder,
    WhoUpdateWheelReward,
)


class DeferUnusedTrialsTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.user_id = 8_700_000_000

    def create_user(self, duration=7, **fieldToValue):
        self.user_id += 1
        trial_at = self.now - timedelta(days=20)
        defaultFieldToValue = {
            "user_id": self.user_id,
            "chat_id": self.user_id,
            "access_unlimited": False,
            "trial_started_at": trial_at,
            "access_expires_at": trial_at + timedelta(days=duration),
            "access_expired_notified_at": self.now - timedelta(days=1),
            "last_start_at": trial_at,
        }
        return UserTg.objects.create(**(defaultFieldToValue | fieldToValue))

    def run_backfill(self, *, apply=False):
        output = StringIO()
        call_command("defer_unused_trials", apply=apply, stdout=output)
        return output.getvalue()

    def access_state(self, user):
        user.refresh_from_db()
        return (
            user.trial_started_at,
            user.access_expires_at,
            user.access_expired_notified_at,
            user.access_unlimited,
        )

    def test_dry_run_does_not_change_users(self):
        user = self.create_user()
        original = self.access_state(user)
        result = self.run_backfill()
        self.assertIn("Dry run: deferred=1", result)
        self.assertEqual(self.access_state(user), original)

    def test_apply_defers_unused_seven_fourteen_and_zero_day_trials_once(self):
        users = [self.create_user(duration=days) for days in (7, 14, 0)]
        self.assertIn("Applied: deferred=3", self.run_backfill(apply=True))
        for user in users:
            with self.subTest(user=user.pk):
                last_start = user.last_start_at
                self.assertEqual(self.access_state(user), (None, None, None, False))
                self.assertEqual(user.last_start_at, last_start)
        self.assertIn("Applied: deferred=0", self.run_backfill(apply=True))

    def test_explicit_connection_disconnect_and_reward_markers_are_never_changed(self):
        users = [
            self.create_user(business_is_connected=True),
            self.create_user(business_connected_at=self.now),
            self.create_user(business_disconnected_at=self.now),
            self.create_user(business_connection_id="historical-connection"),
            self.create_user(referral_rewarded_at=self.now),
            self.create_user(access_unlimited=True),
        ]
        original_states = [self.access_state(user) for user in users]
        self.assertIn("Applied: deferred=0", self.run_backfill(apply=True))
        for user, original in zip(users, original_states):
            self.assertEqual(self.access_state(user), original)

    def test_connected_funnel_or_conversion_preserves_trial(self):
        connected = self.create_user()
        converted = self.create_user()
        WhoUpdateOnboardingFunnel.objects.create(user=connected, connected_at=self.now)
        funnel = WhoUpdateOnboardingFunnel.objects.create(user=converted)
        WhoUpdateMetrikaConversion.objects.create(
            funnel=funnel,
            event_type=WhoUpdateMetrikaConversion.EventType.CONNECTED,
            target="who_update_connected",
            occurred_at=self.now,
            identifier_type=WhoUpdateMetrikaConversion.IdentifierType.CLIENT_ID,
            identifier="trial-backfill-test",
        )
        original_states = [self.access_state(user) for user in (connected, converted)]
        result = self.run_backfill(apply=True)
        self.assertIn("deferred=0", result)
        self.assertIn("skipped_history_or_custom_access=2", result)
        for user, original in zip((connected, converted), original_states):
            self.assertEqual(self.access_state(user), original)

    def test_historical_incoming_connection_json_keeps_bigint_users_unchanged(self):
        users = [self.create_user(), self.create_user()]
        original_states = [self.access_state(user) for user in users]
        for enabled, user in zip((True, False), users):
            TelegramIncomingUpdate.objects.create(
                update_id=user.pk,
                queue=TelegramIncomingUpdate.Queue.PRIORITY,
                next_attempt_at=self.now,
                payload={"business_connection": {
                    "id": f"old-{user.pk}",
                    "user": {"id": user.user_id},
                    "is_enabled": enabled,
                }},
            )
        stored_ids = list(TelegramIncomingUpdate.objects.filter(
            payload__has_key="business_connection",
        ).values_list("payload__business_connection__user__id", flat=True))
        self.assertEqual(stored_ids, [user.user_id for user in users])
        self.assertTrue(all(type(user_id) is int for user_id in stored_ids))
        self.assertIn("deferred=0", self.run_backfill(apply=True))
        for user, original in zip(users, original_states):
            self.assertEqual(self.access_state(user), original)

    def test_start_only_funnel_and_incoming_message_do_not_block_backfill(self):
        user = self.create_user()
        funnel = WhoUpdateOnboardingFunnel.objects.create(user=user, telegram_started_at=self.now)
        WhoUpdateMetrikaConversion.objects.create(
            funnel=funnel,
            event_type=WhoUpdateMetrikaConversion.EventType.START,
            target="who_update_start",
            occurred_at=self.now,
            identifier_type=WhoUpdateMetrikaConversion.IdentifierType.CLIENT_ID,
            identifier="trial-backfill-start",
        )
        TelegramIncomingUpdate.objects.create(
            update_id=user.pk,
            queue=TelegramIncomingUpdate.Queue.PRIORITY,
            next_attempt_at=self.now,
            payload={"message": {"from": {"id": user.user_id}, "text": "/start"}},
        )
        self.assertIn("deferred=1", self.run_backfill(apply=True))
        self.assertEqual(self.access_state(user), (None, None, None, False))

    def test_paid_referral_and_granted_wheel_deadlines_are_preserved(self):
        paid = self.create_user(duration=37)
        legacy_paid = self.create_user(duration=37)
        referral = self.create_user(duration=21, referral_bonus_days=14)
        wheel = self.create_user(duration=10)
        WhoUpdatePaymentOrder.objects.create(
            user=paid, status=WhoUpdatePaymentOrder.Status.PAID,
            plan=WhoUpdatePaymentOrder.Plan.MONTH, amount=Decimal("99.00"), duration_days=30,
        )
        WhoUpdatePaymentOrder.objects.create(
            user=legacy_paid, paid_at=self.now,
            plan=WhoUpdatePaymentOrder.Plan.MONTH, amount=Decimal("99.00"), duration_days=30,
        )
        WhoUpdateWheelReward.objects.create(
            user=wheel, spin_day=self.now.date(), expires_at=self.now,
            prize="days3", granted_days=3, used_at=self.now,
        )
        users = [paid, legacy_paid, referral, wheel]
        deadlines = [user.access_expires_at for user in users]
        result = self.run_backfill(apply=True)
        self.assertIn("deferred=4", result)
        self.assertIn("preserved_paid_or_bonus_deadlines=4", result)
        for user, deadline in zip(users, deadlines):
            self.assertEqual(self.access_state(user), (None, deadline, None, False))
        self.assertIn("deferred=0", self.run_backfill(apply=True))

    def test_pending_payment_and_ungranted_wheel_do_not_preserve_unused_trial(self):
        user = self.create_user()
        WhoUpdatePaymentOrder.objects.create(
            user=user, plan=WhoUpdatePaymentOrder.Plan.MONTH,
            amount=Decimal("99.00"), duration_days=30,
        )
        WhoUpdateWheelReward.objects.create(
            user=user, spin_day=self.now.date(), expires_at=self.now,
            prize="discount10",
        )
        self.run_backfill(apply=True)
        self.assertEqual(self.access_state(user), (None, None, None, False))

    def test_unexplained_manual_deadline_is_not_changed(self):
        user = self.create_user(duration=45)
        original = self.access_state(user)
        result = self.run_backfill(apply=True)
        self.assertIn("deferred=0", result)
        self.assertIn("skipped_history_or_custom_access=1", result)
        self.assertEqual(self.access_state(user), original)

    @patch("webhook_tg.management.commands.send_connection_reminders.LOCK_PATH")
    @patch("webhook_tg.management.commands.send_connection_reminders.fcntl.flock")
    @patch("webhook_tg.management.commands.send_connection_reminders.notify_access_expired", return_value=True)
    def test_pending_trials_do_not_starve_real_expirations_at_timer_limit(self, notify, flock, lock_path):
        self.create_user(trial_started_at=None, access_expired_notified_at=None)
        expired = self.create_user(
            access_expires_at=self.now - timedelta(minutes=1),
            access_expired_notified_at=None,
        )
        call_command("send_connection_reminders", limit=1, stdout=StringIO())
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0].pk, expired.pk)

    @patch("webhook_tg.management.commands.send_connection_reminders.LOCK_PATH")
    @patch("webhook_tg.management.commands.send_connection_reminders.fcntl.flock")
    @patch("webhook_tg.management.commands.send_connection_reminders.schedule_access_expiry_reminders")
    def test_timer_does_not_schedule_old_expiry_chain_for_pending_trial(self, schedule, flock, lock_path):
        self.create_user(
            trial_started_at=None,
            access_expires_at=self.now - timedelta(hours=1),
        )
        call_command("send_connection_reminders", stdout=StringIO())
        schedule.assert_not_called()

    @patch("webhook_tg.background_tasks.dispatch_telegram_request")
    def test_queued_expiry_reminder_is_skipped_after_trial_is_deferred(self, dispatch):
        user = self.create_user(duration=10, referral_bonus_days=3)
        task = BackgroundTask.objects.create(
            task_type=ACCESS_EXPIRY_REMINDER_TASK,
            payload={"user_pk": user.pk, "expires_at": user.access_expires_at.isoformat()},
            run_at=self.now,
            idempotency_key="trial-defer-expiry-test",
        )
        self.run_backfill(apply=True)
        _send_access_expiry_reminder(task)
        dispatch.assert_not_called()
        task.refresh_from_db()
        self.assertEqual(task.payload["delivery_status"], "skipped")
