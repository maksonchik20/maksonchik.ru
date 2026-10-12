from contextlib import ExitStack
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.db import transaction
from django.test import SimpleTestCase, TestCase

from .incoming import enqueue_incoming_update
from .incoming_notifications import IDLE_POLL_SECONDS, IncomingQueueWakeup, notify_incoming_queue
from .models import TelegramIncomingUpdate


HELPER = "webhook_tg.incoming_notifications"
COMMAND = "webhook_tg.management.commands.process_telegram_updates"


class IncomingNotificationCommitTests(TestCase):
    def test_new_update_notifies_only_after_commit(self):
        with patch("webhook_tg.incoming.notify_incoming_queue") as notify:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                item, created = enqueue_incoming_update({"update_id": 920001, "message": {}})
                notify.assert_not_called()
            self.assertTrue(created)
            self.assertEqual(len(callbacks), 1)
            callbacks[0]()
        notify.assert_called_once_with(item.queue, using="default")

    def test_rolled_back_insert_does_not_notify(self):
        with patch("webhook_tg.incoming.notify_incoming_queue") as notify:
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                with self.assertRaises(RuntimeError):
                    with transaction.atomic():
                        enqueue_incoming_update({"update_id": 920002})
                        raise RuntimeError("rollback")
            self.assertEqual(callbacks, [])
        notify.assert_not_called()
        self.assertFalse(TelegramIncomingUpdate.objects.filter(update_id=920002).exists())

    def test_duplicate_update_does_not_notify_again(self):
        with patch("webhook_tg.incoming.notify_incoming_queue") as notify:
            with self.captureOnCommitCallbacks(execute=True):
                original, created = enqueue_incoming_update({"update_id": 920003})
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                duplicate, created = enqueue_incoming_update({"update_id": 920003})
        self.assertFalse(created)
        self.assertEqual(duplicate.pk, original.pk)
        self.assertEqual(callbacks, [])
        notify.assert_called_once()

    def test_notify_failure_does_not_lose_committed_queue_item(self):
        source = Mock(vendor="postgresql")
        source.cursor.side_effect = RuntimeError("connection unavailable")
        with (
            patch(f"{HELPER}.connections", {"default": source}),
            self.assertLogs(HELPER, level="WARNING"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            item, created = enqueue_incoming_update({"update_id": 920004})
        self.assertTrue(created)
        item.refresh_from_db()
        self.assertEqual(item.status, TelegramIncomingUpdate.Status.PENDING)


class IncomingQueueWakeupTests(SimpleTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.source = Mock(vendor="postgresql")
        self.listener = Mock()
        self.listener.settings_dict = {"OPTIONS": {"connect_timeout": 5, "pool": True}}
        self.listener.connection.closed = False
        self.source.copy.return_value = self.listener
        self.cursor = Mock()
        self.listener.cursor.return_value = Mock(
            __enter__=Mock(return_value=self.cursor), __exit__=Mock(return_value=False),
        )
        self.source.cursor.return_value = self.listener.cursor.return_value
        self.stack.enter_context(patch(f"{HELPER}.connections", {"default": self.source}))
        self.clock = self.stack.enter_context(patch(f"{HELPER}.time.monotonic", return_value=100))

    def test_notify_uses_only_queue_channel_and_no_payload(self):
        notify_incoming_queue("priority")
        self.cursor.execute.assert_called_once_with(
            "SELECT pg_notify(%s, %s)", ["who_update_incoming_priority", ""],
        )

    def test_sqlite_is_noop_and_uses_polling_fallback(self):
        self.source.vendor = "sqlite"
        notify_incoming_queue("business")
        wakeup = IncomingQueueWakeup("business")
        wakeup.prepare()
        self.assertFalse(wakeup.wait())
        self.source.cursor.assert_not_called()
        self.source.copy.assert_not_called()

    def test_listener_is_separate_autocommit_connection_and_is_reused(self):
        wakeup = IncomingQueueWakeup("priority")
        wakeup.prepare()
        wakeup.prepare()
        self.source.copy.assert_called_once_with(alias="default_incoming_priority")
        self.listener.ensure_connection.assert_called_once()
        self.listener.set_autocommit.assert_called_once_with(True)
        self.cursor.execute.assert_called_once_with("LISTEN who_update_incoming_priority")
        self.assertEqual(self.listener.settings_dict["OPTIONS"]["connect_timeout"], 2)
        self.assertNotIn("pool", self.listener.settings_dict["OPTIONS"])

    def test_notification_wakes_and_generator_is_closed(self):
        closed = []

        def notifications():
            try:
                yield SimpleNamespace(channel="who_update_incoming_business", payload="")
            finally:
                closed.append(True)

        self.listener.connection.notifies.return_value = notifications()
        wakeup = IncomingQueueWakeup("business")
        wakeup.prepare()
        self.assertTrue(wakeup.wait())
        self.listener.connection.notifies.assert_called_once_with(timeout=IDLE_POLL_SECONDS, stop_after=1)
        self.assertEqual(closed, [True])

    def test_lost_notification_still_returns_after_bounded_timeout(self):
        self.listener.connection.notifies.return_value = (item for item in ())
        wakeup = IncomingQueueWakeup("business")
        wakeup.prepare()
        self.assertTrue(wakeup.wait())
        self.listener.connection.notifies.assert_called_once_with(timeout=1.0, stop_after=1)

    def test_buffered_notifications_are_coalesced_in_one_wait(self):
        consumed = []

        def notifications():
            for number in range(10):
                consumed.append(number)
                yield SimpleNamespace(channel="who_update_incoming_business", payload="")

        self.listener.connection.notifies.return_value = notifications()
        wakeup = IncomingQueueWakeup("business")
        wakeup.prepare()
        self.assertTrue(wakeup.wait())
        self.assertEqual(consumed, list(range(10)))
        self.listener.connection.notifies.assert_called_once()

    def test_connection_failure_falls_back_and_reconnect_is_throttled(self):
        self.listener.ensure_connection.side_effect = RuntimeError("unavailable")
        wakeup = IncomingQueueWakeup("priority")
        with self.assertLogs(HELPER, level="WARNING"):
            wakeup.prepare()
        self.assertFalse(wakeup.wait())
        self.listener.close.assert_called_once()
        self.clock.return_value = 101
        wakeup.prepare()
        self.assertEqual(self.source.copy.call_count, 1)
        self.clock.return_value = 131
        self.listener.ensure_connection.side_effect = None
        wakeup.prepare()
        self.assertEqual(self.source.copy.call_count, 2)
        self.cursor.execute.assert_called_once_with("LISTEN who_update_incoming_priority")

    def test_disconnected_listener_falls_back_and_does_not_leak_credentials(self):
        wakeup = IncomingQueueWakeup("business")
        wakeup.prepare()
        self.listener.connection.notifies.side_effect = RuntimeError("password=private")
        with self.assertLogs(HELPER, level="WARNING") as captured:
            self.assertFalse(wakeup.wait())
        self.assertNotIn("private", " ".join(captured.output))
        self.listener.close.assert_called_once()

    def test_unsupported_driver_uses_polling_without_reopening_connections(self):
        self.listener.connection.notifies = []
        wakeup = IncomingQueueWakeup("business")
        wakeup.prepare()
        wakeup.prepare()
        self.assertFalse(wakeup.wait())
        self.source.copy.assert_called_once()
        self.listener.close.assert_called_once()

    def test_close_is_idempotent(self):
        wakeup = IncomingQueueWakeup("business")
        wakeup.prepare()
        wakeup.close()
        wakeup.close()
        self.listener.close.assert_called_once()


class StopWorker(Exception):
    pass


class IncomingWakeupWorkerTests(SimpleTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.wakeup_class = self.stack.enter_context(patch(f"{COMMAND}.IncomingQueueWakeup"))
        self.wakeup = self.wakeup_class.return_value
        self.stack.enter_context(patch(f"{COMMAND}.close_old_connections"))
        self.stack.enter_context(patch(f"{COMMAND}.record_heartbeat"))
        self.stack.enter_context(patch(f"{COMMAND}.recover_stale_updates", return_value=0))
        self.claim = self.stack.enter_context(patch(f"{COMMAND}.claim_next_update"))
        self.process = self.stack.enter_context(patch(f"{COMMAND}.process_claimed_update"))
        self.clock = self.stack.enter_context(patch(f"{COMMAND}.time"))
        self.clock.monotonic.return_value = 100

    def test_listen_precedes_queue_check_and_notify_causes_new_check(self):
        order = []
        self.wakeup.prepare.side_effect = lambda: order.append("listen")

        def claim(queue):
            order.append("query")
            if len(order) > 3:
                raise StopWorker
            return None

        self.claim.side_effect = claim
        self.wakeup.wait.return_value = True
        with self.assertRaises(StopWorker):
            call_command("process_telegram_updates", queue="priority", stdout=StringIO())
        self.assertEqual(order, ["listen", "query", "listen", "query"])
        self.wakeup.wait.assert_called_once_with(IDLE_POLL_SECONDS)
        self.clock.sleep.assert_not_called()
        self.wakeup.close.assert_called_once()

    def test_unavailable_listener_falls_back_to_one_second_polling(self):
        self.claim.side_effect = [None, StopWorker]
        self.wakeup.wait.return_value = False
        with self.assertRaises(StopWorker):
            call_command("process_telegram_updates", queue="business", stdout=StringIO())
        self.clock.sleep.assert_called_once_with(1.0)

    def test_busy_queue_does_not_wait_for_notifications(self):
        item = Mock()
        self.claim.side_effect = [item, StopWorker]
        with self.assertRaises(StopWorker):
            call_command("process_telegram_updates", queue="business", stdout=StringIO())
        self.process.assert_called_once_with(item)
        self.wakeup.wait.assert_not_called()
        self.clock.sleep.assert_not_called()

    def test_once_does_not_create_listener_or_sleep(self):
        self.claim.return_value = None
        call_command("process_telegram_updates", queue="business", once=True, stdout=StringIO())
        self.wakeup.prepare.assert_not_called()
        self.wakeup.wait.assert_not_called()
        self.wakeup.close.assert_called_once()
