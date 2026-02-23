from django.test import TestCase
from unittest.mock import patch
import json

from .config import START_PHOTO_ID, START_TEXT
from .models import Message

TELEGRAM_REQUESTS_PATCH = "webhook_tg.telegram.requests.post"


def make_business_message_payload(
    message_id=777,
    username_from="test_biz_user",
    text="Тестовый текст сообщения",
    business_connection_id="test_conn_123",
):
    """Payload Telegram с business_message (новое сообщение в бизнес-чате)."""
    return {
        "update_id": 1,
        "business_message": {
            "business_connection_id": business_connection_id,
            "message_id": message_id,
            "from": {
                "id": 111222,
                "is_bot": False,
                "first_name": "Test",
                "last_name": "User",
                "username": username_from,
                "language_code": "ru",
            },
            "chat": {
                "id": 999888,
                "first_name": "Test Chat",
                "username": "test_chat",
                "type": "private",
            },
            "date": 1771859306,
            "text": text,
        },
    }


def make_start_payload(chat_id=12345, user_id=67890, username="testuser"):
    """Payload Telegram для сообщения /start боту (message, не business_message)."""
    return {
        "message": {
            "message_id": 1,
            "from": {"id": user_id, "username": username, "first_name": "Test"},
            "chat": {"id": chat_id, "type": "private"},
            "text": "/start",
        }
    }


def get_post_call_args(call):
    """Из вызова mock_post извлекает (url, json_body)."""
    args, kwargs = call[0], call[1]
    url = args[0] if args else kwargs.get("url")
    body = kwargs.get("json", {})
    return url, body


class NoTelegramApiTestCase(TestCase):
    """Базовый класс: мокаем все вызовы к Telegram API (requests.post в webhook_tg.telegram)."""

    def setUp(self):
        super().setUp()
        self._requests_patcher = patch(TELEGRAM_REQUESTS_PATCH)
        self.mock_post = self._requests_patcher.start()
        # Для get_business_connection: вызывается .json() у ответа
        self.mock_post.return_value.json.return_value = {
            "result": {
                "user_chat_id": 0,
                "user": {"id": 0},
            }
        }

    def tearDown(self):
        self._requests_patcher.stop()
        super().tearDown()


class WebhookStartTests(NoTelegramApiTestCase):
    """Тесты обработки /start: проверяем URL и JSON, передаваемые в requests.post."""

    def test_start_calls_post_with_send_photo_url_and_correct_json(self):
        """При /start вызывается requests.post с URL sendPhoto и верным json (chat_id, caption, photo)."""
        chat_id = 999
        payload = make_start_payload(chat_id=chat_id)
        response = self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.mock_post.called, "requests.post должен быть вызван при /start")

        # Ищем вызов sendPhoto
        send_photo_call = None
        for call in self.mock_post.call_args_list:
            url, _ = get_post_call_args(call)
            if url and "sendPhoto" in str(url):
                send_photo_call = call
                break
        self.assertIsNotNone(send_photo_call, "Должен быть вызов с URL sendPhoto")

        url, body = get_post_call_args(send_photo_call)

        self.assertIn("sendPhoto", str(url), f"URL должен содержать sendPhoto: {url}")

        self.assertEqual(body.get("chat_id"), chat_id, "В json должен передаваться chat_id из сообщения")
        self.assertEqual(body.get("caption"), START_TEXT, "В json должен передаваться caption = START_TEXT")
        self.assertEqual(body.get("photo"), START_PHOTO_ID, "В json должен передаваться photo = START_PHOTO_ID")
        self.assertEqual(body.get("parse_mode"), "HTML", "В json должен быть parse_mode HTML")
        self.assertTrue(body.get("disable_web_page_preview"), "В json должен быть disable_web_page_preview True")

    def test_start_does_not_send_on_non_start_message(self):
        """Если текст не /start, отправка сообщения не вызывается."""
        payload = make_start_payload()
        payload["message"]["text"] = "привет"
        self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertFalse(
            self.mock_post.called,
            "requests.post не должен вызываться при сообщении не /start",
        )


class WebhookBusinessMessageTests(NoTelegramApiTestCase):
    """Тесты обработки нового сообщения из business_message: запись в таблицу Message."""

    def test_business_message_creates_message_with_username_text_message_id(self):
        """Новое business_message добавляется в Message с нужным username_from, text и message_id."""
        message_id = 558634
        username_from = "test_business_user"
        text = "Привет, это тестовое сообщение"
        payload = make_business_message_payload(
            message_id=message_id,
            username_from=username_from,
            text=text,
        )
        response = self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

        msg = Message.objects.get(message_id=message_id)
        self.assertEqual(msg.username_from, username_from)
        self.assertEqual(msg.text, text)
        self.assertEqual(msg.message_id, message_id)

    def test_business_message_without_text_uses_default_caption(self):
        """business_message без text (например голосовое) сохраняется с текстом по умолчанию."""
        message_id = 100501
        username_from = "voice_user"
        payload = make_business_message_payload(
            message_id=message_id,
            username_from=username_from,
            text="Тестовый текст сообщения",
        )
        payload["business_message"].pop("text")
        payload["business_message"]["audio"] = {
            "duration": 15,
            "mime_type": "audio/ogg",
            "file_id": "test_file_id",
            "file_unique_id": "test_unique",
            "file_size": 55727,
        }
        response = self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

        msg = Message.objects.get(message_id=message_id)
        self.assertEqual(msg.username_from, username_from)
        self.assertEqual(msg.text, "Не текстовое сообщение")
        self.assertEqual(msg.message_id, message_id)
