from datetime import timedelta
from unittest.mock import patch

from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from .config import BOT_ACTIVATED_TEXT
from .models import Message, TelegramOutbox, UserTg, WhoUpdateFirstObservedValue, WhoUpdateUserAcquisition
from .outbox import deliver_outbox_item, enqueue_outbox
from .useful_delivery import record_first_observed_value
from .views import _handle_business_connection_update, _send_deleted_notifications
from .inner_models.BusinessConnection import BusinessConnection


class FirstObservedValueTests(TestCase):
    def setUp(self):
        self.user = UserTg.objects.create(user_id=101, chat_id=101)

    def item(self, key="edit:202:123:hash", chat_id=101):
        return enqueue_outbox(
            chat_id=chat_id, method=TelegramOutbox.Method.SEND_MESSAGE,
            payload={"text": "Private edit notification"}, idempotency_key=key,
        )

    @patch("webhook_tg.outbox._dispatch_outbox_item", return_value=(True, ""))
    def test_success_is_recorded_once_after_delivery_not_enqueue(self, dispatch):
        item = self.item()
        self.assertFalse(WhoUpdateFirstObservedValue.objects.exists())
        self.assertEqual(deliver_outbox_item(item.pk), "sent")
        self.assertEqual(deliver_outbox_item(item.pk), "skipped")
        first = WhoUpdateFirstObservedValue.objects.get(user=self.user)
        self.assertEqual(first.event_type, "edit")
        self.assertEqual(first.outbox_id, item.pk)
        self.assertIsNotNone(first.observed_at)
        dispatch.assert_called_once()

    def test_failure_partial_delivery_and_blocking_do_not_count(self):
        for index, error in enumerate(("timeout", "partial delivery failed", "Forbidden: bot was blocked by the user")):
            item = self.item(key=f"edit:202:{index}:hash")
            with patch("webhook_tg.outbox._dispatch_outbox_item", return_value=(False, error)):
                result = deliver_outbox_item(item.pk)
            self.assertIn(result, ("failed", "dropped"))
        self.assertFalse(WhoUpdateFirstObservedValue.objects.exists())

    @patch("webhook_tg.outbox._dispatch_outbox_item", return_value=(True, ""))
    def test_commands_and_unknown_or_ambiguous_recipients_do_not_count(self, dispatch):
        for key, chat_id in (("command:1:start-photo", 101), ("edit:2:3:4", 999)):
            deliver_outbox_item(self.item(key=key, chat_id=chat_id).pk)
        UserTg.objects.create(user_id=202, chat_id=101)
        deliver_outbox_item(self.item(key="edit:5:6:7").pk)
        self.assertFalse(WhoUpdateFirstObservedValue.objects.exists())

    @patch("webhook_tg.outbox._dispatch_outbox_item", return_value=(True, ""))
    def test_analytics_error_never_rolls_back_sent_or_retries_success(self, dispatch):
        item = self.item()
        with patch("webhook_tg.useful_delivery.record_first_observed_value", side_effect=IntegrityError("analytics unavailable")):
            self.assertEqual(deliver_outbox_item(item.pk), "sent")
        item.refresh_from_db()
        self.assertEqual(item.status, TelegramOutbox.Status.SENT)
        self.assertEqual(deliver_outbox_item(item.pk), "skipped")
        dispatch.assert_called_once()

    @patch("webhook_tg.outbox._dispatch_outbox_item", return_value=(True, ""))
    def test_first_observation_is_not_replaced_by_later_delivery(self, dispatch):
        first_item = self.item()
        deliver_outbox_item(first_item.pk)
        first = WhoUpdateFirstObservedValue.objects.get(user=self.user)
        first_time = first.observed_at
        second_item = self.item(key="edit:2:3:5")
        deliver_outbox_item(second_item.pk)
        first.refresh_from_db()
        self.assertEqual(first.outbox_id, first_item.pk)
        self.assertEqual(first.observed_at, first_time)

    def test_helper_rejects_pending_and_uses_stored_confirmed_time(self):
        item = self.item()
        now = timezone.now()
        record_first_observed_value(item, sent_at=now)
        self.assertFalse(WhoUpdateFirstObservedValue.objects.exists())
        TelegramOutbox.objects.filter(pk=item.pk).update(status="sent", sent_at=now)
        record_first_observed_value(item, sent_at=now)
        record_first_observed_value(item, sent_at=now - timedelta(seconds=1))
        first = WhoUpdateFirstObservedValue.objects.get(user=self.user)
        self.assertEqual(first.observed_at, now)

    @patch("webhook_tg.outbox.dispatch_telegram_request")
    def test_only_successful_known_deleted_text_is_observed(self, send):
        Message.objects.create(
            message_id=10, chat_id=202, business_connection_id="conn-101",
            payload='{"from": {"id": 202}}', text="Known private text",
        )
        business = BusinessConnection(user_id=101, user_chat_id=101)
        payloadToValue = {"business_connection_id": "conn-101", "chat": {"id": 202}, "message_ids": [10]}
        send.return_value = (False, "Connection timeout")
        _send_deleted_notifications(payloadToValue, business)
        self.assertFalse(WhoUpdateFirstObservedValue.objects.exists())
        send.return_value = (True, "")
        _send_deleted_notifications({**payloadToValue, "message_ids": [999]}, business)
        self.assertFalse(WhoUpdateFirstObservedValue.objects.exists())
        _send_deleted_notifications(payloadToValue, business)
        item = TelegramOutbox.objects.get(delivery_context__event_type="delete_text")
        TelegramOutbox.objects.filter(pk=item.pk).update(next_attempt_at=timezone.now())
        deliver_outbox_item(item.pk)
        first = WhoUpdateFirstObservedValue.objects.get(user=self.user)
        self.assertEqual(first.event_type, "delete_text")
        self.assertEqual(first.outbox_id, item.pk)

    @patch("webhook_tg.outbox.dispatch_telegram_request", return_value=(True, ""))
    def test_legacy_reconnection_guard_uses_state_before_handler_overwrites_it(self, send):
        self.user.business_is_connected = True
        self.user.business_connection_id = "old-connection"
        self.user.business_connected_at = timezone.now() - timedelta(days=10)
        self.user.save()
        _handle_business_connection_update({
            "id": "new-connection", "is_enabled": True, "user_chat_id": 101,
            "user": {"id": 101, "username": "example", "first_name": "Example"},
        })
        acquisition = WhoUpdateUserAcquisition.objects.get(user=self.user)
        self.assertTrue(acquisition.has_connection_history)
        self.assertIsNone(acquisition.first_connected_at)
        self.assertIsNone(acquisition.first_connection_funnel_id)

    def test_activation_text_explains_consensual_new_message_check(self):
        self.assertIn("согласен на проверку", BOT_ACTIVATED_TEXT)
        self.assertIn("после подключения", BOT_ACTIVATED_TEXT)
        self.assertIn("удалит его у обоих", BOT_ACTIVATED_TEXT)
        self.assertIn("/status", BOT_ACTIVATED_TEXT)
