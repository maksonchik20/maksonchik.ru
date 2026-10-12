from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import Mock, patch

from django.db import OperationalError, connection, transaction
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from . import metric_buffer
from .metric_snapshots import collect_database_gauges
from .metrics import (
    PAYMENT_EVENTS, TELEGRAM_SEND_DURATION, INCOMING_UPDATES_RECEIVED,
    ONBOARDING_FUNNEL_TOTAL, ONBOARDING_MILESTONES_TOTAL, OUTBOX_DUE,
    OUTBOX_SIZE, closed_bucket_cutoff, observe_metric, set_gauges,
)
from .models import OperationalMetricBucket, TelegramOutbox, UserTg, WhoUpdateOnboardingFunnel


@override_settings(OPERATIONAL_METRICS_BATCH_SECONDS=5)
class MetricBufferTests(TestCase):
    def setUp(self):
        metric_buffer._reset_after_fork()
        metric_buffer._thread = Mock()

    def tearDown(self):
        metric_buffer._reset_after_fork()

    def observe(self, name, value, labelToValue=None):
        with self.captureOnCommitCallbacks(execute=True):
            observe_metric(name, value, labelToValue)

    def test_many_observations_use_one_upsert_and_preserve_statistics(self):
        with self.assertNumQueries(0):
            for value in range(100):
                self.observe(TELEGRAM_SEND_DURATION, value, {"method": "sendMessage"})
        with self.assertNumQueries(1):
            metric_buffer.flush_metric_buffer()
        item = OperationalMetricBucket.objects.get()
        self.assertEqual((item.count, item.total, item.maximum), (100, 4950, 99))
        self.assertEqual(item.labels, {"method": "sendMessage"})
        self.observe(TELEGRAM_SEND_DURATION, 101, {"method": "sendMessage"})
        metric_buffer.flush_metric_buffer()
        item.refresh_from_db()
        self.assertEqual((item.count, item.total, item.maximum), (101, 5051, 101))

    def test_business_metrics_are_immediate_and_not_buffered(self):
        observe_metric(PAYMENT_EVENTS, 1)
        self.assertEqual(OperationalMetricBucket.objects.get().metric_name, PAYMENT_EVENTS)
        self.assertFalse(metric_buffer.keyToObservation)

    def test_rollback_does_not_create_buffered_observation(self):
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    observe_metric(INCOMING_UPDATES_RECEIVED, 1)
                    raise ValueError("rollback")
            except ValueError:
                pass
        self.assertFalse(metric_buffer.keyToObservation)

    def test_failed_flush_retains_data_for_retry(self):
        self.observe(INCOMING_UPDATES_RECEIVED, 1)
        with patch.object(metric_buffer, "_persist", side_effect=OperationalError("temporary")):
            metric_buffer.flush_metric_buffer()
        self.assertEqual(len(metric_buffer.keyToObservation), 1)
        metric_buffer.flush_metric_buffer()
        self.assertEqual(OperationalMetricBucket.objects.get().count, 1)
        self.assertFalse(metric_buffer.keyToObservation)

    def test_observation_arriving_during_failed_flush_is_merged(self):
        self.observe(INCOMING_UPDATES_RECEIVED, 1)
        def fail(observations):
            self.observe(INCOMING_UPDATES_RECEIVED, 1)
            raise OperationalError("temporary")
        with patch.object(metric_buffer, "_persist", side_effect=fail):
            metric_buffer.flush_metric_buffer()
        metric_buffer.flush_metric_buffer()
        self.assertEqual(OperationalMetricBucket.objects.get().count, 2)

    def test_exported_bucket_is_never_mutated(self):
        self.observe(INCOMING_UPDATES_RECEIVED, 1)
        metric_buffer.flush_metric_buffer()
        OperationalMetricBucket.objects.update(exported_at=timezone.now())
        self.observe(INCOMING_UPDATES_RECEIVED, 1)
        metric_buffer.flush_metric_buffer()
        self.assertEqual(OperationalMetricBucket.objects.get().count, 1)

    def test_minute_boundary_remains_separate(self):
        minute = timezone.now().replace(second=0, microsecond=0)
        with patch.object(metric_buffer.timezone, "now", return_value=minute):
            self.observe(INCOMING_UPDATES_RECEIVED, 1)
        with patch.object(metric_buffer.timezone, "now", return_value=minute + timedelta(minutes=1)):
            self.observe(INCOMING_UPDATES_RECEIVED, 1)
        metric_buffer.flush_metric_buffer()
        self.assertEqual(OperationalMetricBucket.objects.count(), 2)

    def test_buffer_is_bounded(self):
        with patch.object(metric_buffer, "MAX_BUFFER_KEYS", 2):
            for label in ("a", "b", "c"):
                self.observe(INCOMING_UPDATES_RECEIVED, 1, {"label": label})
        self.assertEqual(len(metric_buffer.keyToObservation), 2)

    def test_producers_do_not_lose_concurrent_counts(self):
        minute = timezone.now().replace(second=0, microsecond=0)
        def add(_):
            metric_buffer._add(metric_buffer.Observation(INCOMING_UPDATES_RECEIVED, minute, "hash", {}, 1, 1, 1), 5)
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(add, range(200)))
        metric_buffer.flush_metric_buffer()
        self.assertEqual(OperationalMetricBucket.objects.get().count, 200)

    def test_export_waits_for_buffer_grace(self):
        now = timezone.now().replace(second=0, microsecond=0)
        with patch("webhook_tg.metrics._current_minute", return_value=now):
            self.assertLess(closed_bucket_cutoff(), now - timedelta(minutes=1))

    def test_flusher_start_failure_never_breaks_committed_business_work(self):
        metric_buffer._thread = None
        with patch.object(metric_buffer.threading, "Thread") as thread:
            thread.return_value.start.side_effect = RuntimeError("can't start new thread")
            self.observe(INCOMING_UPDATES_RECEIVED, 1)
        self.assertFalse(metric_buffer.keyToObservation)


