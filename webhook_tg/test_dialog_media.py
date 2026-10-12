from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .dialog_media import event_media, resolve_event_media
from .models import BotChatEvent, FileType, Message, UserTg


PHOTO_ID = "AgACAgIAAxkBAAIB_photo_original_1234567890"
COPY_ID = "AgACAgIAAxkBAAIB_photo_saved_copy_1234567890"
OTHER_ID = "BAACAgIAAxkBAAIB_other_attachment_1234567890"
SAVED_CAPTION = (
    "⏱ Сохранено одноразовое / view-once медиа\n"
    "От: Test (@sender)\n"
    "message_id=123"
)


def media_event(payload, *, direction=BotChatEvent.Direction.USER, **kwargs):
    return BotChatEvent(
        chat_id=600001,
        direction=direction,
        payload=payload,
        **kwargs,
    )


class EventMediaTests(SimpleTestCase):
    def test_photo_uses_largest_size_without_database_or_telegram(self):
        event = media_event({"photo": [
            {"file_id": PHOTO_ID, "width": 1920, "height": 2560},
            {"file_id": OTHER_ID, "width": 90, "height": 120},
        ]})

        media = event_media(event)

        self.assertEqual(len(media), 1)
        self.assertEqual(media[0].file_id, PHOTO_ID)
        self.assertEqual(media[0].file_type, FileType.PHOTO)

    def test_incoming_media_types_are_detected(self):
        for key, expected in (
            ("video", FileType.VIDEO),
            ("video_note", FileType.VIDEO),
            ("audio", FileType.AUDIO),
            ("voice", FileType.AUDIO),
            ("document", FileType.DOCUMENT),
            ("sticker", FileType.STICKER),
            ("animation", FileType.ANIMATION),
        ):
            with self.subTest(key=key):
                media = event_media(media_event({key: {"file_id": OTHER_ID}}))
                self.assertEqual(len(media), 1)
                self.assertEqual(media[0].file_id, OTHER_ID)
                self.assertEqual(media[0].file_type, expected)

    def test_document_preserves_filename(self):
        media = event_media(media_event({
            "document": {"file_id": OTHER_ID, "file_name": "report.pdf"},
        }))

        self.assertEqual(media[0].file_name, "report.pdf")

    def test_saved_photo_is_preferred_to_original_or_upload_filename(self):
        for original in (PHOTO_ID, "file_298.jpg"):
            with self.subTest(original=original):
                media = event_media(media_event({
                    "photo": original,
                    "source_file_id": PHOTO_ID,
                    "saved_photo": [{"file_id": COPY_ID, "width": 800, "height": 1200}],
                }, direction=BotChatEvent.Direction.BOT))
                self.assertEqual(len(media), 1)
                self.assertEqual(media[0].file_id, COPY_ID)

    def test_outgoing_telegram_media_is_preferred_to_request_payload(self):
        media = event_media(media_event({
            "photo": "upload.jpg",
            "telegram_media": [{"file_id": COPY_ID, "file_type": FileType.PHOTO}],
        }, direction=BotChatEvent.Direction.BOT))

        self.assertEqual(len(media), 1)
        self.assertEqual(media[0].file_id, COPY_ID)

    def test_outgoing_file_id_and_album_preserve_media_order(self):
        single = event_media(media_event(
            {"photo": PHOTO_ID}, direction=BotChatEvent.Direction.BOT,
        ))
        album = event_media(media_event({"media": [
            {"type": "photo", "media": PHOTO_ID},
            {"type": "video", "media": OTHER_ID},
        ]}, direction=BotChatEvent.Direction.BOT, event_type=BotChatEvent.EventType.MEDIA_GROUP))

        self.assertEqual(single[0].file_id, PHOTO_ID)
        self.assertEqual(
            [(item.file_id, item.file_type) for item in album],
            [(PHOTO_ID, FileType.PHOTO), (OTHER_ID, FileType.VIDEO)],
        )

    def test_paths_urls_and_upload_references_are_not_telegram_file_ids(self):
        for reference in (
            "file_298.jpg", "/tmp/file_298.jpg", "photos/file_298.jpg",
            "https://example.com/private.jpg", "attach://upload_photo",
        ):
            with self.subTest(reference=reference):
                media = event_media(media_event(
                    {"photo": reference}, direction=BotChatEvent.Direction.BOT,
                ))
                self.assertEqual(len(media), 1)
                self.assertEqual(media[0].file_id, "")

    def test_callback_and_reply_do_not_duplicate_nested_message_media(self):
        nested = {"photo": [{"file_id": PHOTO_ID}]}
        callback = media_event({"message": nested}, event_type=BotChatEvent.EventType.CALLBACK)
        reply = media_event({"text": "Ответ", "reply_to_message": nested})

        self.assertEqual(event_media(callback), [])
        self.assertEqual(event_media(reply), [])

    def test_plain_text_has_no_media(self):
        self.assertEqual(event_media(media_event({"text": "Привет"})), [])


