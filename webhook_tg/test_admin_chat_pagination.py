from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import Message


class MessageAdminChatPaginationTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser(
            username="chat-admin",
            email="chat-admin@example.com",
            password="test-password",
        )
        self.client.force_login(self.admin)
        self.chat_id = 777001
        self.connection_id = "connection-pagination-test"
        self.messages = [
            Message.objects.create(
                chat_id=self.chat_id,
                business_connection_id=self.connection_id,
                message_id=index + 1,
                username_from="history_user",
                text=f"message-{index:03d}",
            )
            for index in range(205)
        ]
        Message.objects.create(
            chat_id=888002,
            business_connection_id=self.connection_id,
            message_id=1,
            username_from="other_user",
            text="message-from-another-chat",
        )

    def chat_params(self, **extra):
        return {
            "chat_id": self.chat_id,
            "business_connection_id": self.connection_id,
            **extra,
        }

    def test_initial_chat_page_contains_only_latest_hundred_messages(self):
        response = self.client.get(
            reverse("admin:webhook_tg_message_changelist"),
            self.chat_params(),
        )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn("message-204", content)
        self.assertIn("message-105", content)
        self.assertNotIn("message-104", content)
        self.assertNotIn("message-from-another-chat", content)
        self.assertIn('data-has-more="1"', content)
        self.assertIn("прокрутите вверх для загрузки истории", content)
        self.assertLess(content.index("message-105"), content.index("message-204"))

    def test_ajax_pages_walk_back_without_duplicates(self):
        page_url = reverse("admin:webhook_tg_message_chat_page")
        second = self.client.get(
            page_url,
            self.chat_params(before_id=self.messages[105].id),
        )

        self.assertEqual(second.status_code, 200)
        second_data = second.json()
        self.assertEqual(second_data["loaded"], 100)
        self.assertTrue(second_data["has_more"])
        self.assertEqual(second_data["next_before"], self.messages[5].id)
        self.assertIn("message-005", second_data["html"])
        self.assertIn("message-104", second_data["html"])
        self.assertNotIn("message-105", second_data["html"])
        self.assertLess(
            second_data["html"].index("message-005"),
            second_data["html"].index("message-104"),
        )

        third = self.client.get(
            page_url,
            self.chat_params(before_id=second_data["next_before"]),
        )
        third_data = third.json()
        self.assertEqual(third_data["loaded"], 5)
        self.assertFalse(third_data["has_more"])
        self.assertIsNone(third_data["next_before"])
        self.assertIn("message-000", third_data["html"])
        self.assertIn("message-004", third_data["html"])

    def test_ajax_requires_valid_chat_and_cursor(self):
        page_url = reverse("admin:webhook_tg_message_chat_page")

        self.assertEqual(self.client.get(page_url).status_code, 400)
        self.assertEqual(
            self.client.get(page_url, self.chat_params(before_id="wrong")).status_code,
            400,
        )
