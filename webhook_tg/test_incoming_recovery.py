from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch
import requests

from django.core.management import call_command
from django.test import TransactionTestCase
from django.utils import timezone

from .incoming import (
    MAX_ATTEMPTS,
    STALE_AFTER,
    claim_next_update,
    process_claimed_update,
    recover_stale_updates,
)
from .models import TelegramIncomingUpdate, WebhookUpdate


COMMAND = "webhook_tg.management.commands.process_telegram_updates"


class StopWorker(Exception):
    pass


class IncomingRecoveryTests(TransactionTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.next_update_id = 910000
        network = patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("Network must not be used in queue tests"),
        )
        network.start()
        self.addCleanup(network.stop)

    def make_item(self, **fieldToValue):
        self.next_update_id += 1
        defaultFieldToValue = {
            "update_id": self.next_update_id,
            "payload": {"update_id": self.next_update_id},
            "queue": TelegramIncomingUpdate.Queue.BUSINESS,
            "status": TelegramIncomingUpdate.Status.PROCESSING,
            "started_at": self.now - STALE_AFTER - timedelta(seconds=1),
            "next_attempt_at": self.now + timedelta(hours=1),
        }
        defaultFieldToValue.update(fieldToValue)
        return TelegramIncomingUpdate.objects.create(**defaultFieldToValue)

    def test_only_stale_processing_in_selected_queue_is_recovered(self):
        stale = self.make_item(attempts=3)
        fresh = self.make_item(started_at=self.now)
        priority = self.make_item(queue=TelegramIncomingUpdate.Queue.PRIORITY)
        done = self.make_item(status=TelegramIncomingUpdate.Status.DONE)
        failed = self.make_item(status=TelegramIncomingUpdate.Status.FAILED)
        pending = self.make_item(status=TelegramIncomingUpdate.Status.PENDING)

        self.assertEqual(recover_stale_updates("business"), 1)
        stale.refresh_from_db()
        self.assertEqual(stale.status, TelegramIncomingUpdate.Status.PENDING)
        self.assertIsNone(stale.started_at)
        self.assertLessEqual(stale.next_attempt_at, timezone.now())
        self.assertEqual(stale.attempts, 3)
        for item in (fresh, priority, done, failed, pending):
            expected_status = item.status
            expected_next_attempt = item.next_attempt_at
            item.refresh_from_db()
            self.assertEqual(item.status, expected_status)
            self.assertEqual(item.next_attempt_at, expected_next_attempt)
        self.assertEqual(recover_stale_updates("business"), 0)

    def test_old_processing_without_started_at_is_recovered(self):
        old = self.make_item(started_at=None)
        fresh = self.make_item(started_at=None)
        TelegramIncomingUpdate.objects.filter(pk=old.pk).update(
            created_at=self.now - STALE_AFTER - timedelta(seconds=1),
        )
        self.assertEqual(recover_stale_updates("business"), 1)
        old.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual(old.status, TelegramIncomingUpdate.Status.PENDING)
        self.assertEqual(fresh.status, TelegramIncomingUpdate.Status.PROCESSING)

    def test_fresh_lease_on_old_event_is_not_recovered(self):
        item = self.make_item(started_at=self.now)
        TelegramIncomingUpdate.objects.filter(pk=item.pk).update(
            created_at=self.now - timedelta(days=10),
        )
        self.assertEqual(recover_stale_updates("business"), 0)

    def test_recovered_completed_update_skips_handler(self):
        item = self.make_item()
        WebhookUpdate.objects.create(update_id=item.update_id)
        recover_stale_updates("business")
        claimed = claim_next_update("business")
        with patch("webhook_tg.views.process_telegram_update") as handler:
            self.assertTrue(process_claimed_update(claimed))
        handler.assert_not_called()
        item.refresh_from_db()
        self.assertEqual(item.status, TelegramIncomingUpdate.Status.DONE)
        self.assertIsNotNone(item.processed_at)
        self.assertIsNone(claim_next_update("business"))

    def test_recovered_update_is_processed_and_marked_done(self):
        item = self.make_item()
        recover_stale_updates("business")
        claimed = claim_next_update("business")
        with patch("webhook_tg.views.process_telegram_update") as handler:
            self.assertTrue(process_claimed_update(claimed))
        handler.assert_called_once_with(item.payload, use_idempotency=False)
        item.refresh_from_db()
        self.assertEqual(item.status, TelegramIncomingUpdate.Status.DONE)
        self.assertEqual(item.last_error, "")
        self.assertTrue(WebhookUpdate.objects.filter(update_id=item.update_id).exists())

    def test_recovery_preserves_retry_limit(self):
        item = self.make_item(attempts=MAX_ATTEMPTS - 1)
        recover_stale_updates("business")
        with (
            patch("webhook_tg.views.process_telegram_update", side_effect=RuntimeError("test failure")),
            self.assertLogs("webhook_tg.incoming", level="ERROR"),
        ):
            self.assertFalse(process_claimed_update(claim_next_update("business")))
        item.refresh_from_db()
        self.assertEqual(item.status, TelegramIncomingUpdate.Status.FAILED)
        self.assertEqual(item.attempts, MAX_ATTEMPTS)
        self.assertEqual(recover_stale_updates("business"), 0)

    def test_network_outage_does_not_exhaust_handler_attempts(self):
        item = self.make_item(attempts=MAX_ATTEMPTS - 1)
        recover_stale_updates("business")
        for _ in range(MAX_ATTEMPTS + 1):
            with (
                patch("webhook_tg.views.process_telegram_update",
                      side_effect=requests.ConnectionError("circuit breaker")),
                self.assertLogs("webhook_tg.incoming", level="ERROR"),
            ):
                self.assertFalse(process_claimed_update(claim_next_update("business")))
            item.refresh_from_db()
            self.assertEqual(item.status, TelegramIncomingUpdate.Status.PENDING)
            self.assertEqual(item.attempts, MAX_ATTEMPTS - 1)
            self.assertGreater(item.next_attempt_at, timezone.now() + timedelta(seconds=25))
            TelegramIncomingUpdate.objects.filter(pk=item.pk).update(next_attempt_at=timezone.now())
        with patch("webhook_tg.views.process_telegram_update"):
            self.assertTrue(process_claimed_update(claim_next_update("business")))
        item.refresh_from_db()
        self.assertEqual(item.status, TelegramIncomingUpdate.Status.DONE)

    def _assert_periodic_recovery(self, *, busy):
        item = self.make_item(started_at=self.now - STALE_AFTER + timedelta(seconds=10))
        TelegramIncomingUpdate.objects.filter(pk=item.pk).update(
            created_at=self.now - timedelta(days=10),
        )
        if busy:
            self.make_item(status=TelegramIncomingUpdate.Status.PENDING,
                           started_at=None, next_attempt_at=self.now)
        clock = SimpleNamespace(elapsed=0)
        claim_count = 0

        def advance(*args, **fieldToValue):
            clock.elapsed += 31

        def claim(queue):
            nonlocal claim_count
            claim_count += 1
            if claim_count > 2:
                raise StopWorker
            return claim_next_update(queue)

        fake_time = SimpleNamespace(monotonic=lambda: clock.elapsed, sleep=advance)
        output = StringIO()
        with (
            patch(f"{COMMAND}.time", fake_time),
            patch("webhook_tg.incoming.timezone.now",
                  side_effect=lambda: self.now + timedelta(seconds=clock.elapsed)),
            patch(f"{COMMAND}.claim_next_update", side_effect=claim),
            patch(f"{COMMAND}.recover_stale_updates", wraps=recover_stale_updates) as recover,
            patch("webhook_tg.views.process_telegram_update", side_effect=advance) as handler,
            self.assertRaises(StopWorker),
        ):
            call_command("process_telegram_updates", queue="business", stdout=output)
        self.assertEqual(recover.call_count, 3)
        self.assertEqual(handler.call_count, 2 if busy else 1)
        item.refresh_from_db()
        self.assertEqual(item.status, TelegramIncomingUpdate.Status.DONE)
        self.assertIn("Recovered stale updates: 1", output.getvalue())

    def test_periodic_recovery_while_idle_without_restart(self):
        self._assert_periodic_recovery(busy=False)

    def test_periodic_recovery_under_continuous_load(self):
        self._assert_periodic_recovery(busy=True)

    def test_recovery_is_throttled_between_iterations(self):
        with (
            patch(f"{COMMAND}.time") as clock,
            patch(f"{COMMAND}.recover_stale_updates", return_value=0) as recover,
            patch(f"{COMMAND}.claim_next_update", side_effect=[None, None, StopWorker]),
            self.assertRaises(StopWorker),
        ):
            clock.monotonic.side_effect = [0, 1, 2]
            call_command("process_telegram_updates", queue="priority")
        recover.assert_called_once_with("priority")

    def test_once_mode_recovers_and_processes_one_item(self):
        item = self.make_item()
        with patch("webhook_tg.views.process_telegram_update") as handler:
            call_command("process_telegram_updates", queue="business", once=True, stdout=StringIO())
        handler.assert_called_once()
        item.refresh_from_db()
        self.assertEqual(item.status, TelegramIncomingUpdate.Status.DONE)
