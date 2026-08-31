from django.test import TestCase, override_settings
from django.utils import timezone
from unittest.mock import patch
from datetime import timedelta
import json

from .config import (
    DEMO_ALBUM_CAPTION,
    DEMO_CALLBACK_DATA,
    DEMO_VIDEOS,
    PROFILE_SETTINGS_URL,
    START_PHOTO_ID,
    START_REPLY_MARKUP,
    START_TEXT,
)
from .models import (
    BackgroundTask,
    Message,
    FileType,
    TelegramIncomingUpdate,
    TelegramOutbox,
    UserTg,
    WhoUpdateOnboardingFunnel,
)
from .outbox import process_outbox
from .incoming import claim_next_update, process_claimed_update

TELEGRAM_REQUESTS_PATCH = "webhook_tg.telegram.TELEGRAM_SESSION.post"
WHO_UPDATE_BOT_LINK = '<a href="https://t.me/who_update_bot">@who_update_bot</a>'


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


def make_start_payload(chat_id=600001, user_id=700001, username="testuser", update_id=100):
    """Payload Telegram для сообщения /start боту (message, не business_message)."""
    return {
        "update_id": update_id,
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


@override_settings(
    TELEGRAM_WEBHOOK_SYNC_PROCESSING=True,
    TELEGRAM_WEBHOOK_SECRET_REQUIRED=False,
)
class NoTelegramApiTestCase(TestCase):
    """Базовый класс: мокаем все вызовы к Telegram API (requests.post в webhook_tg.telegram)."""

    def setUp(self):
        super().setUp()
        self._requests_patcher = patch(TELEGRAM_REQUESTS_PATCH)
        self.mock_post = self._requests_patcher.start()
        # Для get_business_connection: вызывается .json() у ответа
        self.mock_post.return_value.json.return_value = {
            "ok": True,
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
        self.assertEqual(body.get("reply_markup"), START_REPLY_MARKUP)
        start_keyboard = body["reply_markup"]["inline_keyboard"]
        self.assertEqual(len(start_keyboard), 2)
        self.assertEqual(len(start_keyboard[0]), 1)
        self.assertEqual(len(start_keyboard[1]), 1)
        self.assertEqual(start_keyboard[0][0]["text"], "🎬 Демонстрация работы бота")
        self.assertEqual(start_keyboard[1][0]["text"], "🟢 Подключить")
        self.assertEqual(start_keyboard[1][0]["url"], PROFILE_SETTINGS_URL)
        self.assertEqual(PROFILE_SETTINGS_URL, "tg://settings/edit")

        user = UserTg.objects.get(user_id=700001)
        self.assertFalse(user.business_is_connected)
        self.assertIsNotNone(user.last_start_at)
        reminders = list(
            BackgroundTask.objects.filter(
                task_type="send_connection_reminder",
                payload__user_pk=user.pk,
                status=BackgroundTask.Status.PENDING,
            ).order_by("run_at")
        )
        self.assertEqual(len(reminders), 2)
        self.assertAlmostEqual(
            (reminders[0].run_at - user.last_start_at).total_seconds(),
            timedelta(minutes=30).total_seconds(),
            delta=1,
        )
        self.assertAlmostEqual(
            (reminders[1].run_at - user.last_start_at).total_seconds(),
            timedelta(days=1).total_seconds(),
            delta=1,
        )
        self.assertTrue(all(task.idempotency_key for task in reminders))

        telegram_calls = [
            call
            for call in self.mock_post.call_args_list
            if (
                "api.telegram.org" in str(get_post_call_args(call)[0])
                and str(get_post_call_args(call)[1].get("chat_id")) == str(chat_id)
            )
        ]
        self.assertEqual(len(telegram_calls), 1)
        self.assertIn("sendPhoto", str(get_post_call_args(telegram_calls[0])[0]))

    def test_start_sends_only_welcome_photo_to_rollout_owner(self):
        UserTg.objects.create(
            user_id=1394340082,
            chat_id=1394340082,
            username="maksonchik200",
        )
        payload = make_start_payload(
            chat_id=1394340082,
            user_id=1394340082,
            username="maksonchik200",
            update_id=102,
        )

        response = self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        telegram_calls = [
            call
            for call in self.mock_post.call_args_list
            if (
                "api.telegram.org" in str(get_post_call_args(call)[0])
                and str(get_post_call_args(call)[1].get("chat_id")) == "1394340082"
            )
        ]
        self.assertEqual(len(telegram_calls), 1)
        url, body = get_post_call_args(telegram_calls[0])
        self.assertIn("sendPhoto", str(url))
        self.assertEqual(body.get("caption"), START_TEXT)

    def test_start_does_not_schedule_reminder_for_connected_user(self):
        UserTg.objects.create(
            user_id=700002,
            chat_id=600002,
            username="connected",
            business_is_connected=True,
            business_connected_at=timezone.now(),
        )
        payload = make_start_payload(
            chat_id=600002,
            user_id=700002,
            username="connected",
            update_id=101,
        )

        response = self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        user = UserTg.objects.get(user_id=700002)
        self.assertTrue(user.business_is_connected)
        self.assertIsNone(user.connection_reminder_at)
        self.assertFalse(
            BackgroundTask.objects.filter(
                task_type="send_connection_reminder",
                payload__user_pk=user.pk,
                status=BackgroundTask.Status.PENDING,
            ).exists()
        )

    def test_referral_start_notification_contains_inviter_username(self):
        inviter = UserTg.objects.create(
            user_id=710001,
            chat_id=610001,
            username="coffee_inviter",
            access_unlimited=False,
            access_expires_at=timezone.now() + timedelta(days=7),
        )
        payload = make_start_payload(
            chat_id=610002,
            user_id=710002,
            username="new_referral",
            update_id=103,
        )
        payload["message"]["text"] = f"/start ref_{inviter.referral_code}"

        response = self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        owner_texts = [
            get_post_call_args(call)[1].get("text", "")
            for call in self.mock_post.call_args_list
            if str(get_post_call_args(call)[1].get("chat_id")) == "1394340082"
        ]
        referral_notification = next(
            text for text in owner_texts if "Новый пользователь WhoUpdate" in text
        )
        self.assertIn("Источник: реферальная ссылка", referral_notification)
        self.assertIn("Пригласил: @coffee_inviter", referral_notification)
        self.assertIn("Username: @new_referral", referral_notification)

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

    def test_demo_button_sends_three_videos_as_one_media_group(self):
        payload = {
            "update_id": 104,
            "callback_query": {
                "id": "demo_callback_1",
                "from": {"id": 700001, "username": "testuser"},
                "message": {"chat": {"id": 600001, "type": "private"}},
                "data": DEMO_CALLBACK_DATA,
            },
        }

        response = self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        group_bodies = [
            get_post_call_args(call)[1]
            for call in self.mock_post.call_args_list
            if "sendMediaGroup" in str(get_post_call_args(call)[0])
        ]
        self.assertEqual(len(group_bodies), 1)
        media = group_bodies[0]["media"]
        self.assertEqual(len(media), 3)
        self.assertEqual(
            [item["media"] for item in media],
            [video["file_id"] for video in DEMO_VIDEOS],
        )
        self.assertEqual(media[0]["caption"], DEMO_ALBUM_CAPTION)
        self.assertEqual(media[0]["parse_mode"], "HTML")
        self.assertNotIn("caption", media[1])
        self.assertNotIn("caption", media[2])
        self.assertTrue(all(item["type"] == "video" for item in media))
        self.assertTrue(
            any(
                "answerCallbackQuery" in str(get_post_call_args(call)[0])
                for call in self.mock_post.call_args_list
            )
        )

    def test_history_without_username_sends_usage_hint(self):
        payload = make_start_payload(update_id=105)
        payload["message"]["text"] = "/history"

        response = self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        messages = [
            get_post_call_args(call)[1].get("text", "")
            for call in self.mock_post.call_args_list
            if "sendMessage" in str(get_post_call_args(call)[0])
        ]
        self.assertTrue(any("Укажите username" in message for message in messages))
        self.assertTrue(any("/history @username" in message for message in messages))

    def test_history_sends_only_current_owners_messages_as_txt(self):
        UserTg.objects.create(
            user_id=700001,
            chat_id=600001,
            username="testuser",
            first_name="Test",
            business_connection_id="owner_conn",
        )
        Message.objects.create(
            business_connection_id="owner_conn",
            message_id=201,
            chat_id=301,
            username_from="Target_User",
            first_name="Target",
            text="Первое сообщение",
        )
        Message.objects.create(
            business_connection_id="owner_conn",
            message_id=202,
            chat_id=301,
            username_from="target_user",
            first_name="Target",
            text="Второе сообщение",
            file_id="document-file-id",
            file_type=FileType.DOCUMENT,
        )
        Message.objects.create(
            business_connection_id="another_conn",
            message_id=203,
            chat_id=302,
            username_from="target_user",
            text="Чужая переписка",
        )
        Message.objects.create(
            business_connection_id="owner_conn",
            message_id=204,
            chat_id=303,
            username_from="another_user",
            text="Другой пользователь",
        )
        payload = make_start_payload(update_id=106)
        payload["message"]["text"] = "/history @TARGET_USER"

        response = self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        document_calls = [
            call
            for call in self.mock_post.call_args_list
            if "sendDocument" in str(get_post_call_args(call)[0])
        ]
        self.assertEqual(len(document_calls), 1)
        call = document_calls[0]
        filename, content, content_type = call.kwargs["files"]["document"]
        self.assertEqual(filename, "history_target_user.txt")
        self.assertEqual(content_type, "text/plain; charset=utf-8")
        archive = content.decode("utf-8-sig")
        self.assertIn("Сообщений: 2", archive)
        self.assertIn("Первое сообщение", archive)
        self.assertIn("Второе сообщение", archive)
        self.assertIn("[вложение: DOCUMENT]", archive)
        self.assertNotIn("Чужая переписка", archive)
        self.assertNotIn("Другой пользователь", archive)


@override_settings(
    TELEGRAM_WEBHOOK_SYNC_PROCESSING=False,
    TELEGRAM_WEBHOOK_SECRET_REQUIRED=False,
)
class IncomingWebhookQueueTests(TestCase):
    def test_start_is_enqueued_in_priority_queue_without_api_call(self):
        payload = make_start_payload(update_id=880001)
        with patch(TELEGRAM_REQUESTS_PATCH) as mock_post:
            response = self.client.post(
                "/webhook_tg/",
                data=json.dumps(payload),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        item = TelegramIncomingUpdate.objects.get(update_id=880001)
        self.assertEqual(item.queue, TelegramIncomingUpdate.Queue.PRIORITY)
        self.assertEqual(item.status, TelegramIncomingUpdate.Status.PENDING)
        mock_post.assert_not_called()

    def test_business_message_is_enqueued_in_business_queue(self):
        payload = make_business_message_payload(message_id=880002)
        payload["update_id"] = 880002
        response = self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        item = TelegramIncomingUpdate.objects.get(update_id=880002)
        self.assertEqual(item.queue, TelegramIncomingUpdate.Queue.BUSINESS)

    def test_duplicate_update_is_stored_once(self):
        payload = make_start_payload(update_id=880003)
        for _ in range(2):
            response = self.client.post(
                "/webhook_tg/",
                data=json.dumps(payload),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 200)

        self.assertEqual(TelegramIncomingUpdate.objects.filter(update_id=880003).count(), 1)

    def test_priority_worker_sends_start_photo_and_marks_update_done(self):
        payload = make_start_payload(update_id=880005)
        self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        item = claim_next_update(TelegramIncomingUpdate.Queue.PRIORITY)
        self.assertIsNotNone(item)

        with patch(TELEGRAM_REQUESTS_PATCH) as mock_post:
            mock_post.return_value.json.return_value = {"ok": True, "result": {}}
            self.assertTrue(process_claimed_update(item))

        item.refresh_from_db()
        self.assertEqual(item.status, TelegramIncomingUpdate.Status.DONE)
        send_photo_calls = [
            call
            for call in mock_post.call_args_list
            if "sendPhoto" in str(get_post_call_args(call)[0])
        ]
        self.assertEqual(len(send_photo_calls), 1)

    def test_start_timeout_is_queued_and_input_is_marked_done(self):
        payload = make_start_payload(update_id=880006)
        UserTg.objects.create(
            user_id=payload["message"]["from"]["id"],
            chat_id=payload["message"]["chat"]["id"],
            username=payload["message"]["from"]["username"],
        )
        self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        item = claim_next_update(TelegramIncomingUpdate.Queue.PRIORITY)

        with patch(
            "webhook_tg.outbox.dispatch_telegram_request",
            return_value=(False, "Telegram API timeout"),
        ):
            self.assertTrue(process_claimed_update(item))

        item.refresh_from_db()
        self.assertEqual(item.status, TelegramIncomingUpdate.Status.DONE)
        queued = TelegramOutbox.objects.get(
            idempotency_key="command:880006:start-photo"
        )
        self.assertEqual(queued.status, TelegramOutbox.Status.PENDING)
        self.assertEqual(queued.attempts, 1)
        self.assertEqual(
            WhoUpdateOnboardingFunnel.objects.filter(start_update_id=880006).count(),
            1,
        )

        TelegramOutbox.objects.filter(pk=queued.pk).update(
            next_attempt_at=timezone.now() - timedelta(seconds=1)
        )
        with patch(
            "webhook_tg.outbox.dispatch_telegram_request",
            return_value=(True, ""),
        ) as dispatch_mock:
            stats = process_outbox()
        self.assertEqual(stats["sent"], 1)
        self.assertEqual(dispatch_mock.call_count, 1)

        from .views import process_telegram_update

        with patch(
            "webhook_tg.outbox.dispatch_telegram_request",
            return_value=(True, ""),
        ) as duplicate_dispatch:
            process_telegram_update(payload, use_idempotency=False)
        duplicate_dispatch.assert_not_called()
        self.assertEqual(
            WhoUpdateOnboardingFunnel.objects.filter(start_update_id=880006).count(),
            1,
        )

    def test_blocked_start_is_dropped_without_retrying_input(self):
        payload = make_start_payload(update_id=880007)
        UserTg.objects.create(
            user_id=payload["message"]["from"]["id"],
            chat_id=payload["message"]["chat"]["id"],
            username=payload["message"]["from"]["username"],
        )
        self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        item = claim_next_update(TelegramIncomingUpdate.Queue.PRIORITY)

        with patch(
            "webhook_tg.outbox.dispatch_telegram_request",
            return_value=(False, "Forbidden: bot was blocked by the user"),
        ):
            self.assertTrue(process_claimed_update(item))

        item.refresh_from_db()
        self.assertEqual(item.status, TelegramIncomingUpdate.Status.DONE)
        delivery = TelegramOutbox.objects.get(
            idempotency_key="command:880007:start-photo"
        )
        self.assertEqual(delivery.status, TelegramOutbox.Status.DROPPED)
        self.assertEqual(delivery.attempts, 0)


@override_settings(
    TELEGRAM_WEBHOOK_SYNC_PROCESSING=False,
    TELEGRAM_WEBHOOK_SECRET_REQUIRED=True,
)
class WebhookSecretTests(TestCase):
    def test_missing_secret_is_rejected(self):
        response = self.client.post(
            "/webhook_tg/",
            data=json.dumps(make_start_payload(update_id=880004)),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)


class BusinessConnectionActivatedTests(NoTelegramApiTestCase):
    def test_enabled_connection_sends_activated_message(self):
        from webhook_tg.config import BOT_ACTIVATED_TEXT, OWNER_CHAT_ID

        self.mock_post.return_value.json.return_value = {"ok": True, "result": True}
        payload = {
            "update_id": 94001,
            "business_connection": {
                "id": "conn_act_1",
                "user": {
                    "id": 1394340082,
                    "is_bot": False,
                    "first_name": "Max",
                    "username": "maksonchik200",
                },
                "user_chat_id": 1394340082,
                "date": 1700000000,
                "is_enabled": True,
            },
        }
        response = self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        send_calls = [
            c for c in self.mock_post.call_args_list
            if get_post_call_args(c)[0] and "sendMessage" in str(get_post_call_args(c)[0])
        ]
        activated = [
            get_post_call_args(c)[1]
            for c in send_calls
            if get_post_call_args(c)[1].get("text") == BOT_ACTIVATED_TEXT
        ]
        self.assertEqual(len(activated), 1)
        self.assertEqual(int(activated[0].get("chat_id")), 1394340082)
        user_texts = [
            get_post_call_args(c)[1].get("text", "")
            for c in send_calls
            if str(get_post_call_args(c)[1].get("chat_id")) == "1394340082"
        ]
        self.assertFalse(any("Доступ к WhoUpdate" in text for text in user_texts))

        owner_notifications = [
            get_post_call_args(c)[1]
            for c in send_calls
            if "WhoUpdate полностью подключён" in get_post_call_args(c)[1].get("text", "")
        ]
        self.assertEqual(len(owner_notifications), 1)
        self.assertEqual(str(owner_notifications[0].get("chat_id")), str(OWNER_CHAT_ID))
        owner_text = owner_notifications[0]["text"]
        self.assertIn("@maksonchik200", owner_text)
        self.assertIn("Telegram ID: <code>1394340082</code>", owner_text)
        self.assertIn("Business connection ID: <code>conn_act_1</code>", owner_text)
        stored_user = UserTg.objects.get(user_id=1394340082)
        self.assertTrue(stored_user.business_is_connected)
        self.assertEqual(stored_user.business_connection_id, "conn_act_1")
        self.assertIsNotNone(stored_user.business_connected_at)
        self.assertIsNone(stored_user.connection_reminder_at)

    def test_duplicate_enabled_connection_does_not_send_notifications_again(self):
        self.mock_post.return_value.json.return_value = {"ok": True, "result": True}
        connected_at = timezone.now() - timedelta(hours=1)
        UserTg.objects.create(
            user_id=1394340083,
            chat_id=1394340083,
            username="already_connected",
            first_name="Already",
            business_connection_id="conn_duplicate_1",
            business_is_connected=True,
            business_connected_at=connected_at,
        )
        payload = {
            "update_id": 94005,
            "business_connection": {
                "id": "conn_duplicate_1",
                "user": {
                    "id": 1394340083,
                    "is_bot": False,
                    "first_name": "Already",
                    "username": "already_connected",
                },
                "user_chat_id": 1394340083,
                "date": 1700000000,
                "is_enabled": True,
            },
        }

        response = self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        texts = [
            get_post_call_args(call)[1].get("text", "")
            for call in self.mock_post.call_args_list
            if get_post_call_args(call)[0] and "sendMessage" in str(get_post_call_args(call)[0])
        ]
        self.assertFalse(any("WhoUpdate успешно активирован" in text for text in texts))
        self.assertFalse(any("WhoUpdate полностью подключён" in text for text in texts))
        stored_user = UserTg.objects.get(user_id=1394340083)
        self.assertEqual(stored_user.business_connected_at, connected_at)

    def test_disabled_connection_does_not_notify_owner_about_activation(self):
        self.mock_post.return_value.json.return_value = {"ok": True, "result": True}
        payload = {
            "update_id": 94002,
            "business_connection": {
                "id": "conn_disabled_1",
                "user": {
                    "id": 812345,
                    "first_name": "Test",
                    "username": "test_owner",
                },
                "user_chat_id": 812345,
                "is_enabled": False,
            },
        }

        response = self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        stored_user = UserTg.objects.get(user_id=812345)
        self.assertFalse(stored_user.business_is_connected)
        self.assertEqual(stored_user.business_connection_id, "conn_disabled_1")
        self.assertIsNotNone(stored_user.business_disconnected_at)
        self.assertIsNone(stored_user.connection_reminder_at)
        texts = [
            get_post_call_args(c)[1].get("text", "")
            for c in self.mock_post.call_args_list
            if get_post_call_args(c)[0] and "sendMessage" in str(get_post_call_args(c)[0])
        ]
        self.assertFalse(any("WhoUpdate полностью подключён" in text for text in texts))
        self.assertFalse(any("WhoUpdate отключён" in text for text in texts))

    def test_disabling_connected_user_notifies_owner(self):
        from webhook_tg.config import OWNER_CHAT_ID

        self.mock_post.return_value.json.return_value = {"ok": True, "result": True}
        UserTg.objects.create(
            user_id=812346,
            chat_id=812346,
            username="disconnected_owner",
            first_name="Disconnected",
            business_connection_id="conn_disabled_2",
            business_is_connected=True,
            business_connected_at=timezone.now() - timedelta(hours=1),
        )
        payload = {
            "update_id": 94004,
            "business_connection": {
                "id": "conn_disabled_2",
                "user": {
                    "id": 812346,
                    "first_name": "Disconnected",
                    "last_name": "User",
                    "username": "disconnected_owner",
                },
                "user_chat_id": 812346,
                "is_enabled": False,
            },
        }

        response = self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        stored_user = UserTg.objects.get(user_id=812346)
        self.assertFalse(stored_user.business_is_connected)
        owner_notifications = [
            get_post_call_args(call)[1]
            for call in self.mock_post.call_args_list
            if "WhoUpdate отключён" in get_post_call_args(call)[1].get("text", "")
            and str(get_post_call_args(call)[1].get("chat_id")) == str(OWNER_CHAT_ID)
        ]
        self.assertEqual(len(owner_notifications), 1)
        owner_text = owner_notifications[0]["text"]
        self.assertIn("Пользователь: Disconnected User", owner_text)
        self.assertIn("Username: @disconnected_owner", owner_text)
        self.assertIn("Telegram ID: <code>812346</code>", owner_text)
        self.assertIn("Business connection ID: <code>conn_disabled_2</code>", owner_text)
        disconnected_at = stored_user.business_disconnected_at

        self.mock_post.reset_mock()
        payload["update_id"] = 94006
        repeated_response = self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(repeated_response.status_code, 200)
        repeated_texts = [
            get_post_call_args(call)[1].get("text", "")
            for call in self.mock_post.call_args_list
            if get_post_call_args(call)[0] and "sendMessage" in str(get_post_call_args(call)[0])
        ]
        self.assertFalse(any("WhoUpdate отключён" in text for text in repeated_texts))
        stored_user.refresh_from_db()
        self.assertEqual(stored_user.business_disconnected_at, disconnected_at)

    def test_referral_connection_notification_contains_inviter_username(self):
        self.mock_post.return_value.json.return_value = {"ok": True, "result": True}
        inviter = UserTg.objects.create(
            user_id=820001,
            chat_id=820001,
            username="restaurant_inviter",
            access_unlimited=False,
            access_expires_at=timezone.now() + timedelta(days=7),
        )
        UserTg.objects.create(
            user_id=820002,
            chat_id=820002,
            username="connected_referral",
            referred_by=inviter,
            access_unlimited=False,
            access_expires_at=timezone.now() + timedelta(days=7),
        )
        payload = {
            "update_id": 94003,
            "business_connection": {
                "id": "conn_referral_1",
                "user": {
                    "id": 820002,
                    "first_name": "Referral",
                    "username": "connected_referral",
                },
                "user_chat_id": 820002,
                "is_enabled": True,
            },
        }

        response = self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        texts = [
            get_post_call_args(call)[1].get("text", "")
            for call in self.mock_post.call_args_list
        ]
        notification = next(text for text in texts if "WhoUpdate полностью подключён" in text)
        self.assertIn("Источник: реферальная ссылка", notification)
        self.assertIn("Пригласил: @restaurant_inviter", notification)
        self.assertIn("Username: @connected_referral", notification)


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

        msg = Message.objects.get(chat_id=300001, message_id=message_id)
        self.assertEqual(msg.username_from, username_from)
        self.assertEqual(msg.text, text)
        self.assertEqual(msg.message_id, message_id)

    def test_business_message_saves_payload(self):
        """При создании business_message в Message записывается payload (исходный dict сообщения)."""
        message_id = 100019
        username_from = "payload_user"
        text = "payload test text"
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
        msg = Message.objects.get(chat_id=300001, message_id=message_id)
        self.assertIsNotNone(msg.payload, "payload должен быть записан")
        self.assertIn(str(message_id), msg.payload)
        self.assertIn(username_from, msg.payload)
        self.assertIn(text, msg.payload)

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

        msg = Message.objects.get(chat_id=300001, message_id=message_id)
        self.assertEqual(msg.username_from, username_from)
        self.assertEqual(msg.text, "")
        self.assertEqual(msg.message_id, message_id)
        self.assertEqual(msg.file_id, "test_file_001")
        self.assertEqual(msg.file_type, FileType.AUDIO)

    def test_business_message_with_photo_saves_last_file_id_and_caption(self):
        """business_message с фото сохраняет последний file_id из списка и caption."""
        message_id = 100012
        payload = make_business_message_payload(
            message_id=message_id,
            username_from="photo_user",
            text=None,
        )
        payload["business_message"].pop("text")
        payload["business_message"]["photo"] = [
            {"file_id": "photo_small", "width": 90, "height": 90},
            {"file_id": "photo_medium", "width": 320, "height": 320},
            {"file_id": "photo_large", "width": 1280, "height": 1280},
        ]
        payload["business_message"]["caption"] = "подпись к фото"
        response = self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        msg = Message.objects.get(chat_id=300001, message_id=message_id)
        self.assertEqual(msg.file_id, "photo_large")
        self.assertEqual(msg.file_type, FileType.PHOTO)
        self.assertEqual(msg.caption, "подпись к фото")
        self.assertEqual(msg.text, "подпись к фото")

    def test_business_message_with_video_saves_file_id_and_file_type(self):
        """business_message с видео сохраняет file_id и file_type VIDEO."""
        message_id = 100013
        payload = make_business_message_payload(
            message_id=message_id,
            username_from="video_user",
            text=None,
        )
        payload["business_message"].pop("text")
        payload["business_message"]["video"] = {
            "file_id": "video_file_123",
            "duration": 10,
            "width": 720,
            "height": 1280,
        }
        payload["business_message"]["caption"] = "видео подпись"
        response = self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        msg = Message.objects.get(chat_id=300001, message_id=message_id)
        self.assertEqual(msg.file_id, "video_file_123")
        self.assertEqual(msg.file_type, FileType.VIDEO)
        self.assertEqual(msg.caption, "видео подпись")

    def test_business_message_with_document_saves_file_id_and_file_type(self):
        """business_message с документом сохраняет file_id и file_type DOCUMENT."""
        message_id = 100014
        payload = make_business_message_payload(
            message_id=message_id,
            username_from="doc_user",
            text="текст",
        )
        payload["business_message"]["document"] = {
            "file_id": "doc_file_456",
            "file_name": "file.pdf",
        }
        response = self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        msg = Message.objects.get(chat_id=300001, message_id=message_id)
        self.assertEqual(msg.file_id, "doc_file_456")
        self.assertEqual(msg.file_type, FileType.DOCUMENT)

    def test_business_message_with_video_sticker_saves_file_id(self):
        """video-стикер сохраняет file_id и file_type STICKER."""
        message_id = 100015
        payload = make_business_message_payload(
            message_id=message_id,
            username_from="sticker_user",
            text="placeholder",
        )
        payload["business_message"].pop("text")
        payload["business_message"]["sticker"] = {
            "width": 512,
            "height": 512,
            "is_video": True,
            "file_id": "sticker_video_file_789",
            "file_unique_id": "uniq_sticker",
        }
        response = self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        msg = Message.objects.get(chat_id=300001, message_id=message_id)
        self.assertEqual(msg.file_id, "sticker_video_file_789")
        self.assertEqual(msg.file_type, FileType.STICKER)


class WebhookEditedBusinessMessageTests(NoTelegramApiTestCase):
    """Тесты обработки edited_business_message: обновление Message и уведомление пользователю."""

    def test_edited_business_message_updates_text_in_db_and_sends_notification(self):
        """При редактировании собеседником: текст в Message обновляется и владельцу уходит уведомление."""
        message_id = 400010
        old_text = "old text"
        new_text = "new edited text"
        username_from = "test_editor_user"
        first_name = "TestEditor"
        chat_id = 500010
        owner_id = 800001
        user_chat_id_notification = 800001
        self.mock_post.return_value.json.return_value = {
            "result": {
                "user_chat_id": user_chat_id_notification,
                "user": {"id": owner_id},
            }
        }

        Message.objects.create(
            message_id=message_id,
            chat_id=chat_id,
            username_from=username_from,
            text=old_text,
            business_connection_id="test_conn_edit_001",
        )

        payload = make_edited_business_message_payload(
            message_id=message_id,
            username_from=username_from,
            first_name=first_name,
            new_text=new_text,
            chat_id=chat_id,
            user_id=600010,
        )
        response = self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

        msg = Message.objects.get(chat_id=chat_id, message_id=message_id)
        self.assertEqual(msg.text, new_text)
        self.assertEqual(msg.username_from, username_from)

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
        self.assertTrue(notification_text.endswith(WHO_UPDATE_BOT_LINK))

    def test_edited_business_message_skips_notification_when_owner_edits(self):
        """Когда владелец business-аккаунта редактирует — себе и собеседнику не шлём (только через чужое подключение)."""
        message_id = 400011
        old_text = "аоаоаоа"
        new_text = "бобобобо"
        partner_chat_id = 870546616
        owner_id = 1394340082
        owner_user_chat_id = 1394340082
        self.mock_post.return_value.json.return_value = {
            "result": {
                "user_chat_id": owner_user_chat_id,
                "user": {"id": owner_id, "username": "maksonchik200"},
            }
        }

        Message.objects.create(
            message_id=message_id,
            chat_id=partner_chat_id,
            username_from="maksonchik200",
            text=old_text,
            business_connection_id="test_conn_owner_edit",
        )

        payload = make_edited_business_message_payload(
            message_id=message_id,
            username_from="maksonchik200",
            first_name="Максим",
            new_text=new_text,
            chat_id=partner_chat_id,
            user_id=owner_id,
            business_connection_id="test_conn_owner_edit",
        )
        payload["update_id"] = 963538804
        response = self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

        from webhook_tg.models import TelegramOutbox
        self.assertEqual(TelegramOutbox.objects.count(), 0)

        process_outbox()

        send_message_calls = [
            c for c in self.mock_post.call_args_list
            if get_post_call_args(c)[0] and "sendMessage" in str(get_post_call_args(c)[0])
        ]
        self.assertEqual(len(send_message_calls), 0)


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
            business_connection_id="test_conn_del_001",
            chat_id=chat_id,
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
        self.assertTrue(notification_text.endswith(WHO_UPDATE_BOT_LINK))

    def test_deleted_own_message_by_owner_skips_notification(self):
        """Удаление своего сообщения владельцем business — уведомление не шлём."""
        message_id = 600011
        chat_id = 900011
        owner_id = 1394340082
        owner_username = "maksonchik200"
        owner_user_chat_id = 1394340082
        self.mock_post.return_value.json.return_value = {
            "result": {
                "user_chat_id": owner_user_chat_id,
                "user": {"id": owner_id, "username": owner_username},
            }
        }

        Message.objects.create(
            message_id=message_id,
            username_from=owner_username,
            text="my own message",
            business_connection_id="test_conn_del_own",
            chat_id=chat_id,
        )

        payload = make_deleted_business_messages_payload(
            message_ids=[message_id],
            chat_id=chat_id,
            first_name="Partner",
            username="partner_user",
            business_connection_id="test_conn_del_own",
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
        self.assertEqual(
            len(send_message_calls),
            0,
            "При удалении своего сообщения владельцу не шлём уведомление",
        )

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

    def test_bulk_delete_sends_one_txt_archive_with_all_messages(self):
        """Удаление более 10 сообщений отправляет один TXT со всем событием."""
        chat_id = 900030
        user_chat_id_notification = 950003
        self.mock_post.return_value.json.return_value = {
            "ok": True,
            "result": {
                "user_chat_id": user_chat_id_notification,
                "user": {"id": chat_id},
            }
        }
        message_ids = list(range(600100, 600125))
        Message.objects.bulk_create(
            [
                Message(
                    message_id=message_id,
                    username_from="history_user",
                    first_name="History User",
                    text=f"history text {message_id}",
                    business_connection_id="test_conn_del_001",
                    chat_id=chat_id,
                )
                for message_id in message_ids
            ]
        )
        payload = make_deleted_business_messages_payload(
            message_ids=message_ids,
            chat_id=chat_id,
        )
        response = self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        send_document_calls = [
            c for c in self.mock_post.call_args_list
            if get_post_call_args(c)[0] and "sendDocument" in str(get_post_call_args(c)[0])
        ]
        self.assertEqual(len(send_document_calls), 1)
        call = send_document_calls[0]
        self.assertEqual(call.kwargs["data"]["chat_id"], user_chat_id_notification)
        self.assertTrue(
            call.kwargs["data"]["caption"].endswith(WHO_UPDATE_BOT_LINK)
        )
        self.assertEqual(call.kwargs["data"]["parse_mode"], "HTML")
        filename, content, content_type = call.kwargs["files"]["document"]
        self.assertTrue(filename.endswith(".txt"))
        self.assertEqual(content_type, "text/plain; charset=utf-8")
        archive = content.decode("utf-8-sig")
        self.assertIn("Сообщений в событии удаления: 25", archive)
        for message_id in message_ids:
            self.assertIn(f"history text {message_id}", archive)

    def test_delete_up_to_ten_sends_notifications_without_file(self):
        """При удалении 10 сообщений бот не формирует TXT-файл."""
        chat_id = 900031
        recipient = 950004
        self.mock_post.return_value.json.return_value = {
            "ok": True,
            "result": {
                "user_chat_id": recipient,
                "user": {"id": chat_id},
            },
        }
        message_ids = list(range(600200, 600210))
        payload = make_deleted_business_messages_payload(
            message_ids=message_ids,
            chat_id=chat_id,
        )

        response = self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        send_document_calls = [
            c for c in self.mock_post.call_args_list
            if get_post_call_args(c)[0] and "sendDocument" in str(get_post_call_args(c)[0])
        ]
        send_message_calls = [
            c for c in self.mock_post.call_args_list
            if get_post_call_args(c)[0] and "sendMessage" in str(get_post_call_args(c)[0])
        ]
        self.assertEqual(send_document_calls, [])
        self.assertEqual(len(send_message_calls), 10)
        for call in send_message_calls:
            _, body = get_post_call_args(call)
            self.assertTrue(body["text"].endswith(WHO_UPDATE_BOT_LINK))


class WebhookIdempotencyTests(NoTelegramApiTestCase):
    """Повторный webhook с тем же update_id не должен вызывать side effects."""

    def test_duplicate_update_id_skips_processing(self):
        payload = make_business_message_payload(
            message_id=100050,
            username_from="dup_user",
            text="first",
        )
        payload["update_id"] = 9001

        response1 = self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response1.status_code, 200)
        self.assertEqual(Message.objects.filter(chat_id=300001, message_id=100050).count(), 1)
        calls_after_first_delivery = self.mock_post.call_count

        payload["business_message"]["text"] = "second attempt"
        response2 = self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response2.status_code, 200)

        msg = Message.objects.get(chat_id=300001, message_id=100050)
        self.assertEqual(msg.text, "first")
        self.assertEqual(self.mock_post.call_count, calls_after_first_delivery)


class WebhookCompositeMessageKeyTests(NoTelegramApiTestCase):
    """message_id уникален внутри chat_id, но может повторяться в разных чатах."""

    def test_same_message_id_in_different_chats_creates_two_rows(self):
        shared_message_id = 777001

        payload_a = make_business_message_payload(
            message_id=shared_message_id,
            username_from="user_a",
            text="chat A",
        )
        payload_a["update_id"] = 9101
        payload_a["business_message"]["chat"]["id"] = 300101

        payload_b = make_business_message_payload(
            message_id=shared_message_id,
            username_from="user_b",
            text="chat B",
        )
        payload_b["update_id"] = 9102
        payload_b["business_message"]["chat"]["id"] = 300202

        self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload_a),
            content_type="application/json",
        )
        self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload_b),
            content_type="application/json",
        )

        self.assertEqual(Message.objects.filter(message_id=shared_message_id).count(), 2)
        msg_a = Message.objects.get(chat_id=300101, message_id=shared_message_id)
        msg_b = Message.objects.get(chat_id=300202, message_id=shared_message_id)
        self.assertEqual(msg_a.text, "chat A")
        self.assertEqual(msg_b.text, "chat B")


