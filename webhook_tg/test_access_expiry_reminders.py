from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from .background_tasks import (
    ACCESS_EXPIRY_REMINDER_TASK,
    _send_access_expiry_reminder,
    claim_next_task,
    process_claimed_task,
    recover_stale_tasks,
)
from .models import BackgroundTask, UserTg
from .subscriptions import notify_access_expired


class AccessExpiryReminderTests(TestCase):
    def setUp(self):
        self.expires_at = timezone.now()
        self.user = UserTg.objects.create(
            user_id=801,
            chat_id=801,
            access_unlimited=False,
            trial_started_at=self.expires_at - timedelta(days=7),
            access_expires_at=self.expires_at,
        )

    def run_timer(self):
        call_command("send_connection_reminders", stdout=StringIO())

    def drain_tasks(self):
        while task := claim_next_task(claimed_by="expiry-test-worker"):
            self.assertTrue(process_claimed_task(task))

    @patch("webhook_tg.background_tasks.dispatch_telegram_request", return_value=(True, ""))
    @patch("webhook_tg.telegram.tg_send_message", return_value=True)
    def test_timer_and_worker_send_once_at_expiry_12_24_and_48_hours(self, initial, reminder):
        for elapsed, expected_reminders in (
            (timedelta(seconds=-1), 0),
            (timedelta(), 0),
            (timedelta(hours=12, microseconds=-1), 0),
            (timedelta(hours=12), 1),
            (timedelta(hours=24, microseconds=-1), 1),
            (timedelta(hours=24), 2),
            (timedelta(hours=48, microseconds=-1), 2),
            (timedelta(hours=48), 3),
            (timedelta(days=5), 3),
        ):
            with self.subTest(elapsed=elapsed), patch(
                "django.utils.timezone.now", return_value=self.expires_at + elapsed
            ):
                for _ in range(2):
                    self.run_timer()
                    self.drain_tasks()
                self.assertEqual(initial.call_count, int(elapsed >= timedelta()))
                self.assertEqual(reminder.call_count, expected_reminders)

        tasks = list(BackgroundTask.objects.order_by("run_at"))
        self.assertEqual(len(tasks), 3)
        self.assertEqual(
            [task.run_at for task in tasks],
            [self.expires_at + timedelta(hours=hours) for hours in (12, 24, 48)],
        )
        for send in reminder.call_args_list:
            self.assertEqual(send.args[:2], ("sendMessage", self.user.chat_id))
            self.assertEqual(send.args[2]["text"], initial.call_args.args[1])
            self.assertEqual(len(send.args[2]["reply_markup"]["inline_keyboard"]), 4)
            self.assertIn("web_app", send.args[2]["reply_markup"]["inline_keyboard"][-1][0])
            self.assertEqual(send.args[2]["parse_mode"], "HTML")

    @patch("webhook_tg.background_tasks.dispatch_telegram_request", return_value=(True, ""))
    @patch("webhook_tg.telegram.tg_send_message", return_value=True)
    def test_saved_delivery_is_not_resent_after_worker_recovers(self, initial, reminder):
        with patch("django.utils.timezone.now", return_value=self.expires_at):
            notify_access_expired(self.user)
        due_at = self.expires_at + timedelta(hours=12)
        with patch("django.utils.timezone.now", return_value=due_at):
            task = claim_next_task(claimed_by="expiry-test-worker")
            # Имитируем остановку воркера после доставки, до смены processing.
            _send_access_expiry_reminder(task)
        task.refresh_from_db()
        self.assertEqual(task.status, BackgroundTask.Status.PROCESSING)
        self.assertEqual(task.payload["delivery_status"], "sent")
        with patch("django.utils.timezone.now", return_value=due_at + timedelta(minutes=6)):
            self.assertEqual(recover_stale_tasks(), 1)
            recovered = claim_next_task(claimed_by="replacement-worker")
            self.assertEqual(recovered.pk, task.pk)
            self.assertTrue(process_claimed_task(recovered))
        task.refresh_from_db()
        self.assertEqual(task.status, BackgroundTask.Status.COMPLETED)
        reminder.assert_called_once()

    @patch("webhook_tg.background_tasks.dispatch_telegram_request", return_value=(True, ""))
    @patch("webhook_tg.telegram.tg_send_message", return_value=True)
    def test_old_reminders_are_skipped_even_after_renewed_access_also_expires(self, initial, reminder):
        with patch("django.utils.timezone.now", return_value=self.expires_at):
            notify_access_expired(self.user)
            self.user.extend_access(1)
        with patch("django.utils.timezone.now", return_value=self.expires_at + timedelta(hours=48)):
            self.drain_tasks()
        reminder.assert_not_called()
        self.assertEqual(
            list(BackgroundTask.objects.values_list("payload__delivery_status", flat=True)),
            ["skipped"] * 3,
        )

    @patch("webhook_tg.background_tasks.dispatch_telegram_request", return_value=(True, ""))
    @patch("webhook_tg.telegram.tg_send_message", return_value=True)
    def test_unlimited_access_skips_pending_reminders(self, initial, reminder):
        with patch("django.utils.timezone.now", return_value=self.expires_at):
            notify_access_expired(self.user)
        UserTg.objects.filter(pk=self.user.pk).update(access_unlimited=True)
        with patch("django.utils.timezone.now", return_value=self.expires_at + timedelta(hours=48)):
            self.drain_tasks()
        reminder.assert_not_called()

    @patch("webhook_tg.telegram.tg_send_message", return_value=True)
    def test_stale_user_instance_after_renewal_does_not_start_a_chain(self, initial):
        UserTg.objects.filter(pk=self.user.pk).update(
            access_expires_at=self.expires_at + timedelta(days=30)
        )
        self.assertFalse(notify_access_expired(self.user, at=self.expires_at))
        self.assertFalse(BackgroundTask.objects.exists())
        initial.assert_not_called()

    @patch("webhook_tg.telegram.tg_send_message", side_effect=[False, True])
    def test_initial_delivery_retry_does_not_duplicate_the_chain(self, initial):
        with patch("django.utils.timezone.now", return_value=self.expires_at):
            self.assertFalse(notify_access_expired(self.user))
            self.user.refresh_from_db()
            self.assertIsNone(self.user.access_expired_notified_at)
            self.assertTrue(notify_access_expired(self.user))
        self.assertEqual(BackgroundTask.objects.count(), 3)

    @patch("webhook_tg.background_tasks.dispatch_telegram_request")
    @patch("webhook_tg.telegram.tg_send_message", return_value=True)
    def test_temporary_error_retries_only_the_failed_reminder(self, initial, reminder):
        with patch("django.utils.timezone.now", return_value=self.expires_at):
            notify_access_expired(self.user)
        due_at = self.expires_at + timedelta(hours=12)
        with patch("django.utils.timezone.now", return_value=due_at):
            reminder.return_value = (False, "Temporary Telegram error")
            task = claim_next_task(claimed_by="expiry-test-worker")
            with self.assertLogs("webhook_tg.background_tasks", level="ERROR"):
                self.assertFalse(process_claimed_task(task))
            task.refresh_from_db()
            self.assertEqual(task.status, BackgroundTask.Status.PENDING)
            self.assertEqual(task.attempts, 1)
            self.assertIsNone(claim_next_task(claimed_by="expiry-test-worker"))
        with patch("django.utils.timezone.now", return_value=due_at + timedelta(seconds=30)):
            reminder.return_value = (True, "")
            self.drain_tasks()
        task.refresh_from_db()
        self.assertEqual(task.status, BackgroundTask.Status.COMPLETED)
        self.assertEqual(BackgroundTask.objects.count(), 3)

    @patch("webhook_tg.background_tasks.dispatch_telegram_request")
    @patch("webhook_tg.telegram.tg_send_message", return_value=True)
    def test_blocked_user_stops_retrying_the_reminder(self, initial, reminder):
        with patch("django.utils.timezone.now", return_value=self.expires_at):
            notify_access_expired(self.user)
        with patch("django.utils.timezone.now", return_value=self.expires_at + timedelta(hours=12)):
            reminder.return_value = (False, "Forbidden: bot was blocked by the user")
            task = claim_next_task(claimed_by="expiry-test-worker")
            self.assertTrue(process_claimed_task(task))
        task.refresh_from_db()
        self.assertEqual(task.status, BackgroundTask.Status.CANCELLED)

    @patch("webhook_tg.telegram.tg_send_message", return_value=True)
    def test_renewed_access_starts_a_new_chain_on_the_next_expiry(self, initial):
        with patch("django.utils.timezone.now", return_value=self.expires_at):
            notify_access_expired(self.user)
            self.user.extend_access(30)
        with patch("django.utils.timezone.now", return_value=self.expires_at + timedelta(days=30)):
            self.run_timer()
            self.run_timer()
        self.assertEqual(initial.call_count, 2)
        self.assertEqual(BackgroundTask.objects.count(), 6)

    @patch("webhook_tg.telegram.tg_send_message", return_value=True)
    def test_late_initial_notice_does_not_queue_overdue_reminders(self, initial):
        with patch("django.utils.timezone.now", return_value=self.expires_at + timedelta(hours=25)):
            self.run_timer()
        task = BackgroundTask.objects.get(task_type=ACCESS_EXPIRY_REMINDER_TASK)
        self.assertEqual(task.run_at, self.expires_at + timedelta(hours=48))
        initial.assert_called_once()

    @patch("webhook_tg.telegram.tg_send_message", return_value=True)
    def test_previously_notified_users_get_only_remaining_reminders(self, initial):
        UserTg.objects.filter(pk=self.user.pk).update(access_expired_notified_at=self.expires_at)
        old_user = UserTg.objects.create(
            user_id=802,
            chat_id=802,
            access_unlimited=False,
            access_expires_at=self.expires_at - timedelta(days=5),
            access_expired_notified_at=self.expires_at - timedelta(days=5),
        )
        with patch("django.utils.timezone.now", return_value=self.expires_at + timedelta(hours=13)):
            for _ in range(2):
                self.run_timer()
        initial.assert_not_called()
        self.assertEqual(
            list(BackgroundTask.objects.order_by("run_at").values_list("run_at", flat=True)),
            [self.expires_at + timedelta(hours=hours) for hours in (24, 48)],
        )
        self.assertFalse(BackgroundTask.objects.filter(payload__user_pk=old_user.pk).exists())
