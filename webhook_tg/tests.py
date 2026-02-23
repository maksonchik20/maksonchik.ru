from django.test import TestCase
from unittest.mock import patch
import json

from .config import START_PHOTO_ID, START_TEXT
from .models import Message

TELEGRAM_REQUESTS_PATCH = "webhook_tg.telegram.requests.post"


def make_business_message_payload(
    message_id=100001,
    username_from="test_biz_user",
    text="test message text",
    business_connection_id="test_conn_001",
):
    """Payload Telegram с business_message (новое сообщение в бизнес-чате)."""
    return {
        "update_id": 1,
        "business_message": {
            "business_connection_id": business_connection_id,
            "message_id": message_id,
            "from": {
                "id": 200001,
                "is_bot": False,
                "first_name": "Test",
                "last_name": "User",
                "username": username_from,
                "language_code": "ru",
            },
            "chat": {
                "id": 300001,
                "first_name": "TestChat",
                "username": "test_chat",
                "type": "private",
            },
            "date": 1000000,
            "text": text,
        },
    }


def make_edited_business_message_payload(
    message_id=400001,
    username_from="test_editor",
    first_name="TestEditor",
    new_text="edited text",
    business_connection_id="test_conn_edit_001",
    chat_id=500001,
    user_id=500001,
):
    """Payload Telegram с edited_business_message (редактирование в бизнес-чате)."""
    return {
        "update_id": 2,
        "edited_business_message": {
            "business_connection_id": business_connection_id,
            "message_id": message_id,
            "from": {
                "id": user_id,
                "is_bot": False,
                "first_name": first_name,
                "username": username_from,
                "language_code": "ru",
            },
            "chat": {
                "id": chat_id,
                "first_name": first_name,
                "username": username_from,
                "type": "private",
            },
            "date": 2000000,
            "edit_date": 2000001,
            "text": new_text,
        },
    }


def make_deleted_business_messages_payload(
    message_ids=None,
    business_connection_id="test_conn_del_001",
    chat_id=900001,
    first_name="TestDeleter",
    username="test_deleter",
):
    """Payload Telegram с deleted_business_messages (удаление в бизнес-чате)."""
    if message_ids is None:
        message_ids = [600001]
    return {
        "update_id": 3,
        "deleted_business_messages": {
            "business_connection_id": business_connection_id,
            "chat": {
                "id": chat_id,
                "first_name": first_name,
                "username": username,
                "type": "private",
            },
            "message_ids": message_ids,
        },
    }