class ResolveEventMediaTests(TestCase):
    def setUp(self):
        self.bot_user = UserTg.objects.create(
            user_id=700001, chat_id=600001, username="owner",
            business_connection_id="owner-connection",
        )
        self.event = media_event(
            {"photo": "file_298.jpg", "caption": SAVED_CAPTION},
            direction=BotChatEvent.Direction.BOT,
            event_type=BotChatEvent.EventType.PHOTO,
            text=SAVED_CAPTION,
        )

    def original(self, **overrides):
        values = {
            "business_connection_id": self.bot_user.business_connection_id,
            "chat_id": 800001, "message_id": 123, "username_from": "sender",
            "file_id": PHOTO_ID, "file_type": FileType.SELF_DESTRUCT_PHOTO,
        }
        values.update(overrides)
        return Message.objects.create(**values)

    def test_direct_file_id_works_without_an_original_message(self):
        event = media_event({"photo": [{"file_id": PHOTO_ID}]})

        media = resolve_event_media(self.bot_user, event, 0)

        self.assertEqual(media.file_id, PHOTO_ID)

    def test_direct_photo_id_prefers_archived_durable_copy_for_its_owner(self):
        event = media_event({"photo": [{"file_id": PHOTO_ID}]})
        self.original(saved_photo_source_file_id=PHOTO_ID, saved_photo_file_id=COPY_ID)
        self.original(
            chat_id=800002, business_connection_id="another-owner",
            saved_photo_source_file_id=PHOTO_ID, saved_photo_file_id=OTHER_ID,
        )

        self.assertEqual(resolve_event_media(self.bot_user, event, 0).file_id, COPY_ID)

    def test_foreign_chat_and_invalid_index_have_no_resolvable_attachment(self):
        event = media_event({"photo": [{"file_id": PHOTO_ID}]})
        self.assertIsNone(resolve_event_media(self.bot_user, event, -1))
        self.assertIsNone(resolve_event_media(self.bot_user, event, 1))
        event.chat_id = 900001

        self.assertIsNone(resolve_event_media(self.bot_user, event, 0))

    def test_legacy_filename_resolves_only_matching_owner_sender_and_source_message(self):
        self.original(username_from="SeNdEr")
        self.original(chat_id=800002, business_connection_id="another-owner")
        self.original(chat_id=800003, username_from="someone_else")
        self.original(chat_id=800004, message_id=124)
        self.original(chat_id=800005, file_type=FileType.VIDEO)

        media = resolve_event_media(self.bot_user, self.event, 0)

        self.assertEqual(media.file_id, PHOTO_ID)

    def test_saved_copy_is_preferred_but_stale_copy_is_not_used(self):
        original = self.original(
            saved_photo_source_file_id=PHOTO_ID, saved_photo_file_id=COPY_ID,
        )
        self.assertEqual(resolve_event_media(self.bot_user, self.event, 0).file_id, COPY_ID)

        original.saved_photo_source_file_id = OTHER_ID
        original.save(update_fields=["saved_photo_source_file_id"])

        self.assertEqual(resolve_event_media(self.bot_user, self.event, 0).file_id, PHOTO_ID)

    def test_missing_or_ambiguous_original_cannot_be_guessed(self):
        self.assertIsNone(resolve_event_media(self.bot_user, self.event, 0))
        self.original()
        self.original(chat_id=800002)

        self.assertIsNone(resolve_event_media(self.bot_user, self.event, 0))

    def test_no_business_connection_or_wrong_sender_cannot_resolve_another_message(self):
        original = self.original(username_from="another_sender")
        self.assertIsNone(resolve_event_media(self.bot_user, self.event, 0))
        original.username_from = "sender"
        original.save(update_fields=["username_from"])
        self.bot_user.business_connection_id = ""

        self.assertIsNone(resolve_event_media(self.bot_user, self.event, 0))

    def test_incoming_or_unrelated_caption_cannot_trigger_legacy_lookup(self):
        self.original()
        self.event.direction = BotChatEvent.Direction.USER
        self.assertIsNone(resolve_event_media(self.bot_user, self.event, 0))
        self.event.direction = BotChatEvent.Direction.BOT
        self.event.text = self.event.payload["caption"] = "Чужой текст\n" + SAVED_CAPTION

        self.assertIsNone(resolve_event_media(self.bot_user, self.event, 0))


class DialogMediaAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser(
            username="dialog-media-admin", password="test",
        )
        cls.staff = get_user_model().objects.create_user(
            username="dialog-media-staff", password="test", is_staff=True,
        )
        cls.bot_user = UserTg.objects.create(
            user_id=700001, chat_id=600001, username="owner",
            business_connection_id="owner-connection",
        )
        cls.event = BotChatEvent.objects.create(
            chat_id=cls.bot_user.chat_id,
            direction=BotChatEvent.Direction.USER,
            event_type=BotChatEvent.EventType.PHOTO,
            payload={"photo": [{"file_id": PHOTO_ID, "width": 800, "height": 1200}]},
        )

    def setUp(self):
        self.client.force_login(self.admin)
        self.url = self.media_url(self.event)

    def media_url(self, event, index=0, user=None):
        return reverse(
            "admin:webhook_tg_usertg_dialog_media",
            args=[(user or self.bot_user).pk, event.pk, index],
        )

    @patch("webhook_tg.dialog_media.open_telegram_file_stream")
    @patch("webhook_tg.dialog_media.get_telegram_file_path")
    def test_dialog_renders_media_control_without_loading_telegram(self, get_path, open_stream):
        response = self.client.get(reverse(
            "admin:webhook_tg_usertg_dialog", args=[self.bot_user.pk],
        ))

        self.assertContains(response, self.url)
        get_path.assert_not_called()
        open_stream.assert_not_called()

    @patch("webhook_tg.dialog_media.get_telegram_file_path")
    def test_only_logged_in_superuser_can_fetch_media(self, get_path):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)
        get_path.assert_not_called()

    @patch("webhook_tg.dialog_media.get_telegram_file_path")
    def test_foreign_event_and_invalid_index_are_not_accessible(self, get_path):
        foreign = BotChatEvent.objects.create(
            chat_id=900001, direction=BotChatEvent.Direction.USER,
            event_type=BotChatEvent.EventType.PHOTO,
            payload={"photo": [{"file_id": OTHER_ID}]},
        )

        self.assertEqual(self.client.get(self.media_url(foreign)).status_code, 404)
        self.assertEqual(self.client.get(self.media_url(self.event, index=99)).status_code, 404)
        get_path.assert_not_called()

    @patch("webhook_tg.dialog_media.get_telegram_file_path")
    def test_post_does_not_start_media_download(self, get_path):
        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 405)
        get_path.assert_not_called()

    @patch("webhook_tg.dialog_media.open_telegram_file_stream")
    @patch("webhook_tg.dialog_media.get_telegram_file_path", return_value="photos/photo.jpg")
    def test_stream_uses_event_file_id_and_closes_upstream(self, get_path, open_stream):
        upstream = Mock()
        upstream.iter_content.return_value = iter([b"jpeg", b"", b"-data"])
        open_stream.return_value = upstream

        response = self.client.get(self.url, {"file_id": OTHER_ID})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(b"".join(response.streaming_content), b"jpeg-data")
        # Manual close must mirror Django test client signal isolation.
        from django.core.signals import request_finished
        from django.db import close_old_connections
        request_finished.disconnect(close_old_connections)
        try:
            response.close()
        finally:
            request_finished.connect(close_old_connections)
        get_path.assert_called_once_with(PHOTO_ID)
        open_stream.assert_called_once_with("photos/photo.jpg")
        upstream.close.assert_called()

    @patch("webhook_tg.dialog_media.open_telegram_file_stream")
    @patch("webhook_tg.dialog_media.get_telegram_file_path", return_value="photos/photo.jpg")
    def test_closing_response_before_streaming_still_closes_upstream(self, get_path, open_stream):
        upstream = Mock()
        open_stream.return_value = upstream

        response = self.client.get(self.url)
        # Manual close must mirror Django test client signal isolation.
        from django.core.signals import request_finished
        from django.db import close_old_connections
        request_finished.disconnect(close_old_connections)
        try:
            response.close()
        finally:
            request_finished.connect(close_old_connections)

        upstream.iter_content.assert_not_called()
        upstream.close.assert_called_once()

    @patch("webhook_tg.dialog_media.get_telegram_file_path")
    def test_legacy_filename_without_resolvable_original_returns_410(self, get_path):
        event = BotChatEvent.objects.create(
            chat_id=self.bot_user.chat_id, direction=BotChatEvent.Direction.BOT,
            event_type=BotChatEvent.EventType.PHOTO,
            text=SAVED_CAPTION, payload={"photo": "file_298.jpg", "caption": SAVED_CAPTION},
        )

        self.assertEqual(self.client.get(self.media_url(event)).status_code, 410)
        get_path.assert_not_called()

    @patch("webhook_tg.dialog_media.get_telegram_file_path")
    def test_external_url_is_not_fetched(self, get_path):
        self.event.payload = {"photo": "https://example.com/private.jpg"}
        self.event.save(update_fields=["payload"])

        self.assertEqual(self.client.get(self.url).status_code, 410)
        get_path.assert_not_called()

    @patch("webhook_tg.dialog_media.get_telegram_file_path", side_effect=RuntimeError(
        "Bad Request: wrong file_id or the file is temporarily unavailable"
    ))
    def test_expired_telegram_media_returns_410(self, get_path):
        self.assertEqual(self.client.get(self.url).status_code, 410)

    @patch("webhook_tg.dialog_media.get_telegram_file_path", side_effect=RuntimeError(
        "Connection failed at https://api.telegram.org/botTOP-SECRET/getFile"
    ))
    def test_telegram_error_response_does_not_expose_credentials(self, get_path):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 502)
        self.assertNotIn(b"TOP-SECRET", response.content)
        self.assertNotIn(b"api.telegram.org", response.content)
