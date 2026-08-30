from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from .background_tasks import (
    CONNECTION_REMINDER_TASK,
    claim_next_task,
    process_claimed_task,
    schedule_connection_reminders,
)
from .config import CONNECTION_REMINDER_REPLY_MARKUP
from .models import BackgroundTask, UserTg


class BackgroundTaskTests(TestCase):
    def setUp(self):
        self.started_at = timezone.now()
        self.user = UserTg.objects.create(
            user_id=501,
            chat_id=601,
            username="background_user",
            last_start_at=self.started_at,
            business_is_connected=False,
        )

    def _schedule(self, update_id=7001):
        return schedule_connection_reminders(
            self.user,
            started_at=self.started_at,
            start_update_id=update_id,
        )

    def _make_due(self, task):
        BackgroundTask.objects.filter(pk=task.pk).update(run_at=timezone.now() - timedelta(seconds=1))
        return claim_next_task(claimed_by="test-worker")

    def test_schedules_30_minute_and_24_hour_reminders_idempotently(self):
        self._schedule()
        self._schedule()

        tasks = list(BackgroundTask.objects.order_by("run_at"))
        self.assertEqual(len(tasks), 2)
        self.assertAlmostEqual(
            (tasks[0].run_at - self.started_at).total_seconds(),
            timedelta(minutes=30).total_seconds(),
            delta=0.01,
        )
        self.assertAlmostEqual(
            (tasks[1].run_at - self.started_at).total_seconds(),
            timedelta(days=1).total_seconds(),
            delta=0.01,
        )
        self.assertTrue(all(task.idempotency_key for task in tasks))

    @patch("webhook_tg.background_tasks.dispatch_telegram_request")
    def test_checks_connection_immediately_before_sending(self, dispatch_mock):
        task = self._schedule()[0]
        self.user.business_is_connected = True
        self.user.save(update_fields=["business_is_connected"])

        claimed = self._make_due(task)
        self.assertIsNotNone(claimed)
        self.assertTrue(process_claimed_task(claimed))

        dispatch_mock.assert_not_called()
        task.refresh_from_db()
        self.assertEqual(task.status, BackgroundTask.Status.COMPLETED)

    @patch("webhook_tg.background_tasks.dispatch_telegram_request")
    def test_sends_each_due_reminder_when_user_is_still_disconnected(self, dispatch_mock):
        dispatch_mock.return_value = (True, "")
        tasks = self._schedule()

        for task in tasks:
            claimed = self._make_due(task)
            self.assertTrue(process_claimed_task(claimed))

        self.assertEqual(dispatch_mock.call_count, 2)
        for call in dispatch_mock.call_args_list:
            self.assertEqual(
                call.args[2]["reply_markup"],
                CONNECTION_REMINDER_REPLY_MARKUP,
            )
        self.assertFalse(
            BackgroundTask.objects.filter(
                task_type=CONNECTION_REMINDER_TASK,
                status=BackgroundTask.Status.PENDING,
            ).exists()
        )

    @patch("webhook_tg.background_tasks.dispatch_telegram_request")
    def test_old_start_tasks_do_not_send_after_new_start(self, dispatch_mock):
        old_task = self._schedule(update_id=7001)[0]
        new_started_at = self.started_at + timedelta(minutes=5)
        self.user.last_start_at = new_started_at
        self.user.save(update_fields=["last_start_at"])
        schedule_connection_reminders(
            self.user,
            started_at=new_started_at,
            start_update_id=7002,
        )

        old_task.refresh_from_db()
        self.assertEqual(old_task.status, BackgroundTask.Status.CANCELLED)
        dispatch_mock.assert_not_called()

    @patch("webhook_tg.background_tasks.dispatch_telegram_request")
    def test_transient_failure_is_retried(self, dispatch_mock):
        dispatch_mock.return_value = (False, "Temporary Telegram error")
        task = self._schedule()[0]
        claimed = self._make_due(task)

        self.assertFalse(process_claimed_task(claimed))

        task.refresh_from_db()
        self.assertEqual(task.status, BackgroundTask.Status.PENDING)
        self.assertEqual(task.attempts, 1)
        self.assertGreater(task.run_at, timezone.now())
