from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from .config import OWNER_CHAT_ID
from .incoming import process_claimed_update
from .models import TelegramIncomingUpdate, TelegramOutbox, WebhookUpdate
from .outbox import deliver_outbox_item
from .resource_metrics import ResourceSnapshot, resource_report_text
from .views import _handle_metric_command


class MetricCommandTests(SimpleTestCase):
    def setUp(self):
        self.snapshot = ResourceSnapshot(
            disk_path="/",
            disk_used_pct=12,
            disk_free=50 * 1024**3,
            disk_total=100 * 1024**3,
            memory_used_pct=63,
            memory_available=1 * 1024**3,
            memory_total=2 * 1024**3,
            cpu_pct=17,
        )

    @patch("webhook_tg.views.send_message_reliably")
    @patch("webhook_tg.views.collect_resource_snapshot")
    def test_owner_receives_metric_report(self, collect_mock, send_mock):
        collect_mock.return_value = self.snapshot

        handled = _handle_metric_command(OWNER_CHAT_ID, int(OWNER_CHAT_ID), "/metric", update_id=9001)

        self.assertTrue(handled)
        send_mock.assert_called_once_with(
            OWNER_CHAT_ID,
            resource_report_text(self.snapshot),
            idempotency_key="command:9001:metric",
        )

    @patch("webhook_tg.views.send_message_reliably")
    @patch("webhook_tg.views.collect_resource_snapshot")
    def test_other_user_gets_no_metrics(self, collect_mock, send_mock):
        handled = _handle_metric_command(123456, 123456, "/metric", update_id=9001)

        self.assertTrue(handled)
        collect_mock.assert_not_called()
        send_mock.assert_not_called()

    @patch("webhook_tg.views.send_message_reliably")
    def test_other_command_is_not_consumed(self, send_mock):
        self.assertFalse(_handle_metric_command(OWNER_CHAT_ID, int(OWNER_CHAT_ID), "/status", update_id=9001))
        send_mock.assert_not_called()


class MetricCommandDeliveryTests(TestCase):
    def setUp(self):
        self.snapshot = ResourceSnapshot("/", 12, 50 * 1024**3, 100 * 1024**3, 63, 1024**3, 2 * 1024**3, 17)
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected network call"))
        network.start()
        self.addCleanup(network.stop)
        collector = patch("webhook_tg.views.collect_resource_snapshot", return_value=self.snapshot)
        collector.start()
        self.addCleanup(collector.stop)

    def make_incoming(self):
        owner_id = int(OWNER_CHAT_ID)
        return TelegramIncomingUpdate.objects.create(
            update_id=9001,
            queue=TelegramIncomingUpdate.Queue.PRIORITY,
            status=TelegramIncomingUpdate.Status.PROCESSING,
            next_attempt_at=timezone.now(),
            payload={
                "update_id": 9001,
                "message": {
                    "message_id": 42,
                    "date": int(timezone.now().timestamp()),
                    "text": "/metric",
                    "from": {"id": owner_id, "first_name": "Owner"},
                    "chat": {"id": owner_id, "type": "private"},
                },
            },
        )

    def test_transport_failure_keeps_reply_queued_after_incoming_is_done(self):
        incoming = self.make_incoming()
        with patch("webhook_tg.outbox.dispatch_telegram_request", return_value=(False, "Connection timed out")):
            self.assertTrue(process_claimed_update(incoming))

        incoming.refresh_from_db()
        self.assertEqual(incoming.status, TelegramIncomingUpdate.Status.DONE)
        self.assertTrue(WebhookUpdate.objects.filter(update_id=9001).exists())
        reply = TelegramOutbox.objects.get(idempotency_key="command:9001:metric")
        self.assertEqual(reply.status, TelegramOutbox.Status.PENDING)
        self.assertEqual(reply.payload["text"], resource_report_text(self.snapshot))

        TelegramOutbox.objects.filter(pk=reply.pk).update(next_attempt_at=timezone.now())
        with patch("webhook_tg.outbox.dispatch_telegram_request", return_value=(True, "")) as send:
            self.assertEqual(deliver_outbox_item(reply.pk), "sent")
        send.assert_called_once()
        reply.refresh_from_db()
        self.assertEqual(reply.status, TelegramOutbox.Status.SENT)

    def test_same_update_does_not_send_a_second_reply(self):
        with patch("webhook_tg.outbox.dispatch_telegram_request", return_value=(True, "")) as send:
            for _ in range(2):
                _handle_metric_command(OWNER_CHAT_ID, int(OWNER_CHAT_ID), "/metric", update_id=9001)
        send.assert_called_once()
        self.assertEqual(TelegramOutbox.objects.filter(idempotency_key="command:9001:metric").count(), 1)

    def test_persist_failure_does_not_mark_incoming_done(self):
        incoming = self.make_incoming()
        with (
            patch("webhook_tg.outbox.enqueue_outbox", side_effect=RuntimeError("Test storage failure")),
            self.assertLogs("webhook_tg.incoming", level="ERROR"),
        ):
            self.assertFalse(process_claimed_update(incoming))
        incoming.refresh_from_db()
        self.assertEqual(incoming.status, TelegramIncomingUpdate.Status.PENDING)
        self.assertFalse(WebhookUpdate.objects.filter(update_id=9001).exists())

    def test_new_command_gets_a_new_reply(self):
        with patch("webhook_tg.outbox.dispatch_telegram_request", return_value=(True, "")) as send:
            _handle_metric_command(OWNER_CHAT_ID, int(OWNER_CHAT_ID), "/metric", update_id=9001)
            _handle_metric_command(OWNER_CHAT_ID, int(OWNER_CHAT_ID), "/metric", update_id=9002)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(TelegramOutbox.objects.count(), 2)


class DailyResourceReportTests(SimpleTestCase):
    @patch("webhook_tg.management.commands.check_resources.enqueue_outbox")
    @patch("webhook_tg.management.commands.check_resources.collect_resource_snapshot")
    def test_daily_report_is_queued_with_date_idempotency_key(self, collect_mock, enqueue_mock):
        collect_mock.return_value = ResourceSnapshot(
            disk_path="/",
            disk_used_pct=12,
            disk_free=50 * 1024**3,
            disk_total=100 * 1024**3,
            memory_used_pct=63,
            memory_available=1 * 1024**3,
            memory_total=2 * 1024**3,
            cpu_pct=17,
        )

        call_command("check_resources", "--report", "--daily")

        kwargs = enqueue_mock.call_args.kwargs
        self.assertRegex(kwargs["idempotency_key"], r"^daily-resource-report:\d{4}-\d{2}-\d{2}$")
        self.assertIn("RAM", kwargs["payload"]["text"])
        self.assertIn("CPU", kwargs["payload"]["text"])
