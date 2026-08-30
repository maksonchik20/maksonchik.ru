from datetime import timedelta
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from .config import CONNECTION_REMINDER_REPLY_MARKUP
from .models import UserTg


class ConnectionReminderCommandTests(TestCase):
    @patch("webhook_tg.telegram.tg_send_message")
    def test_sends_expired_access_notification_once(self, send_message):
        send_message.return_value = True
        user = UserTg.objects.create(
            user_id=104,
            chat_id=104,
            access_unlimited=False,
            access_expires_at=timezone.now() - timedelta(minutes=1),
        )

        call_command("send_connection_reminders")
        call_command("send_connection_reminders")

        user.refresh_from_db()
        self.assertIsNotNone(user.access_expired_notified_at)
        send_message.assert_called_once()

    @patch("webhook_tg.management.commands.send_connection_reminders.dispatch_telegram_request")
    def test_sends_due_reminder_once(self, dispatch_mock):
        dispatch_mock.return_value = (True, "")
        user = UserTg.objects.create(
            user_id=101,
            chat_id=101,
            last_start_at=timezone.now() - timedelta(hours=1),
            connection_reminder_at=timezone.now() - timedelta(minutes=30),
        )

        call_command("send_connection_reminders")
        call_command("send_connection_reminders")

        user.refresh_from_db()
        self.assertEqual(dispatch_mock.call_count, 1)
        self.assertEqual(
            dispatch_mock.call_args.args[2]["reply_markup"],
            CONNECTION_REMINDER_REPLY_MARKUP,
        )
        self.assertIsNone(user.connection_reminder_at)
        self.assertIsNotNone(user.connection_reminder_sent_at)

    @patch("webhook_tg.management.commands.send_connection_reminders.dispatch_telegram_request")
    def test_skips_connected_user(self, dispatch_mock):
        UserTg.objects.create(
            user_id=102,
            chat_id=102,
            business_is_connected=True,
            connection_reminder_at=timezone.now() - timedelta(minutes=1),
        )

        call_command("send_connection_reminders")

        dispatch_mock.assert_not_called()

    @patch("webhook_tg.management.commands.send_connection_reminders.dispatch_telegram_request")
    def test_does_not_retry_when_bot_is_blocked(self, dispatch_mock):
        dispatch_mock.return_value = (False, "Forbidden: bot was blocked by the user")
        user = UserTg.objects.create(
            user_id=103,
            chat_id=103,
            connection_reminder_at=timezone.now() - timedelta(minutes=1),
        )

        call_command("send_connection_reminders")

        user.refresh_from_db()
        self.assertIsNone(user.connection_reminder_at)
        self.assertIsNone(user.connection_reminder_sent_at)
