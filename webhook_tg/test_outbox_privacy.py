from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from .models import TelegramOutbox
from .outbox import (
    VIDEO_NOTE_PRIVACY_NOTICE,
    VOICE_MESSAGES_FORBIDDEN,
    deliver_outbox_item,
    enqueue_outbox,
)


class OutboxPrivacyTests(TestCase):
    def item(self, *, legacy=True, text="<b>Предупреждение</b>", kind="video_note"):
        payload = {"text": text, "parse_mode": "HTML"}
        if legacy:
            method = TelegramOutbox.Method.SEND_VIDEO_NOTE_WITH_TEXT
            payload["video_note"] = "saved-circle-id"
        else:
            method = TelegramOutbox.Method.SEND_MEDIA_WITH_TEXT
            payload.update(media_kind=kind, file_id="saved-media-id")
        return enqueue_outbox(chat_id=123, method=method, payload=payload)

    def ready(self, item):
        TelegramOutbox.objects.filter(pk=item.pk).update(next_attempt_at=timezone.now())

    @patch("webhook_tg.outbox._notify_owner_outbox_failed")
    @patch("webhook_tg.outbox.dispatch_telegram_request")
    def test_privacy_refusal_delivers_warning_for_both_payload_formats(self, dispatch, alert):
        for legacy in (True, False):
            with self.subTest(legacy=legacy):
                item = self.item(legacy=legacy)
                dispatch.reset_mock()
                dispatch.side_effect = [(False, "Bad Request: VOICE_MESSAGES_FORBIDDEN"), (True, "")]
                self.assertEqual(deliver_outbox_item(item.pk), "sent")
                self.assertEqual([c.args[0] for c in dispatch.call_args_list], ["sendVideoNote", "sendMessage"])
                self.assertTrue(all(c.args[1] == 123 for c in dispatch.call_args_list))
                self.assertEqual(dispatch.call_args.args[2]["text"], item.payload["text"] + "\n\n" + VIDEO_NOTE_PRIVACY_NOTICE)
                item.refresh_from_db()
                self.assertEqual(item.status, TelegramOutbox.Status.SENT)
                self.assertEqual(item.payload["_media_skipped_reason"], VOICE_MESSAGES_FORBIDDEN)
                self.assertFalse(item.payload.get("_video_note_sent"))
                self.assertFalse(item.payload.get("_media_sent"))
                self.assertEqual(item.last_error, "")
                self.assertEqual(deliver_outbox_item(item.pk), "skipped")
        alert.assert_not_called()

    @patch("webhook_tg.outbox.dispatch_telegram_request")
    def test_retry_after_text_timeout_never_retries_forbidden_circle(self, dispatch):
        item = self.item()
        original = item.payload["text"]
        dispatch.side_effect = [(False, VOICE_MESSAGES_FORBIDDEN), (False, "Temporary timeout")]
        self.assertEqual(deliver_outbox_item(item.pk), "failed")
        item.refresh_from_db()
        self.assertEqual(item.payload["text"], original)
        dispatch.reset_mock()
        dispatch.side_effect = None
        dispatch.return_value = (True, "")
        self.ready(item)
        self.assertEqual(deliver_outbox_item(item.pk), "sent")
        dispatch.assert_called_once_with("sendMessage", 123, {
            "text": original + "\n\n" + VIDEO_NOTE_PRIVACY_NOTICE,
            "parse_mode": "HTML", "disable_web_page_preview": True,
        })

    @patch("webhook_tg.outbox._notify_owner_outbox_failed")
    @patch("webhook_tg.outbox.dispatch_telegram_request", return_value=(True, ""))
    def test_existing_failed_rows_resume_from_text_without_media_attempt(self, dispatch, alert):
        item = self.item()
        TelegramOutbox.objects.filter(pk=item.pk).update(
            last_error="Bad Request: voice_messages_forbidden", attempts=10,
        )
        self.assertEqual(deliver_outbox_item(item.pk), "sent")
        self.assertEqual([c.args[0] for c in dispatch.call_args_list], ["sendMessage"])
        alert.assert_not_called()

    @patch("webhook_tg.outbox.dispatch_telegram_request")
    def test_long_warning_preserves_progress_after_media_skip(self, dispatch):
        item = self.item(text="<b>" + "я" * 7000 + "</b>")
        dispatch.side_effect = [(False, VOICE_MESSAGES_FORBIDDEN), (True, ""), (False, "Temporary timeout")]
        self.assertEqual(deliver_outbox_item(item.pk), "failed")
        item.refresh_from_db()
        self.assertEqual(item.payload["_text_delivery"]["next"], 1)
        failed_part = dispatch.call_args.args[2]
        dispatch.reset_mock()
        dispatch.side_effect = None
        dispatch.return_value = (True, "")
        self.ready(item)
        self.assertEqual(deliver_outbox_item(item.pk), "sent")
        dispatch.assert_called_once_with("sendMessage", 123, failed_part)
        self.assertEqual(failed_part["text"].count(VIDEO_NOTE_PRIVACY_NOTICE), 1)

    @patch("webhook_tg.outbox.dispatch_telegram_request", return_value=(False, "Temporary timeout"))
    def test_network_failure_keeps_media_before_text(self, dispatch):
        item = self.item()
        self.assertEqual(deliver_outbox_item(item.pk), "failed")
        self.assertEqual([c.args[0] for c in dispatch.call_args_list], ["sendVideoNote"])
        item.refresh_from_db()
        self.assertNotIn("_media_skipped_reason", item.payload)

    @patch("webhook_tg.outbox.dispatch_telegram_request", return_value=(True, ""))
    def test_successful_circle_has_original_text_and_no_privacy_notice(self, dispatch):
        item = self.item()
        self.assertEqual(deliver_outbox_item(item.pk), "sent")
        self.assertEqual(dispatch.call_args.args[2]["text"], item.payload["text"])
        item.refresh_from_db()
        self.assertTrue(item.payload["_video_note_sent"])
        self.assertNotIn("_media_skipped_reason", item.payload)

    @patch("webhook_tg.outbox.dispatch_telegram_request", return_value=(False, "Bad Request: VOICE_MESSAGES_FORBIDDEN"))
    def test_standalone_media_refusal_is_permanent(self, dispatch):
        item = enqueue_outbox(chat_id=123, method="sendVideoNote", payload={"video_note": "circle"})
        self.assertEqual(deliver_outbox_item(item.pk), "dropped")
        self.assertEqual(deliver_outbox_item(item.pk), "skipped")
        dispatch.assert_called_once()

    @patch("webhook_tg.outbox.dispatch_telegram_request", return_value=(False, "Forbidden: bot was blocked by the user"))
    def test_blocked_bot_is_not_treated_as_a_media_privacy_refusal(self, dispatch):
        item = self.item()
        self.assertEqual(deliver_outbox_item(item.pk), "dropped")
        self.assertEqual(dispatch.call_count, 1)
        item.refresh_from_db()
        self.assertNotIn("_media_skipped_reason", item.payload)
