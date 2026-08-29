from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .bot_outgoing_log import log_bot_incoming, log_bot_outgoing
from .models import BotChatEvent, UserTg


class BotChatJournalTests(TestCase):
    def setUp(self):
        self.bot_user = UserTg.objects.create(
            user_id=700001,
            chat_id=600001,
            username="testuser",
            first_name="Test",
        )

    def test_incoming_message_is_logged_idempotently(self):
        update = {
            "update_id": 101,
            "message": {
                "message_id": 12,
                "from": {"id": self.bot_user.user_id, "username": "testuser"},
                "chat": {"id": self.bot_user.chat_id, "type": "private"},
                "text": "/status",
            },
        }

        log_bot_incoming(update)
        log_bot_incoming(update)

        event = BotChatEvent.objects.get()
        self.assertEqual(event.direction, BotChatEvent.Direction.USER)
        self.assertEqual(event.event_type, BotChatEvent.EventType.MESSAGE)
        self.assertEqual(event.text, "/status")
        self.assertEqual(event.telegram_message_id, 12)

    def test_callback_records_human_button_label(self):
        log_bot_incoming(
            {
                "update_id": 102,
                "callback_query": {
                    "id": "callback-1",
                    "data": "who_update_demo",
                    "message": {
                        "chat": {"id": self.bot_user.chat_id, "type": "private"},
                        "reply_markup": {
                            "inline_keyboard": [[{
                                "text": "🎬 Демонстрация работы бота",
                                "callback_data": "who_update_demo",
                            }]],
                        },
                    },
                },
            }
        )

        event = BotChatEvent.objects.get()
        self.assertEqual(event.event_type, BotChatEvent.EventType.CALLBACK)
        self.assertIn("Демонстрация работы бота", event.text)

    def test_outgoing_message_keeps_text_and_payload(self):
        log_bot_outgoing(
            chat_id=self.bot_user.chat_id,
            method="sendMessage",
            payload={"text": "<b>Доступ активен</b>", "reply_markup": {"inline_keyboard": []}},
            result={"message_id": 77},
        )

        event = BotChatEvent.objects.get()
        self.assertEqual(event.direction, BotChatEvent.Direction.BOT)
        self.assertEqual(event.text, "Доступ активен")
        self.assertEqual(event.telegram_message_id, 77)
        self.assertIn("reply_markup", event.payload)

    def test_dialog_page_is_available_to_superuser(self):
        BotChatEvent.objects.create(
            chat_id=self.bot_user.chat_id,
            direction=BotChatEvent.Direction.USER,
            text="Привет",
        )
        admin_user = get_user_model().objects.create_superuser(
            username="journal-admin",
            email="admin@example.com",
            password="secret",
        )
        self.client.force_login(admin_user)

        response = self.client.get(
            reverse("admin:webhook_tg_usertg_dialog", args=[self.bot_user.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Привет")
        self.assertContains(response, "@testuser")
