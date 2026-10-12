import fcntl
import os
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from django.db import connection, transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from .background_tasks import PermanentTaskError, process_claimed_task
from .history import handle_history_command
from .history_exports import export_history_task, schedule_history_export
from .history_files import DocumentMultipart, history_document_path, open_history_document
from .models import BackgroundTask, Message, TelegramOutbox, UserTg
from .outbox import _deliver_outbox_record, deliver_outbox_item
from . import telegram


class DurableHistoryTests(TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(override_settings(WHO_UPDATE_HISTORY_EXPORT_DIR=str(self.directory)))
        self.user = UserTg.objects.create(user_id=100, chat_id=100, business_connection_id="owner")
        self.enterContext(patch("webhook_tg.outbox.observe_metric"))
        self.enterContext(patch("webhook_tg.outbox._notify_owner_outbox_failed"))
        self.send = self.enterContext(patch("webhook_tg.outbox.dispatch_document_file", return_value=(True, "")))

    def message(self, number, **fieldToValue):
        return Message.objects.create(chat_id=200, message_id=number, business_connection_id="owner",
                                      **{"text": f"message {number}", **fieldToValue})

    def schedule(self):
        return schedule_history_export(self.user, chat_id=100, target=200, update_id=123)

    def export(self):
        task = self.schedule()
        export_history_task(task)
        return task, TelegramOutbox.objects.get()

    def test_command_only_persists_task_and_replays_without_new_task(self):
        with CaptureQueriesContext(connection) as queries:
            handle_history_command(100, self.user, "/history 200", update_id=123)
            handle_history_command(100, self.user, "/history 200", update_id=123)
        self.assertEqual(BackgroundTask.objects.count(), 1)
        self.assertFalse(TelegramOutbox.objects.exists())
        self.assertFalse(any('"webhook_tg_message"' in item["sql"] for item in queries))
        self.send.assert_not_called()

    def test_snapshot_bound_survives_restart_and_excludes_later_rows(self):
        self.message(1)
        task = self.schedule()
        with patch("webhook_tg.history_exports._write_history", side_effect=OSError("restart")):
            with self.assertRaises(OSError):
                export_history_task(task)
        self.message(2)
        export_history_task(BackgroundTask.objects.get(pk=task.pk))
        item = TelegramOutbox.objects.get()
        text = Path(item.payload["document_path"]).read_text()
        self.assertIn("message 1", text)
        self.assertNotIn("message 2", text)

    def test_background_completion_requires_durable_outbox_response(self):
        self.message(1)
        task = self.schedule()
        task.status = BackgroundTask.Status.PROCESSING
        task.save(update_fields=["status"])
        self.assertTrue(process_claimed_task(task))
        task.refresh_from_db()
        self.assertEqual(task.status, BackgroundTask.Status.COMPLETED)
        self.assertEqual(TelegramOutbox.objects.get().status, TelegramOutbox.Status.PENDING)
        self.send.assert_not_called()

    def test_export_reads_small_pages_without_archive_payload(self):
        for number in range(6):
            self.message(number, payload="large-secret-payload")
        task = self.schedule()
        with patch("webhook_tg.history_exports.HISTORY_BATCH_SIZE", 2), CaptureQueriesContext(connection) as queries:
            export_history_task(task)
        selects = [item["sql"] for item in queries if 'FROM "webhook_tg_message"' in item["sql"]]
        self.assertGreaterEqual(len(selects), 5)
        self.assertTrue(all('"payload"' not in sql and "OFFSET" not in sql for sql in selects))
        item = TelegramOutbox.objects.get()
        self.assertEqual(item.payload["count"], 6)
        self.assertEqual(os.stat(item.payload["document_path"]).st_mode & 0o777, 0o600)

    def test_queue_failure_keeps_files_and_replay_does_not_duplicate(self):
        self.message(1)
        task = self.schedule()
        with patch("webhook_tg.outbox.enqueue_outbox", side_effect=RuntimeError("db fail")):
            with self.assertRaises(RuntimeError):
                export_history_task(task)
        task.refresh_from_db()
        path = Path(task.payload["parts"][0]["document_path"])
        original = path.read_bytes()
        export_history_task(task)
        export_history_task(task)
        self.assertEqual(TelegramOutbox.objects.count(), 1)
        self.assertEqual(path.read_bytes(), original)

    def test_owner_change_cancels_without_exposing_old_connection(self):
        task = self.schedule()
        self.user.business_connection_id = "other"
        self.user.save(update_fields=["business_connection_id"])
        with self.assertRaises(PermanentTaskError):
            export_history_task(task)
        self.assertFalse(TelegramOutbox.objects.exists())

    def test_wrong_recipient_cannot_schedule(self):
        with self.assertRaises(ValueError):
            schedule_history_export(self.user, chat_id=999, target=200, update_id=123)
        self.assertFalse(BackgroundTask.objects.exists())

    def test_file_parts_are_bounded_and_cover_each_message_once(self):
        for number in range(5):
            self.message(number, text=f"unique-{number}:" + "a" * 4000)
        with patch("webhook_tg.history_exports.HISTORY_FILE_LIMIT", 9000):
            export_history_task(self.schedule())
        items = list(TelegramOutbox.objects.order_by("id"))
        self.assertEqual(len(items), 3)
        merged = "".join(Path(item.payload["document_path"]).read_text() for item in items)
        for number in range(5):
            self.assertEqual(merged.count(f"unique-{number}:"), 1)
        for item in items:
            self.assertLessEqual(Path(item.payload["document_path"]).stat().st_size, 9000)

    def test_giant_single_row_gets_clear_durable_response(self):
        self.message(1, text="a" * 2000)
        with patch("webhook_tg.history_exports.HISTORY_FILE_LIMIT", 5000):
            export_history_task(self.schedule())
        item = TelegramOutbox.objects.get()
        self.assertEqual(item.method, TelegramOutbox.Method.SEND_MESSAGE)
        self.assertIn("слишком велик", item.payload["text"])

    def test_network_retry_keeps_file_and_terminal_success_cleans_after_commit(self):
        self.message(1)
        _, item = self.export()
        path = Path(item.payload["document_path"])
        self.send.return_value = (False, "temporary network timeout")
        self.assertEqual(deliver_outbox_item(item.pk), "failed")
        self.assertTrue(path.exists())
        item.refresh_from_db()
        self.send.return_value = (True, "")
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(deliver_outbox_item(item.pk, now=item.next_attempt_at), "sent")
            self.assertTrue(path.exists())
        self.assertFalse(path.exists())
        self.assertEqual(deliver_outbox_item(item.pk), "skipped")

    def test_blocked_user_drops_and_cleans_after_commit(self):
        self.message(1)
        _, item = self.export()
        self.send.return_value = (False, "Forbidden: bot was blocked by the user")
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(deliver_outbox_item(item.pk), "dropped")
        self.assertFalse(Path(item.payload["document_path"]).exists())

    def test_failed_status_commit_never_removes_file(self):
        self.message(1)
        _, item = self.export()
        with patch("webhook_tg.outbox._record_delivery_result", side_effect=RuntimeError("DB down")):
            with self.assertRaises(RuntimeError):
                deliver_outbox_item(item.pk)
        self.assertTrue(Path(item.payload["document_path"]).exists())
        item.refresh_from_db()
        self.assertEqual(item.status, TelegramOutbox.Status.PENDING)

    def test_missing_file_recovers_from_durable_task(self):
        self.message(1)
        _, item = self.export()
        path = Path(item.payload["document_path"])
        original = path.read_bytes()
        path.unlink()
        self.send.side_effect = lambda chat_id, stream, **fieldToValue: (stream.read() == original, "")
        self.assertEqual(deliver_outbox_item(item.pk), "sent")
        self.assertEqual(path.read_bytes(), original)

    def test_missing_file_and_archive_range_stays_pending_not_dropped(self):
        message = self.message(1)
        _, item = self.export()
        path = Path(item.payload["document_path"])
        path.unlink()
        message.delete()
        with patch("webhook_tg.outbox.logger.exception"):
            self.assertEqual(deliver_outbox_item(item.pk), "failed")
        item.refresh_from_db()
        self.assertEqual(item.status, TelegramOutbox.Status.PENDING)
        self.assertFalse(path.exists())
        self.send.assert_not_called()

    def test_archive_growth_during_file_recovery_is_retryable(self):
        message = self.message(1)
        _, item = self.export()
        Path(item.payload["document_path"]).unlink()
        Message.objects.filter(pk=message.pk).update(text="a" * 2000)
        with patch("webhook_tg.history_exports.HISTORY_FILE_LIMIT", 5000), patch("webhook_tg.outbox.logger.exception"):
            self.assertEqual(deliver_outbox_item(item.pk), "failed")
        item.refresh_from_db()
        self.assertEqual(item.status, TelegramOutbox.Status.PENDING)
        self.send.assert_not_called()

    def test_stale_claim_does_not_restore_or_send_already_finished_item(self):
        self.message(1)
        _, item = self.export()
        TelegramOutbox.objects.filter(pk=item.pk).update(status=TelegramOutbox.Status.SENT)
        Path(item.payload["document_path"]).unlink()
        try:
            self.assertEqual(_deliver_outbox_record(item), "skipped")
        finally:
            item._history_delivery_lock.close()
        self.send.assert_not_called()
        self.assertFalse(Path(item.payload["document_path"]).exists())

    def test_outer_rollback_does_not_remove_pending_document(self):
        self.message(1)
        _, item = self.export()
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    self.assertEqual(deliver_outbox_item(item.pk), "sent")
                    raise RuntimeError("rollback")
            except RuntimeError:
                pass
        item.refresh_from_db()
        self.assertEqual(item.status, TelegramOutbox.Status.PENDING)
        self.assertTrue(Path(item.payload["document_path"]).exists())

    def test_slow_upload_lock_prevents_second_worker_send(self):
        self.message(1)
        _, item = self.export()
        lock_path = Path(item.payload["document_path"]).with_suffix(".delivery.lock")
        with lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(deliver_outbox_item(item.pk), "skipped")
        self.send.assert_not_called()

    def test_null_dates_and_same_timestamps_survive_keyset_pages(self):
        rows = [self.message(number) for number in range(5)]
        Message.objects.filter(pk__in=[row.pk for row in rows[:3]]).update(created_at=timezone.now())
        Message.objects.filter(pk__in=[row.pk for row in rows[3:]]).update(created_at=None)
        with patch("webhook_tg.history_exports.HISTORY_BATCH_SIZE", 1):
            _, item = self.export()
        text = Path(item.payload["document_path"]).read_text()
        for number in range(5):
            self.assertEqual(text.count(f"message {number}"), 1)


class HistoryFileSafetyTests(SimpleTestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(override_settings(WHO_UPDATE_HISTORY_EXPORT_DIR=str(self.directory)))

    def test_traversal_and_symlinks_are_rejected(self):
        for path in ("relative.txt", self.directory / ".." / "outside.txt", self.directory / "plain.txt"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                open_history_document(path)
        target = self.directory / "target"
        target.write_bytes(b"secret")
        path = history_document_path("test", 1)
        path.symlink_to(target)
        with self.assertRaises(ValueError):
            open_history_document(path)

    def test_streamed_multipart_rewinds_for_fallback_without_eager_read(self):
        path = history_document_path("test", 1)
        path.write_bytes(b"CONTENT" * 20000)
        path.chmod(0o600)
        with open_history_document(path) as document:
            body = DocumentMultipart(document, chat_id=1, filename="history.txt", caption="caption", content_type="text/plain")
            chunks = []
            while chunk := body.read(8192):
                self.assertLessEqual(len(chunk), 8192)
                chunks.append(chunk)
            expected = b"".join(chunks)
            self.assertEqual(len(expected), len(body))
            body.seek(0)
            self.assertEqual(body.read(8192), expected[:8192])
            body.seek(0)
            self.assertLessEqual(len(body.read()), 65536)
            body.seek(0)
            prepared = requests.Request("POST", "https://example.invalid", data=body,
                                        headers={"Content-Type": body.content_type}).prepare()
            self.assertIs(prepared.body, body)
            self.assertEqual(prepared.headers["Content-Length"], str(len(body)))
            self.assertEqual(body.tell(), 0)

    def test_actual_telegram_fallback_rewinds_multipart_body(self):
        path = history_document_path("test", 1)
        path.write_bytes(b"test content")
        path.chmod(0o600)
        calls = []

        def post(url, **fieldToValue):
            calls.append(fieldToValue["data"].read(1024))
            if len(calls) == 1:
                raise requests.ConnectionError("test unreachable")
            return Mock(json=lambda: {"ok": True, "result": {}})

        with open_history_document(path) as document, patch.dict(telegram._endpoint_failures, {}, clear=True), patch.object(telegram.TELEGRAM_SESSION, "post", side_effect=post), patch.object(telegram, "observe_metric"), patch.object(telegram, "log_bot_outgoing"):
            self.assertEqual(telegram.dispatch_document_file(1, document, filename="history.txt"), (True, ""))
        self.assertEqual(calls[0], calls[1])
