from unittest.mock import patch

from django.test import SimpleTestCase

from .config import OWNER_CHAT_ID
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

    @patch("webhook_tg.views.tg_send_message")
    @patch("webhook_tg.views.collect_resource_snapshot")
    def test_owner_receives_metric_report(self, collect_mock, send_mock):
        collect_mock.return_value = self.snapshot

        handled = _handle_metric_command(OWNER_CHAT_ID, int(OWNER_CHAT_ID), "/metric")

        self.assertTrue(handled)
        send_mock.assert_called_once_with(
            OWNER_CHAT_ID,
            resource_report_text(self.snapshot),
        )

    @patch("webhook_tg.views.tg_send_message")
    @patch("webhook_tg.views.collect_resource_snapshot")
    def test_other_user_gets_no_metrics(self, collect_mock, send_mock):
        handled = _handle_metric_command(123456, 123456, "/metric")

        self.assertTrue(handled)
        collect_mock.assert_not_called()
        send_mock.assert_not_called()

    @patch("webhook_tg.views.tg_send_message")
    def test_other_command_is_not_consumed(self, send_mock):
        self.assertFalse(_handle_metric_command(OWNER_CHAT_ID, int(OWNER_CHAT_ID), "/status"))
        send_mock.assert_not_called()