class WebhookViewOnceRescueTests(NoTelegramApiTestCase):
    """Reply на has_protected_content фото/видео → копия владельцу."""

    def test_owner_reply_to_protected_photo_sends_copy(self):
        owner_id = 800100
        owner_chat_id = 800100
        partner_id = 700100
        chat_id = 300100
        source_message_id = 500100
        self.mock_post.return_value.json.return_value = {
            "ok": True,
            "result": {
                "user_chat_id": owner_chat_id,
                "user": {"id": owner_id, "username": "owner_user"},
            },
        }

        payload = make_business_message_payload(
            message_id=500101,
            username_from="owner_user",
            text=".",
            business_connection_id="conn_view_once_1",
        )
        payload["update_id"] = 92001
        payload["business_message"]["from"]["id"] = owner_id
        payload["business_message"]["chat"]["id"] = chat_id
        payload["business_message"]["reply_to_message"] = {
            "message_id": source_message_id,
            "from": {
                "id": partner_id,
                "is_bot": False,
                "first_name": "Partner",
                "username": "partner_user",
            },
            "chat": {
                "id": chat_id,
                "type": "private",
            },
            "date": 1000000,
            "has_protected_content": True,
            "photo": [
                {"file_id": "photo_small_vo", "width": 90, "height": 90},
                {"file_id": "photo_large_vo", "width": 1280, "height": 1280},
            ],
            "caption": "secret shot",
        }

        with patch(
            "webhook_tg.views.download_telegram_file_bytes",
            return_value=(b"jpeg-bytes", "photos/file_42.jpg"),
        ) as mock_dl, patch(
            "webhook_tg.views.send_photo_bytes",
            return_value=True,
        ) as mock_send:
            response = self.client.post(
                "/webhook_tg/",
                data=json.dumps(payload),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 200)
            mock_dl.assert_called_once_with("photo_large_vo", timeout=90)
            mock_send.assert_called_once()
            args, kwargs = mock_send.call_args
            self.assertEqual(args[0], owner_chat_id)
            self.assertEqual(args[1], b"jpeg-bytes")
            self.assertIn("одноразовое", kwargs.get("caption", ""))
            self.assertIn("secret shot", kwargs.get("caption", ""))

        saved = Message.objects.get(chat_id=chat_id, message_id=source_message_id)
        self.assertEqual(saved.file_id, "photo_large_vo")
        self.assertEqual(saved.file_type, FileType.PHOTO)

    def test_non_owner_reply_does_not_rescue(self):
        owner_id = 800101
        partner_id = 700101
        chat_id = 300101
        self.mock_post.return_value.json.return_value = {
            "ok": True,
            "result": {
                "user_chat_id": owner_id,
                "user": {"id": owner_id},
            },
        }

        payload = make_business_message_payload(
            message_id=500201,
            username_from="partner_user",
            text="ok",
            business_connection_id="conn_view_once_2",
        )
        payload["update_id"] = 92002
        payload["business_message"]["from"]["id"] = partner_id
        payload["business_message"]["chat"]["id"] = chat_id
        payload["business_message"]["reply_to_message"] = {
            "message_id": 500200,
            "from": {"id": partner_id, "first_name": "Partner"},
            "chat": {"id": chat_id, "type": "private"},
            "date": 1000000,
            "has_protected_content": True,
            "photo": [{"file_id": "should_not_send", "width": 100, "height": 100}],
        }

        self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        send_photo_calls = [
            c for c in self.mock_post.call_args_list
            if get_post_call_args(c)[0] and "sendPhoto" in str(get_post_call_args(c)[0])
        ]
        self.assertEqual(len(send_photo_calls), 0)

    def test_unprotected_media_reply_ignored(self):
        owner_id = 800102
        chat_id = 300102
        self.mock_post.return_value.json.return_value = {
            "ok": True,
            "result": {
                "user_chat_id": owner_id,
                "user": {"id": owner_id},
            },
        }

        payload = make_business_message_payload(
            message_id=500301,
            text=".",
            business_connection_id="conn_view_once_3",
        )
        payload["update_id"] = 92003
        payload["business_message"]["from"]["id"] = owner_id
        payload["business_message"]["chat"]["id"] = chat_id
        payload["business_message"]["reply_to_message"] = {
            "message_id": 500300,
            "from": {"id": 700102, "first_name": "Partner"},
            "chat": {"id": chat_id, "type": "private"},
            "date": 1000000,
            "has_protected_content": False,
            "photo": [{"file_id": "normal_photo", "width": 100, "height": 100}],
        }

        self.client.post(
            "/webhook_tg/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        send_photo_calls = [
            c for c in self.mock_post.call_args_list
            if get_post_call_args(c)[0] and "sendPhoto" in str(get_post_call_args(c)[0])
        ]
        self.assertEqual(len(send_photo_calls), 0)