def make_start_payload(chat_id=600001, user_id=700001, username="testuser"):
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
        chat_id = 900001
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
        payload["message"]["text"] = "other"
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
        message_id = 100010
        username_from = "test_business_user"
        text = "test message body"
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
        message_id = 100011
        username_from = "test_voice_user"
        payload = make_business_message_payload(
            message_id=message_id,
            username_from=username_from,
            text="placeholder",
        )
        payload["business_message"].pop("text")
        payload["business_message"]["voice"] = {
            "duration": 1,
            "mime_type": "audio/ogg",
            "file_id": "test_file_001",
            "file_unique_id": "test_unique_001",
            "file_size": 1000,
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


class WebhookEditedBusinessMessageTests(NoTelegramApiTestCase):
    """Тесты обработки edited_business_message: обновление Message и уведомление пользователю."""

    def test_edited_business_message_updates_text_in_db_and_sends_notification(self):
        """При редактировании: текст в Message обновляется и пользователю уходит уведомление (sendMessage)."""
        message_id = 400010
        old_text = "old text"
        new_text = "new edited text"
        username_from = "test_editor_user"
        first_name = "TestEditor"
        chat_id = 500010
        user_chat_id_notification = 800001
        self.mock_post.return_value.json.return_value = {
            "result": {
                "user_chat_id": user_chat_id_notification,
                "user": {"id": 500010},
            }
        }

        Message.objects.create(
            message_id=message_id,
            username_from=username_from,
            text=old_text,
        )

        payload = make_edited_business_message_payload(
            message_id=message_id,
            username_from=username_from,
            first_name=first_name,
            new_text=new_text,
            chat_id=chat_id,
            user_id=500010,
        )
        response = self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

        # В таблице Message текст обновлён на новый
        msg = Message.objects.get(message_id=message_id)
        self.assertEqual(msg.text, new_text)
        self.assertEqual(msg.username_from, username_from)

        # Вызван sendMessage с уведомлением об редактировании
        send_message_calls = [
            c for c in self.mock_post.call_args_list
            if get_post_call_args(c)[0] and "sendMessage" in str(get_post_call_args(c)[0])
        ]
        self.assertGreaterEqual(
            len(send_message_calls), 1,
            "Должен быть вызов sendMessage с уведомлением пользователю",
        )
        _, body = get_post_call_args(send_message_calls[0])
        self.assertEqual(body.get("chat_id"), user_chat_id_notification)
        notification_text = body.get("text", "")
        self.assertIn("изменил(а) сообщение", notification_text)
        self.assertIn("Old:", notification_text)
        self.assertIn("New:", notification_text)
        self.assertIn(new_text, notification_text)


class WebhookDeletedBusinessMessageTests(NoTelegramApiTestCase):
    """Тесты обработки deleted_business_messages: уведомление пользователю об удалении."""

    def test_deleted_business_messages_sends_notification_with_saved_text(self):
        """При удалении: пользователю уходит sendMessage с текстом удалённых сообщений из БД."""
        message_id = 600010
        saved_text = "deleted message text"
        chat_id = 900010
        first_name = "TestDeleter"
        username = "test_deleter"
        user_chat_id_notification = 950001
        self.mock_post.return_value.json.return_value = {
            "result": {
                "user_chat_id": user_chat_id_notification,
                "user": {"id": 900010},
            }
        }

        Message.objects.create(
            message_id=message_id,
            username_from=username,
            text=saved_text,
        )

        payload = make_deleted_business_messages_payload(
            message_ids=[message_id],
            chat_id=chat_id,
            first_name=first_name,
            username=username,
        )
        response = self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

        send_message_calls = [
            c for c in self.mock_post.call_args_list
            if get_post_call_args(c)[0] and "sendMessage" in str(get_post_call_args(c)[0])
        ]
        self.assertGreaterEqual(
            len(send_message_calls), 1,
            "Должен быть вызов sendMessage с уведомлением об удалении",
        )
        _, body = get_post_call_args(send_message_calls[0])
        self.assertEqual(body.get("chat_id"), user_chat_id_notification)
        notification_text = body.get("text", "")
        self.assertIn("удалил(а)", notification_text)
        self.assertIn(saved_text, notification_text)
        self.assertIn(first_name, notification_text)
        self.assertIn(username, notification_text)

    def test_deleted_business_messages_unknown_id_shows_not_saved_placeholder(self):
        """Если удалённое сообщение не было в БД, в уведомлении — «текст не сохранён»."""
        message_id = 600020
        chat_id = 900020
        user_chat_id_notification = 950002
        self.mock_post.return_value.json.return_value = {
            "result": {
                "user_chat_id": user_chat_id_notification,
                "user": {"id": 900020},
            }
        }

        payload = make_deleted_business_messages_payload(
            message_ids=[message_id],
            chat_id=chat_id,
        )
        response = self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

        send_message_calls = [
            c for c in self.mock_post.call_args_list
            if get_post_call_args(c)[0] and "sendMessage" in str(get_post_call_args(c)[0])
        ]
        self.assertGreaterEqual(len(send_message_calls), 1)
        _, body = get_post_call_args(send_message_calls[0])
        notification_text = body.get("text", "")
        self.assertIn("удалил(а)", notification_text)
        self.assertIn("текст не сохранён", notification_text)
