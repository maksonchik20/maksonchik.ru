from datetime import timedelta
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from .metrics import (
    SYSTEM_MEMORY_AVAILABLE,
    SYSTEM_MEMORY_USED,
    TELEGRAM_MESSAGES_SENT,
    TELEGRAM_SEND_DURATION,
    WEBHOOK_DURATION,
    observe_metric,
)
from .models import OperationalMetricBucket
from .resource_metrics import ResourceSnapshot
from .telegram import _telegram_post


class OperationalMetricsTests(TestCase):
    @patch("webhook_tg.metric_snapshots.collect_resource_snapshot")
    def test_system_memory_gauges_are_collected(self, collect_mock):
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

        from .metric_snapshots import collect_system_gauges

        collect_system_gauges()

        used = OperationalMetricBucket.objects.get(metric_name=SYSTEM_MEMORY_USED)
        available = OperationalMetricBucket.objects.get(metric_name=SYSTEM_MEMORY_AVAILABLE)
        self.assertEqual(used.total, 63)
        self.assertEqual(available.total, 1024**3)
        collect_mock.assert_called_once_with(cpu_interval=0)

    def test_observations_are_aggregated_per_minute(self):
        observe_metric(WEBHOOK_DURATION, 10, {"status": 200})
        observe_metric(WEBHOOK_DURATION, 30, {"status": 200})

        bucket = OperationalMetricBucket.objects.get(metric_name=WEBHOOK_DURATION)
        self.assertEqual(bucket.count, 2)
        self.assertEqual(bucket.total, 40)
        self.assertEqual(bucket.maximum, 30)
        self.assertEqual(bucket.labels, {"status": "200"})

    @patch("webhook_tg.telegram.TELEGRAM_SESSION.post")
    def test_successful_telegram_send_records_duration_and_count(self, post):
        response = Mock()
        response.json.return_value = {"ok": True}
        post.return_value = response

        self.assertIs(_telegram_post("sendMessage", json={"chat_id": 1}), response)

        self.assertTrue(
            OperationalMetricBucket.objects.filter(metric_name=TELEGRAM_SEND_DURATION).exists()
        )
        sent = OperationalMetricBucket.objects.get(metric_name=TELEGRAM_MESSAGES_SENT)
        self.assertEqual(sent.count, 1)

    @patch("webhook_tg.management.commands.export_operational_metrics.requests.post")
    @patch("webhook_tg.management.commands.export_operational_metrics.requests.get")
    def test_export_marks_buckets_only_after_success(self, get, post):
        token_response = Mock()
        token_response.json.return_value = {"access_token": "test-token"}
        token_response.raise_for_status.return_value = None
        get.return_value = token_response
        write_response = Mock()
        write_response.raise_for_status.return_value = None
        post.return_value = write_response
        bucket = OperationalMetricBucket.objects.create(
            metric_name=TELEGRAM_MESSAGES_SENT,
            minute=timezone.now().replace(second=0, microsecond=0) - timedelta(minutes=1),
            labels_hash="hash",
            labels={"method": "sendMessage"},
            count=3,
            total=3,
            maximum=1,
        )

        call_command("export_operational_metrics")

        bucket.refresh_from_db()
        self.assertIsNotNone(bucket.exported_at)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["metrics"][0]["value"], 3)
        self.assertEqual(payload["metrics"][0]["name"], TELEGRAM_MESSAGES_SENT)

    @override_settings(TELEGRAM_WEBHOOK_SECRET_REQUIRED=False)
    def test_webhook_request_records_duration(self):
        response = self.client.post(
            "/webhook_tg/",
            data="{}",
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        bucket = OperationalMetricBucket.objects.get(metric_name=WEBHOOK_DURATION)
        self.assertEqual(bucket.labels["status"], "400")
