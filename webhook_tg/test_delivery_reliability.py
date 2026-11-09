from datetime import timedelta
from unittest.mock import patch

import requests
from django.test import TestCase
from django.db import connection
from django.utils import timezone

from .connection_check import CHECK_CALLBACK, check_message, handle_check_callback
from .incoming import enqueue_incoming_update, claim_next_update, process_claimed_update
from .models import Message, TelegramIncomingUpdate, TelegramOutbox, UserTg, WhoUpdateConnectionCheck, WhoUpdateFirstObservedValue
from .outbox import deliver_outbox_item, enqueue_outbox, edit_notification_idempotency_key, _next_attempt_at
from .telegram import get_business_connection
from .views import process_telegram_update, _handle_business_connection_update


class DeliveryReliabilityTests(TestCase):
    def setUp(self):
        self.owner = UserTg.objects.create(user_id=101, chat_id=101, username="owner", business_connection_id="reliable-conn", business_is_connected=True, access_unlimited=True)
        self.sequence = 800000

    def update(self, kind="business_message", *, mid=10, text="original", **extra):
        self.sequence += 1
        msg = {"business_connection_id": "reliable-conn", "chat": {"id": 202, "type": "private"},
               "from": {"id": 202, "username": "peer"}, "message_id": mid, "text": text, **extra}
        if kind == "deleted_business_messages":
            msg = {"business_connection_id": "reliable-conn", "chat": {"id": 202}, "message_ids": [mid]}
        return {"update_id": self.sequence, kind: msg}

    def incoming(self, payload):
        item, created = enqueue_incoming_update(payload, source="test")
        if created:
            self.assertTrue(process_claimed_update(claim_next_update(item.queue)))
        item.refresh_from_db()
        self.assertEqual(item.status, TelegramIncomingUpdate.Status.DONE)
        return item

    def ready(self, item):
        TelegramOutbox.objects.filter(pk=item.pk).update(next_attempt_at=timezone.now())

    @patch("webhook_tg.telegram._telegram_post", side_effect=requests.ConnectionError("Telegram is offline"))
    def test_known_connection_and_new_message_need_no_telegram_api(self, post):
        business = get_business_connection({"business_connection_id": "reliable-conn"})
        self.assertEqual(business.user_chat_id, 101)
        self.incoming(self.update())
        self.assertEqual(Message.objects.get().text, "original")
        post.assert_not_called()

    @patch("webhook_tg.outbox.dispatch_telegram_request", return_value=(False, "Connection timeout"))
    def test_delete_survives_outage_duplicate_update_and_worker_restart(self, dispatch):
        self.incoming(self.update())
        deleted = self.update("deleted_business_messages")
        self.incoming(deleted)
        item = TelegramOutbox.objects.get()
        self.assertEqual(item.status, "pending")
        self.assertTrue(Message.objects.get().is_deleted)
        self.assertIn("original", item.payload["text"])
        self.assertFalse(WhoUpdateFirstObservedValue.objects.exists())
        self.incoming(deleted)
        self.incoming(self.update("deleted_business_messages"))
        self.assertEqual(TelegramOutbox.objects.count(), 1)
        self.ready(item)
        dispatch.return_value = (True, "")
        self.assertEqual(deliver_outbox_item(item.pk), "sent")
        self.assertEqual(deliver_outbox_item(item.pk), "skipped")
        first = WhoUpdateFirstObservedValue.objects.get()
        self.assertEqual(first.event_type, "delete_text")
        self.assertEqual(first.outbox_id, item.pk)

    @patch("webhook_tg.outbox.dispatch_telegram_request", return_value=(False, "Connection timeout"))
    def test_edit_keeps_old_and_new_text_during_outage(self, dispatch):
        self.incoming(self.update())
        payload = self.update("edited_business_message", text="changed", edit_date=100)
        self.incoming(payload)
        self.assertEqual(Message.objects.get().text, "changed")
        item = TelegramOutbox.objects.get()
        self.assertIn("original", item.payload["text"])
        self.assertIn("changed", item.payload["text"])
        self.incoming(payload)
        self.assertEqual(TelegramOutbox.objects.count(), 1)
        self.ready(item)
        dispatch.return_value = (True, "")
        self.assertEqual(deliver_outbox_item(item.pk), "sent")

    @patch("webhook_tg.outbox.dispatch_telegram_request", return_value=(True, ""))
    def test_two_messages_edited_in_same_second_do_not_collapse(self, dispatch):
        for mid in (10, 11):
            self.incoming(self.update(mid=mid, text=f"original {mid}"))
            self.incoming(self.update("edited_business_message", mid=mid, text="same change", edit_date=100))
        self.assertEqual(TelegramOutbox.objects.filter(status="sent").count(), 2)
        self.assertEqual(len(set(TelegramOutbox.objects.values_list("idempotency_key", flat=True))), 2)

    @patch("webhook_tg.outbox.dispatch_telegram_request", return_value=(True, ""))
    def test_deleted_regular_circle_keeps_its_sending_type(self, dispatch):
        payload = self.update(video_note={"file_id": "regular-circle"})
        payload["business_message"].pop("text")
        self.incoming(payload)
        self.incoming(self.update("deleted_business_messages"))
        self.assertEqual(dispatch.call_args_list[0].args[0], "sendVideoNote")
        self.assertEqual(dispatch.call_args_list[1].args[0], "sendMessage")

    @patch("webhook_tg.outbox.dispatch_document_bytes", return_value=(False, "Connection timeout"))
    def test_bulk_archive_stays_durable_until_success(self, dispatch):
        for mid in range(1, 26):
            self.incoming(self.update(mid=mid))
        payload = self.update("deleted_business_messages")
        payload["deleted_business_messages"]["message_ids"] = list(range(1, 26))
        self.incoming(payload)
        item = TelegramOutbox.objects.get()
        self.assertEqual(item.method, "sendDocumentBytes")
        self.assertEqual(item.status, "pending")
        first_document = dispatch.call_args.args[1]
        self.ready(item)
        dispatch.return_value = (True, "")
        self.assertEqual(deliver_outbox_item(item.pk), "sent")
        self.assertEqual(first_document, dispatch.call_args.args[1])
        self.assertEqual(Message.objects.filter(is_deleted=True).count(), 25)

    @patch("webhook_tg.outbox.dispatch_telegram_request", return_value=(False, "Connection timeout"))
    def test_activation_and_disconnection_messages_are_durable_and_deduplicated(self, dispatch):
        self.owner.business_is_connected = False
        self.owner.save(update_fields=["business_is_connected"])
        conn = {"id": "reliable-conn", "user_chat_id": 101, "is_enabled": True, "user": {"id": 101, "username": "owner"}}
        _handle_business_connection_update(conn, update_id=900001)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.business_is_connected)
        self.assertEqual(TelegramOutbox.objects.filter(status="pending").count(), 2)
        _handle_business_connection_update(conn, update_id=900001)
        _handle_business_connection_update(conn, update_id=900002)
        self.assertEqual(TelegramOutbox.objects.count(), 2)
        self.assertNotIn("reply_markup", TelegramOutbox.objects.get(chat_id=101).payload)
        _handle_business_connection_update({**conn, "is_enabled": False}, update_id=900003)
        self.assertEqual(TelegramOutbox.objects.count(), 4)
        self.owner.refresh_from_db()
        self.assertFalse(self.owner.business_is_connected)

    @patch("webhook_tg.outbox.dispatch_telegram_request", return_value=(False, "Forbidden: bot was blocked by the user"))
    def test_blocked_recipient_is_terminal(self, dispatch):
        self.incoming(self.update())
        self.incoming(self.update("deleted_business_messages"))
        item = TelegramOutbox.objects.get()
        self.assertEqual(item.status, "dropped")
        self.assertEqual(deliver_outbox_item(item.pk), "skipped")
        self.assertEqual(dispatch.call_count, 1)

    def test_network_backoff_is_bounded_and_rate_limit_is_respected(self):
        now = timezone.now()
        self.assertLess((_next_attempt_at(12, "Connection timeout") - now).total_seconds(), 31)
        self.assertLess((_next_attempt_at(12, "circuit breaker") - now).total_seconds(), 11)
        delay = (_next_attempt_at(1, "Too many requests; retry_after=120") - now).total_seconds()
        self.assertGreaterEqual(delay, 120)
        self.assertLess(delay, 121)

    def test_old_outbox_writer_works_with_new_schema(self):
        with connection.cursor() as cursor:
            cursor.execute("""INSERT INTO webhook_tg_telegramoutbox
                (chat_id,method,payload,status,attempts,next_attempt_at,last_error,created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                           [101, "sendMessage", '{"text":"legacy writer"}', "pending", 0, timezone.now(), "", timezone.now()])
        item = TelegramOutbox.objects.get()
        self.assertEqual(item.delivery_context, {})
        with patch("webhook_tg.outbox.dispatch_telegram_request", return_value=(True, "")):
            self.assertEqual(deliver_outbox_item(item.pk), "sent")

    def test_migration_matches_models(self):
        from django.apps import apps
        from django.db.migrations.autodetector import MigrationAutodetector
        from django.db.migrations.loader import MigrationLoader
        from django.db.migrations.state import ProjectState

        loader = MigrationLoader(connection)
        appLabelToChanges = MigrationAutodetector(loader.project_state(), ProjectState.from_apps(apps)).changes(graph=loader.graph)
        self.assertFalse(appLabelToChanges.get("webhook_tg"), "WhoUpdate models need a migration")


class ProtectedMediaDeliveryTests(DeliveryReliabilityTests):
    def protected_item(self):
        return enqueue_outbox(chat_id=101, method=TelegramOutbox.Method.SEND_PROTECTED_MEDIA,
                              payload={"file_id": "protected-video-note", "media_kind": "video_note", "text": "Saved protected circle"})

    @patch("webhook_tg.outbox.dispatch_telegram_request")
    @patch("webhook_tg.outbox.dispatch_media_bytes", return_value=(True, ""))
    @patch("webhook_tg.outbox.download_telegram_file_bytes", return_value=(b"video-data", "circle.mp4"))
    def test_download_and_media_are_not_repeated_after_text_failure(self, download, upload, text):
        item = self.protected_item()
        text.return_value = (False, "Connection timeout")
        self.assertEqual(deliver_outbox_item(item.pk), "failed")
        item.refresh_from_db()
        self.assertTrue(item.payload["_media_sent"])
        self.assertNotIn("media_b64", item.payload)
        self.ready(item)
        text.return_value = (True, "")
        self.assertEqual(deliver_outbox_item(item.pk), "sent")
        download.assert_called_once()
        upload.assert_called_once()

    @patch("webhook_tg.outbox.dispatch_telegram_request", return_value=(True, ""))
    @patch("webhook_tg.outbox.dispatch_media_bytes")
    @patch("webhook_tg.outbox.download_telegram_file_bytes", return_value=(b"video-data", "circle.mp4"))
    def test_downloaded_bytes_survive_upload_failure(self, download, upload, text):
        item = self.protected_item()
        upload.return_value = (False, "Connection timeout")
        self.assertEqual(deliver_outbox_item(item.pk), "failed")
        item.refresh_from_db()
        self.assertTrue(item.payload["media_b64"])
        self.ready(item)
        upload.return_value = (True, "")
        self.assertEqual(deliver_outbox_item(item.pk), "sent")
        download.assert_called_once()

    @patch("webhook_tg.outbox.dispatch_media_bytes", return_value=(True, ""))
    @patch("webhook_tg.outbox.dispatch_telegram_request", return_value=(True, ""))
    @patch("webhook_tg.outbox.download_telegram_file_bytes", return_value=(b"video-data", "circle.mp4"))
    def test_real_protected_circle_reply_is_supported(self, download, text, upload):
        payload = self.update(text="save", reply_to_message={"message_id": 5, "from": {"id": 202}, "has_protected_content": True, "video_note": {"file_id": "circle-id"}})
        payload["business_message"]["from"]["id"] = 101
        self.incoming(payload)
        item = TelegramOutbox.objects.get(method="sendProtectedMedia")
        self.assertEqual(item.status, "sent")
        self.assertEqual(item.payload["media_kind"], "video_note")
        self.assertTrue(Message.objects.filter(message_id=5).exists())
        self.assertEqual(upload.call_args.kwargs["kind"], "video_note")


class ConnectionCheckTests(DeliveryReliabilityTests):
    def start_check(self):
        with patch("webhook_tg.connection_check.answer_callback_query"), patch("webhook_tg.outbox.dispatch_telegram_request", return_value=(True, "")):
            handle_check_callback({"id": "test-callback", "data": CHECK_CALLBACK, "from": {"id": 101}, "message": {"chat": {"id": 101}}}, update_id=1234)
        return WhoUpdateConnectionCheck.objects.get()

    @patch("webhook_tg.outbox.dispatch_telegram_request", return_value=(False, "Connection timeout"))
    def test_progress_requires_same_test_message_and_confirmed_edit_and_delete(self, dispatch):
        check = self.start_check()
        self.incoming(self.update(text=check_message(check)))
        check.refresh_from_db()
        self.assertIsNotNone(check.received_at)
        self.incoming(self.update("edited_business_message", text="changed test", edit_date=100))
        edit = TelegramOutbox.objects.get(idempotency_key__startswith="edit:")
        check.refresh_from_db()
        self.assertIsNone(check.edit_delivered_at)
        self.ready(edit)
        dispatch.return_value = (True, "")
        self.assertEqual(deliver_outbox_item(edit.pk), "sent")
        check.refresh_from_db()
        self.assertIsNotNone(check.edit_delivered_at)
        dispatch.return_value = (False, "Connection timeout")
        self.incoming(self.update("deleted_business_messages"))
        check.refresh_from_db()
        self.assertIsNone(check.delete_delivered_at)
        deleted = TelegramOutbox.objects.get(idempotency_key__startswith="delete:")
        self.ready(deleted)
        dispatch.return_value = (True, "")
        self.assertEqual(deliver_outbox_item(deleted.pk), "sent")
        check.refresh_from_db()
        self.assertIsNotNone(check.delete_delivered_at)
        self.assertEqual(TelegramOutbox.objects.filter(payload__text__icontains="Проверка пройдена").count(), 1)
        self.assertEqual(deliver_outbox_item(deleted.pk), "skipped")

    @patch("webhook_tg.outbox.dispatch_telegram_request", return_value=(True, ""))
    def test_unrelated_and_own_messages_do_not_complete_check(self, dispatch):
        check = self.start_check()
        self.incoming(self.update(text="unrelated"))
        own = self.update(text=check_message(check))
        own["business_message"]["from"]["id"] = 101
        self.incoming(own)
        check.refresh_from_db()
        self.assertIsNone(check.received_at)
        self.incoming(self.update(mid=20, text=check_message(check)))
        self.incoming(self.update("edited_business_message", mid=10, text="unrelated change", edit_date=100))
        check.refresh_from_db()
        self.assertIsNone(check.edit_delivered_at)

    def test_duplicate_button_does_not_reset_active_progress(self):
        first = self.start_check()
        self.start_check()
        first.refresh_from_db()
        self.assertEqual(WhoUpdateConnectionCheck.objects.count(), 1)
        self.assertEqual(first.start_update_id, 1234)
