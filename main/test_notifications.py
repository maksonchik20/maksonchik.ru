from datetime import timedelta
from unittest.mock import patch

from django.db import transaction
from django.test import TestCase
from django.utils import timezone

from webhook_tg.models import TelegramOutbox
from webhook_tg.outbox import deliver_outbox_item, process_outbox

from .models import Lead
from .notifications import enqueue_lead_notification


@patch("main.notifications.OWNER_CHAT_ID", 123456)
class LeadNotificationTests(TestCase):
    def make_lead(self):
        return Lead.objects.create(
            name="Анна <test>",
            contact="@anna",
            message="Нужен сайт & бот",
            notification_error="Telegram API не подтвердил отправку",
        )

    def test_queue_is_idempotent_and_escapes_user_text(self):
        lead = self.make_lead()
        first = enqueue_lead_notification(lead)
        second = enqueue_lead_notification(Lead.objects.get(pk=lead.pk))
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(TelegramOutbox.objects.count(), 1)
        self.assertEqual(first.idempotency_key, f"lead:{lead.pk}")
        self.assertEqual(first.chat_id, 123456)
        self.assertIn("Анна &lt;test&gt;", first.payload["text"])
        self.assertIn("Нужен сайт &amp; бот", first.payload["text"])
        self.assertIn(f"/admin/main/lead/{lead.pk}/change/", first.payload["text"])
        lead.refresh_from_db()
        self.assertFalse(lead.notification_sent)
        self.assertEqual(lead.notification_error, "")

    @patch("main.notifications.enqueue_outbox", return_value=None)
    def test_queue_failure_rolls_back_form_submission(self, enqueue):
        with self.assertRaisesMessage(RuntimeError, "Failed to persist notification"):
            self.client.post("/request/", {"contact": "@anna", "consent": "on"})
        self.assertFalse(Lead.objects.exists())
        self.assertFalse(TelegramOutbox.objects.exists())

    def test_rollback_does_not_leave_orphan_notification(self):
        with self.assertRaisesMessage(RuntimeError, "rollback"):
            with transaction.atomic():
                enqueue_lead_notification(self.make_lead())
                raise RuntimeError("rollback")
        self.assertFalse(Lead.objects.exists())
        self.assertFalse(TelegramOutbox.objects.exists())

    @patch("webhook_tg.outbox.dispatch_telegram_request")
    def test_network_failure_is_retried_and_delivery_updates_lead(self, dispatch):
        lead = self.make_lead()
        item = enqueue_lead_notification(lead)
        dispatch.return_value = (False, "ConnectTimeout: Telegram unavailable")
        self.assertEqual(deliver_outbox_item(item.pk), "failed")
        lead.refresh_from_db()
        item.refresh_from_db()
        self.assertFalse(lead.notification_sent)
        self.assertEqual(lead.notification_error, "ConnectTimeout: Telegram unavailable")
        self.assertEqual(item.last_error, lead.notification_error)
        self.assertEqual(item.status, "pending")
        self.assertEqual(item.attempts, 1)
        self.assertGreater(item.next_attempt_at, timezone.now())
        self.assertEqual(deliver_outbox_item(item.pk), "skipped")

        # A new worker reads the persisted record, without any in-memory callback.
        TelegramOutbox.objects.filter(pk=item.pk).update(next_attempt_at=timezone.now())
        dispatch.return_value = (True, "")
        self.assertEqual(process_outbox()["sent"], 1)
        lead.refresh_from_db()
        item.refresh_from_db()
        self.assertTrue(lead.notification_sent)
        self.assertEqual(lead.notification_error, "")
        self.assertEqual(item.status, "sent")
        self.assertIsNotNone(item.sent_at)
        self.assertEqual(enqueue_lead_notification(lead).pk, item.pk)
        self.assertEqual(deliver_outbox_item(item.pk), "skipped")
        self.assertEqual(dispatch.call_count, 2)

    @patch("webhook_tg.outbox.dispatch_telegram_request", return_value=(False, "Forbidden: bot was blocked by the user"))
    def test_permanent_failure_is_not_retried(self, dispatch):
        lead = self.make_lead()
        item = enqueue_lead_notification(lead)
        self.assertEqual(deliver_outbox_item(item.pk), "dropped")
        lead.refresh_from_db()
        item.refresh_from_db()
        self.assertFalse(lead.notification_sent)
        self.assertIn("bot was blocked", lead.notification_error)
        self.assertEqual(item.status, "dropped")
        self.assertEqual(enqueue_lead_notification(lead).pk, item.pk)
        self.assertEqual(deliver_outbox_item(item.pk), "skipped")
        dispatch.assert_called_once()

    def test_legacy_delivered_lead_is_not_sent_again(self):
        lead = Lead.objects.create(contact="@anna", notification_sent=True)
        self.assertIsNone(enqueue_lead_notification(lead))
        self.assertFalse(TelegramOutbox.objects.exists())

    @patch("webhook_tg.outbox.dispatch_telegram_request", return_value=(True, ""))
    def test_existing_lease_is_respected_and_recovers_after_expiry(self, dispatch):
        lead = self.make_lead()
        item = enqueue_lead_notification(lead)
        TelegramOutbox.objects.filter(pk=item.pk).update(
            next_attempt_at=timezone.now() + timedelta(seconds=120),
        )
        self.assertEqual(deliver_outbox_item(item.pk), "skipped")
        dispatch.assert_not_called()
        TelegramOutbox.objects.filter(pk=item.pk).update(next_attempt_at=timezone.now())
        self.assertEqual(deliver_outbox_item(item.pk), "sent")
        lead.refresh_from_db()
        self.assertTrue(lead.notification_sent)

    @patch("webhook_tg.outbox.dispatch_telegram_request", return_value=(True, ""))
    def test_long_lead_is_split_and_all_parts_delivered(self, dispatch):
        lead = Lead.objects.create(
            contact="@anna",
            message="Задача " * 428,
            page_url="https://maksonchik.ru/?" + "q" * 975,
            utm_campaign="c" * 255,
        )
        item = enqueue_lead_notification(lead)
        self.assertEqual(deliver_outbox_item(item.pk), "sent")
        self.assertGreater(dispatch.call_count, 1)
        lead.refresh_from_db()
        self.assertTrue(lead.notification_sent)
