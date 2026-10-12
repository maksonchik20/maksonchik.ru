from copy import deepcopy

from django.test import TestCase

from .bot_outgoing_log import log_bot_outgoing
from .models import BotChatEvent, BotOutgoingMessage, UserTg


class BotOutgoingMediaTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.bot_user = UserTg.objects.create(user_id=810001, chat_id=810001)

    def log_event(self, *, method="sendPhoto", payload=None, result=None):
        log_bot_outgoing(
            chat_id=self.bot_user.chat_id,
            method=method,
            payload=payload,
            result=result,
        )
        return BotChatEvent.objects.latest("pk")

    def test_uploaded_photo_keeps_largest_returned_id_and_original_payload(self):
        payload = {
            "photo": "file_298.jpg",
            "caption": "Saved media",
            "source_file_id": "expiring-original",
            "saved_photo": [{"file_id": "saved-copy"}],
        }
        original = deepcopy(payload)
        event = self.log_event(
            payload=payload,
            result={
                "message_id": 56,
                "chat": {"id": self.bot_user.chat_id, "first_name": "Private"},
                "photo": [
                    {"file_id": "largest-copy", "width": 1920, "height": 2560, "file_unique_id": "private-id"},
                    {"file_id": "thumbnail", "width": 90, "height": 120},
                ],
            },
        )
        self.assertEqual(event.payload, {
            **original,
            "telegram_media": [{"file_id": "largest-copy", "file_type": "PHOTO"}],
        })
        self.assertEqual(payload, original)
        self.assertEqual(event.telegram_message_id, 56)
        self.assertNotIn("chat", event.payload)

    def test_photo_without_size_metadata_uses_last_valid_size(self):
        event = self.log_event(result={
            "photo": [{"file_id": "small"}, {"file_id": "full"}, {}, None],
        })
        self.assertEqual(event.payload["telegram_media"], [{"file_id": "full", "file_type": "PHOTO"}])

    def test_all_supported_attachment_types_use_returned_file_id(self):
        for field, file_type in (
            ("video", "VIDEO"),
            ("video_note", "VIDEO"),
            ("audio", "AUDIO"),
            ("voice", "AUDIO"),
            ("document", "DOCUMENT"),
            ("sticker", "STICKER"),
            ("animation", "ANIMATION"),
        ):
            with self.subTest(field=field):
                event = self.log_event(
                    payload={field: "https://example.com/upload"},
                    result={field: {"file_id": "reusable", "file_name": "original.bin", "duration": 5}},
                )
                self.assertEqual(event.payload["telegram_media"], [{
                    "file_id": "reusable", "file_type": file_type, "file_name": "original.bin",
                }])
                self.assertEqual(event.payload[field], "https://example.com/upload")

    def test_animation_document_compatibility_field_does_not_duplicate_media(self):
        event = self.log_event(result={
            "animation": {"file_id": "animation-id"},
            "document": {"file_id": "document-id"},
        })
        self.assertEqual(event.payload["telegram_media"], [{"file_id": "animation-id", "file_type": "ANIMATION"}])

    def test_album_keeps_response_order_without_copying_message_bodies(self):
        event = self.log_event(
            method="sendMediaGroup",
            payload={"media": [{"type": "photo", "media": "attach://photo"}, {"type": "video", "media": "attach://video"}]},
            result=[
                {"message_id": 30, "photo": [{"file_id": "album-photo"}], "caption": "Private caption"},
                {"message_id": 31, "video": {"file_id": "album-video"}},
            ],
        )
        self.assertEqual(event.payload["telegram_media"], [
            {"file_id": "album-photo", "file_type": "PHOTO"},
            {"file_id": "album-video", "file_type": "VIDEO"},
        ])
        self.assertEqual(event.telegram_message_id, 30)
        self.assertNotIn("caption", event.payload)

    def test_no_media_or_malformed_result_does_not_add_media_or_lose_event(self):
        for result in (None, True, {}, {"message_id": 5, "text": "Hello"}, [None],
                       {"photo": "not-a-photo-list", "video": {"file_id": 12}},
                       {"photo": [{"file_id": " "}], "document": {"file_id": None}}):
            with self.subTest(result=result):
                event = self.log_event(method="sendMessage", payload={"text": "Hello"}, result=result)
                self.assertEqual(event.payload, {"text": "Hello"})
                self.assertEqual(event.text, "Hello")
        self.assertEqual(BotChatEvent.objects.count(), BotOutgoingMessage.objects.count())

    def test_existing_payload_keys_are_never_overwritten(self):
        event = self.log_event(
            payload={"telegram_media": [{"file_id": "already-recorded", "file_type": "PHOTO"}]},
            result={"photo": [{"file_id": "another-id"}]},
        )
        self.assertEqual(event.payload["telegram_media"], [{"file_id": "already-recorded", "file_type": "PHOTO"}])
