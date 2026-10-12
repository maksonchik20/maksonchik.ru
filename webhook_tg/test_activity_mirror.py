import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, Mock, patch

import requests

from webhook_tg import activity_mirror
from webhook_tg.event_reporter import report_who_update_event


class ActivityMirrorTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state.json"
        self.at = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
        self.env = SimpleNamespace(WHO_UPDATE_EVENT_URL="https://example.invalid/event", WHO_UPDATE_EVENT_TOKEN="test")
        self.environment = patch.dict(sys.modules, {"env": self.env})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.message = patch.object(activity_mirror, "Message", MagicMock()).start()
        self.metric = patch.object(activity_mirror, "OperationalMetricBucket", MagicMock()).start()
        self.post = patch.object(activity_mirror.requests, "post", return_value=Mock(status_code=200)).start()
        self.log = patch.object(activity_mirror.logger, "exception").start()
        self.addCleanup(patch.stopall)
        self.message.objects.order_by.return_value.values_list.return_value.first.return_value = (100, self.at)
        self.message.objects.filter.return_value.order_by.return_value.values_list.return_value.__getitem__.return_value = []
        self.metric.objects.filter.return_value.order_by.return_value.values_list.return_value.first.return_value = None

    def state(self):
        return json.loads(self.path.read_text())

    def initialize(self):
        return activity_mirror.initialize_activity_mirror(state_path=self.path)

    def publish(self):
        return activity_mirror.publish_activity_mirror(state_path=self.path)

    def rows(self, *rows):
        self.message.objects.filter.return_value.order_by.return_value.values_list.return_value.__getitem__.return_value = rows

    def test_initialize_is_idempotent_and_never_sends(self):
        initial = self.initialize()
        self.message.objects.order_by.return_value.values_list.return_value.first.return_value = (200, self.at)
        self.assertEqual(self.initialize(), initial)
        self.assertEqual(self.state()["last_message_pk"], 100)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.post.assert_not_called()

    def test_uninitialized_cursor_fails_closed(self):
        self.assertFalse(self.publish())
        self.post.assert_not_called()
        self.message.objects.filter.assert_not_called()

    def test_success_advances_only_bounded_new_message_range(self):
        self.initialize()
        self.rows((101, self.at + timedelta(minutes=1)), (103, self.at + timedelta(minutes=2)))
        self.assertTrue(self.publish())
        fieldToPayload = self.post.call_args.kwargs["json"]
        self.assertEqual(fieldToPayload["new_archived_messages"], 2)
        self.assertEqual(fieldToPayload["first_message_pk"], 101)
        self.assertEqual(fieldToPayload["last_message_pk"], 103)
        self.assertEqual(self.state()["last_message_pk"], 103)
        self.assertIsNone(self.state()["pending"])
        self.message.objects.filter.assert_called_once_with(pk__gt=100)
        self.message.objects.filter.return_value.order_by.return_value.values_list.return_value.__getitem__.assert_called_once_with(slice(None, 5000, None))

    def test_timeout_retries_identical_persisted_payload_without_rereading(self):
        self.initialize()
        self.rows((101, self.at))
        self.post.side_effect = [requests.Timeout(), Mock(status_code=200)]
        self.assertFalse(self.publish())
        fieldToPending = self.state()["pending"]
        self.assertEqual(self.state()["last_message_pk"], 100)
        self.rows((102, self.at + timedelta(minutes=1)))
        self.assertTrue(self.publish())
        self.assertEqual(self.post.call_args_list[0].kwargs["json"], fieldToPending)
        self.assertEqual(self.post.call_args_list[1].kwargs["json"], fieldToPending)
        self.assertEqual(self.message.objects.filter.call_count, 1)
        self.assertEqual(self.state()["last_message_pk"], 101)

    def test_http_errors_and_redirects_do_not_acknowledge(self):
        self.initialize()
        for status in (302, 403, 409, 500):
            self.post.return_value.status_code = status
            self.assertFalse(self.publish())
            self.assertEqual(self.state()["last_message_pk"], 100)
            self.assertIsNotNone(self.state()["pending"])
        self.assertFalse(self.post.call_args.kwargs["allow_redirects"])

    def test_commit_failure_after_http_replays_same_batch_key(self):
        self.initialize()
        original_save = activity_mirror._save_state
        saves = 0

        def fail_ack(path, fieldToState):
            nonlocal saves
            saves += 1
            if saves == 2:
                raise OSError("disk temporarily full")
            original_save(path, fieldToState)

        with patch.object(activity_mirror, "_save_state", side_effect=fail_ack):
            self.assertFalse(self.publish())
        key = self.state()["pending"]["batch_key"]
        self.assertTrue(self.publish())
        self.assertEqual(self.post.call_args.kwargs["json"]["batch_key"], key)

    def test_edit_only_activity_uses_durable_metric_time(self):
        self.initialize()
        at = self.at + timedelta(minutes=10)
        self.metric.objects.filter.return_value.order_by.return_value.values_list.return_value.first.return_value = at
        self.assertTrue(self.publish())
        fieldToPayload = self.post.call_args.kwargs["json"]
        self.assertEqual(fieldToPayload["new_archived_messages"], 0)
        self.assertIsNone(fieldToPayload["first_message_pk"])
        self.assertEqual(fieldToPayload["last_event_at"], at.isoformat())

    def test_idle_heartbeat_does_not_invent_current_activity(self):
        self.initialize()
        self.assertTrue(self.publish())
        self.assertEqual(self.post.call_args.kwargs["json"]["last_event_at"], self.at.isoformat())

    def test_parallel_exporter_skips_locked_cursor(self):
        self.initialize()
        with activity_mirror._locked_state_file(self.path):
            self.assertFalse(self.publish())
        self.post.assert_not_called()

    def test_database_failure_does_not_break_metrics_or_advance_cursor(self):
        self.initialize()
        self.message.objects.filter.side_effect = RuntimeError("database unavailable")
        self.assertFalse(self.publish())
        self.assertEqual(self.state()["last_message_pk"], 100)
        self.post.assert_not_called()

    def test_disabled_connection_performs_no_io(self):
        self.env.WHO_UPDATE_EVENT_URL = ""
        self.assertFalse(self.publish())
        self.message.objects.filter.assert_not_called()
        self.post.assert_not_called()

    def test_compatibility_hook_does_not_send_or_touch_models(self):
        report_who_update_event(chat_id=1, message_id=2)
        self.message.objects.filter.assert_not_called()
        self.post.assert_not_called()