class GaugeSnapshotTests(TestCase):
    def test_bulk_gauges_overwrite_instead_of_accumulating(self):
        set_gauges([("test", 1, {"a": 2}), ("test", 3, {"a": 2})])
        set_gauges([("test", 5, {"a": 2})])
        item = OperationalMetricBucket.objects.get()
        self.assertEqual((item.count, item.total, item.maximum), (1, 5, 5))
        self.assertEqual(item.labels, {"a": "2"})

    def test_snapshot_values_and_query_bound(self):
        now = timezone.now()
        UserTg.objects.create(user_id=100, chat_id=100, last_start_at=now, business_is_connected=True)
        WhoUpdateOnboardingFunnel.objects.create(landing_viewed_at=now)
        WhoUpdateOnboardingFunnel.objects.create(landing_viewed_at=now, landing_path="/bot/checkout/")
        WhoUpdateOnboardingFunnel.objects.create(telegram_started_at=now, first_reminder_sent_at=now)
        WhoUpdateOnboardingFunnel.objects.create(telegram_started_at=now, connected_at=now, connection_stage="immediate")
        TelegramOutbox.objects.create(chat_id=100, method="sendMessage", payload={}, next_attempt_at=now)
        TelegramOutbox.objects.create(chat_id=100, method="sendMessage", payload={}, next_attempt_at=now + timedelta(hours=1))
        with CaptureQueriesContext(connection) as queries:
            collect_database_gauges()
        selects = [item["sql"] for item in queries.captured_queries if item["sql"].lstrip().upper().startswith("SELECT")]
        self.assertEqual(len(selects), 6)
        self.assertLessEqual(len(queries), 9)
        self.assertEqual(OperationalMetricBucket.objects.get(metric_name=OUTBOX_SIZE).total, 2)
        self.assertEqual(OperationalMetricBucket.objects.get(metric_name=OUTBOX_DUE).total, 1)
        self.assertEqual(OperationalMetricBucket.objects.get(metric_name=ONBOARDING_FUNNEL_TOTAL, labels__stage="landing_only").total, 1)
        self.assertEqual(OperationalMetricBucket.objects.get(metric_name=ONBOARDING_FUNNEL_TOTAL, labels__stage="after_first_reminder").total, 1)
        self.assertEqual(OperationalMetricBucket.objects.get(metric_name=ONBOARDING_MILESTONES_TOTAL, labels__milestone="connected").total, 1)