class MuteCommandTests(NoTelegramApiTestCase):
    def test_mute_wizard_and_unmute(self):
        chat_id = 1394340082
        user_id = 1394340082
        self.mock_post.return_value.json.return_value = {"ok": True, "result": True}

        payload = make_start_payload(chat_id=chat_id, user_id=user_id, update_id=93001)
        payload["message"]["text"] = "/mute @SpamUser"
        self.client.post("/webhook_tg/", data=json.dumps(payload), content_type="application/json")

        from webhook_tg.models import MuteSetup, MutedPeer
        setup = MuteSetup.objects.get(owner_user_id=user_id, muted_username="spamuser")
        self.assertFalse(MutedPeer.objects.filter(muted_username="spamuser").exists())

        # шаг 1: срок
        cb1 = {
            "update_id": 93002,
            "callback_query": {
                "id": "cq1",
                "from": {"id": user_id, "username": "owner"},
                "message": {
                    "message_id": 10,
                    "chat": {"id": chat_id, "type": "private"},
                    "text": "mute",
                },
                "data": f"ms:{setup.id}:d:600",
            },
        }
        self.client.post("/webhook_tg/", data=json.dumps(cb1), content_type="application/json")
        setup.refresh_from_db()
        self.assertEqual(setup.duration_seconds, 600)

        # шаг 2: уведомления
        cb2 = {
            "update_id": 93003,
            "callback_query": {
                "id": "cq2",
                "from": {"id": user_id, "username": "owner"},
                "message": {
                    "message_id": 10,
                    "chat": {"id": chat_id, "type": "private"},
                    "text": "mute",
                },
                "data": f"ms:{setup.id}:n:1",
            },
        }
        self.client.post("/webhook_tg/", data=json.dumps(cb2), content_type="application/json")
        mute = MutedPeer.objects.get(owner_user_id=user_id, muted_username="spamuser")
        self.assertTrue(mute.notify_in_bot)
        self.assertIsNotNone(mute.expires_at)
        self.assertFalse(MuteSetup.objects.filter(id=setup.id).exists())

        payload["update_id"] = 93004
        payload["message"]["text"] = "/unmute @SpamUser"
        self.client.post("/webhook_tg/", data=json.dumps(payload), content_type="application/json")
        self.assertFalse(
            MutedPeer.objects.filter(owner_user_id=user_id, muted_username="spamuser").exists()
        )

    def test_muted_first_message_warns_then_deletes(self):
        owner_id = 1394340082
        from webhook_tg.models import MutedPeer
        mute = MutedPeer.objects.create(
            owner_user_id=owner_id,
            owner_chat_id=owner_id,
            muted_username="spammer",
            notify_in_bot=True,
            warning_sent=False,
        )
        self.mock_post.return_value.json.return_value = {
            "ok": True,
            "result": {
                "user_chat_id": owner_id,
                "user": {"id": owner_id, "username": "maksonchik200"},
            },
        }

        payload = make_business_message_payload(
            message_id=777001,
            username_from="spammer",
            text="spam hello",
            business_connection_id="conn_mute_1",
        )
        payload["update_id"] = 93010
        payload["business_message"]["from"]["id"] = 555001
        payload["business_message"]["chat"]["id"] = 555001

        self.client.post("/webhook_tg/", data=json.dumps(payload), content_type="application/json")

        # business warning + deleteBusinessMessages (+ возможно notify sendMessage)
        methods = []
        for c in self.mock_post.call_args_list:
            url, body = get_post_call_args(c)
            url_s = str(url or "")
            if "deleteBusinessMessages" in url_s:
                methods.append(("delete", body))
            elif "sendMessage" in url_s and body.get("business_connection_id") == "conn_mute_1":
                methods.append(("warn", body))

        self.assertTrue(any(m[0] == "warn" for m in methods))
        self.assertTrue(any(m[0] == "delete" for m in methods))
        mute.refresh_from_db()
        self.assertTrue(mute.warning_sent)
        self.assertFalse(Message.objects.filter(message_id=777001).exists())

    def test_expired_mute_is_cleared_on_message(self):
        owner_id = 1394340082
        from django.utils import timezone
        from datetime import timedelta
        from webhook_tg.models import MutedPeer
        MutedPeer.objects.create(
            owner_user_id=owner_id,
            owner_chat_id=owner_id,
            muted_username="spammer",
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        self.mock_post.return_value.json.return_value = {
            "ok": True,
            "result": {
                "user_chat_id": owner_id,
                "user": {"id": owner_id, "username": "maksonchik200"},
            },
        }
        payload = make_business_message_payload(
            message_id=777002,
            username_from="spammer",
            text="after expiry",
            business_connection_id="conn_mute_2",
        )
        payload["update_id"] = 93011
        payload["business_message"]["from"]["id"] = 555002
        payload["business_message"]["chat"]["id"] = 555002
        self.client.post("/webhook_tg/", data=json.dumps(payload), content_type="application/json")

        delete_calls = [
            c for c in self.mock_post.call_args_list
            if get_post_call_args(c)[0] and "deleteBusinessMessages" in str(get_post_call_args(c)[0])
        ]
        self.assertEqual(len(delete_calls), 0)
        self.assertFalse(MutedPeer.objects.filter(muted_username="spammer").exists())
        self.assertTrue(Message.objects.filter(message_id=777002).exists())
