import json
import tempfile
from pathlib import Path
from datetime import datetime, timezone as datetime_timezone
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from .history import (
    HISTORY_CONTACTS_BUTTON,
    HISTORY_CONTACTS_CALLBACK,
    HISTORY_HELP,
    _build_history_contacts_export,
    _parse_history_target,
    handle_history_callback,
    handle_history_command,
)
from .models import BackgroundTask, FileType, Message, TelegramOutbox, UserTg
from .history_exports import export_history_task


class HistoryTargetTests(SimpleTestCase):
    def test_username_and_numeric_id(self):
        for command, expected in (
            ("/history @Target_User", "target_user"),
            ("/history target_user", "target_user"),
            ("/history 7210174607", 7210174607),
            (" /history@who_update_bot 000123 \n", 123),
            (f"/history {2 ** 52 - 1}", 2 ** 52 - 1),
        ):
            with self.subTest(command=command):
                self.assertEqual(_parse_history_target(command), expected)

    def test_invalid_target(self):
        for argument in (
            "", "@", "0", "-123", "+123", "@123", "@@target_user",
            "123 other", "123.5", "１２３", "<b>name</b>", "a" * 33,
            str(2 ** 52), "9" * 5000,
        ):
            with self.subTest(argument=argument[:40]):
                self.assertIsNone(_parse_history_target(f"/history {argument}"))


class HistoryCommandTests(TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(override_settings(WHO_UPDATE_HISTORY_EXPORT_DIR=directory))
        self.bot_user = UserTg.objects.create(
            user_id=100, chat_id=100, username="owner", business_connection_id="owner_conn",
        )
        self.peer_id = 7210174607
        self.send_message = self.enterContext(patch("webhook_tg.history.send_message_reliably"))
        self.send_document = self.enterContext(patch("webhook_tg.history.send_document_bytes_reliably"))

    def create_message(self, message_id, **fieldToValue):
        messageFieldToValue = {
            "business_connection_id": "owner_conn", "chat_id": self.peer_id,
            "message_id": message_id,
        }
        messageFieldToValue.update(fieldToValue)
        return Message.objects.create(**messageFieldToValue)

    def command(self, text):
        self.assertTrue(handle_history_command(100, self.bot_user, text, update_id=987))
        if _parse_history_target(text) is not None and self.bot_user.business_connection_id:
            export_history_task(BackgroundTask.objects.get())

    def archive(self):
        item = TelegramOutbox.objects.get(method=TelegramOutbox.Method.SEND_DOCUMENT_FILE)
        self.assertEqual(item.chat_id, 100)
        self.assertEqual(item.idempotency_key, "command:987:history:document")
        return Path(item.payload["document_path"]).read_text(encoding="utf-8-sig")

    def test_id_exports_both_sides_without_username_and_keeps_owner_isolation(self):
        self.create_message(1, username_from=None, first_name="Peer", text="Без username")
        self.create_message(2, username_from="owner", text="Мой ответ")
        self.create_message(3, username_from="old_username", text="До смены username")
        self.create_message(4, username_from="", caption="Удалённое фото", file_id="test-photo",
                            file_type=FileType.PHOTO, is_deleted=True)
        self.create_message(5, business_connection_id="other_conn", text="Чужой владелец")
        self.create_message(6, chat_id=999, text="Другой чат")
        self.create_message(7, business_connection_id=None, text="Нет владельца")

        self.command(f"/history {self.peer_id}")

        archive = self.archive()
        self.assertIn(f"История переписки с Telegram ID {self.peer_id}", archive)
        self.assertIn("Сообщений: 4", archive)
        for content in ("Без username", "Мой ответ", "До смены username", "Удалённое фото", "[вложение: PHOTO]"):
            self.assertIn(content, archive)
        for content in ("Чужой владелец", "Другой чат", "Нет владельца"):
            self.assertNotIn(content, archive)
        self.assertLess(archive.index("Без username"), archive.index("Мой ответ"))
        self.assertEqual(TelegramOutbox.objects.get().payload["filename"], f"history_id_{self.peer_id}.txt")
        self.send_message.assert_not_called()

    def test_username_preserves_sender_only_case_insensitive_lookup(self):
        self.create_message(1, username_from="Target_User", text="От собеседника")
        self.create_message(2, username_from="target_user", text="Ещё сообщение")
        self.create_message(3, username_from="owner", text="Мой ответ")
        self.create_message(4, business_connection_id="other_conn", username_from="target_user", text="Чужая переписка")

        self.command("/history @TARGET_USER")

        archive = self.archive()
        self.assertIn("История сообщений от @target_user", archive)
        self.assertIn("Сообщений: 2", archive)
        self.assertNotIn("Мой ответ", archive)
        self.assertNotIn("Чужая переписка", archive)
        self.assertEqual(TelegramOutbox.objects.get().payload["filename"], "history_target_user.txt")

    def test_no_history_does_not_fall_back_to_another_owners_messages(self):
        self.create_message(1, business_connection_id="other_conn", text="Чужая переписка")

        self.command(f"/history {self.peer_id}")

        self.send_document.assert_not_called()
        item = TelegramOutbox.objects.get(idempotency_key="command:987:history:empty")
        self.assertEqual(item.chat_id, 100)
        self.assertEqual(item.payload["text"], f"Сохранённой переписки с Telegram ID <b>{self.peer_id}</b> пока не найдено.")

    def test_no_business_connection_cannot_access_unscoped_history(self):
        self.bot_user.business_connection_id = ""
        self.create_message(1, business_connection_id="", text="Без подключения")

        self.command(f"/history {self.peer_id}")

        self.send_document.assert_not_called()
        self.assertEqual(self.send_message.call_args.kwargs["idempotency_key"], "command:987:history:not-connected")

    def test_missing_or_invalid_target_explains_both_formats_without_querying(self):
        for command in ("/history", "/history -123", f"/history {2 ** 64}"):
            with self.subTest(command=command), self.assertNumQueries(0):
                self.send_message.reset_mock()
                self.command(command)
                self.send_message.assert_called_once_with(
                    100, HISTORY_HELP, idempotency_key="command:987:history:help",
                    reply_markup={"inline_keyboard": [[{
                        "text": HISTORY_CONTACTS_BUTTON,
                        "callback_data": HISTORY_CONTACTS_CALLBACK,
                    }]]},
                )
        self.send_document.assert_not_called()


class HistoryContactsTests(TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(override_settings(WHO_UPDATE_HISTORY_EXPORT_DIR=directory))
        self.bot_user = UserTg.objects.create(
            user_id=100, chat_id=100, username="owner", business_connection_id="owner_conn",
        )
        self.answer = self.enterContext(patch("webhook_tg.history.answer_callback_query"))
        self.send_message = self.enterContext(patch("webhook_tg.history.send_message_reliably"))
        self.send_document = self.enterContext(patch("webhook_tg.history.send_document_bytes_reliably"))

    def create_message(self, message_id, peer_id, date, *, username="", text="", **fieldToValue):
        messageFieldToValue = {
            "message_id": message_id, "chat_id": peer_id, "business_connection_id": "owner_conn",
            "username_from": username, "first_name": f"Peer {peer_id}", "text": text,
            "payload": json.dumps({
                "date": date,
                "chat": {"id": peer_id, "first_name": f"Peer {peer_id}", "username": username},
                "from": {"id": peer_id, "first_name": f"Peer {peer_id}", "username": username},
            }),
        }
        messageFieldToValue.update(fieldToValue)
        return Message.objects.create(**messageFieldToValue)

    def callback(self, *, user_id=100, chat_id=100, chat_type="private"):
        return handle_history_callback({
            "id": "history-button", "data": HISTORY_CONTACTS_CALLBACK,
            "from": {"id": user_id}, "message": {"chat": {"id": chat_id, "type": chat_type}},
        }, update_id=1000)

    def test_file_contains_all_peers_counts_last_message_and_orders_newest_first(self):
        # Insert the newest chat first, and an older message last: neither
        # database insertion order nor name may determine the file's order.
        self.create_message(10, 300, 1791140400, username="newer_peer", text="Последнее новое")
        self.create_message(11, 200, 1791136800, text="Удалённое последнее", is_deleted=True)
        self.create_message(9, 200, 1791133200, text="Старое сообщение")
        self.create_message(20, 500, 1791144000, username="secret", text="Чужой секрет",
                            business_connection_id="other_conn")
        self.create_message(12, 200, 1791144000, username="secret_same_peer", text="Тот же собеседник другого владельца",
                            business_connection_id="other_conn")

        with self.assertNumQueries(2):
            document, count = _build_history_contacts_export(self.bot_user)

        text = document.decode("utf-8-sig")
        self.assertEqual(count, 2)
        self.assertLess(text.index("Telegram ID: 300"), text.index("Telegram ID: 200"))
        self.assertIn("Порядок: от поздней даты последнего сообщения к ранней.", text)
        self.assertIn("Username: @newer_peer", text)
        self.assertIn("Username: отсутствует или не сохранён", text)
        self.assertIn("Сообщений: 2", text)
        self.assertIn("Последнее сообщение: Удалённое последнее", text)
        self.assertIn("Дата последнего сообщения: 04.10.2026 21:00:00", text)
        self.assertIn("Получить переписку: /history 200", text)
        self.assertNotIn("Старое сообщение", text)
        self.assertNotIn("Чужой секрет", text)
        self.assertNotIn("Тот же собеседник другого владельца", text)
        self.assertNotIn("secret_same_peer", text)

    def test_media_is_marked_and_peer_metadata_is_not_taken_from_owner(self):
        self.create_message(1, 200, 1791136800, username="owner", text="Не выводить вместо медиа",
            file_id="private-file-id", file_type=FileType.PHOTO,
            payload=str({
                "date": 1791136800,
                "chat": {"id": 200, "first_name": "Собеседник", "username": "peer"},
                "from": {"id": 100, "first_name": "Владелец", "username": "owner"},
            }))

        document, count = _build_history_contacts_export(self.bot_user)

        text = document.decode("utf-8-sig")
        self.assertEqual(count, 1)
        self.assertIn("1. Собеседник", text)
        self.assertIn("Username: @peer", text)
        self.assertIn("Последнее сообщение: [Медиа:", text)
        self.assertNotIn("Не выводить вместо медиа", text)
        self.assertNotIn("private-file-id", text)
        self.assertNotIn("Username: @owner", text)
        self.assertNotIn("Владелец", text)

    def test_legacy_rows_use_marked_saved_date_and_missing_dates_go_last(self):
        saved = self.create_message(1, 200, None, username="legacy_peer", text="Legacy", payload="broken")
        Message.objects.filter(pk=saved.pk).update(created_at=datetime(2026, 10, 1, tzinfo=datetime_timezone.utc))
        unknown = self.create_message(2, 300, None, username="owner", payload="[]")
        Message.objects.filter(pk=unknown.pk).update(created_at=None)

        document, count = _build_history_contacts_export(self.bot_user)

        text = document.decode("utf-8-sig")
        self.assertEqual(count, 2)
        self.assertLess(text.index("Telegram ID: 200"), text.index("Telegram ID: 300"))
        self.assertIn("Username: @legacy_peer", text)
        self.assertIn("01.10.2026 03:00:00 (дата сохранения;", text)
        self.assertIn("Дата последнего сообщения: неизвестна", text)
        self.assertNotIn("Username: @owner", text)

    def test_callback_uses_only_clicking_users_history_and_reliable_delivery(self):
        self.create_message(1, 200, 1791136800, username="peer", text="Моя переписка")
        self.create_message(2, 300, 1791136800, username="secret", text="Чужая переписка",
                            business_connection_id="other_conn")

        self.assertTrue(self.callback())
        export_history_task(BackgroundTask.objects.get())

        self.answer.assert_called_once_with("history-button", "Готовлю список сохранённых переписок")
        item = TelegramOutbox.objects.get()
        self.assertEqual(item.chat_id, 100)
        self.assertEqual(item.payload["filename"], "history_contacts.txt")
        self.assertEqual(item.idempotency_key, "command:1000:history:contacts:document")
        text = Path(item.payload["document_path"]).read_text(encoding="utf-8-sig")
        self.assertIn("Моя переписка", text)
        self.assertNotIn("Чужая переписка", text)
        self.send_message.assert_not_called()

    def test_callback_rejects_non_private_or_mismatched_recipient_without_db_access(self):
        for chat_id, chat_type in ((-100, "group"), (101, "private"), (100, "group")):
            with self.subTest(chat_id=chat_id, chat_type=chat_type), self.assertNumQueries(0):
                self.assertTrue(self.callback(chat_id=chat_id, chat_type=chat_type))
        self.send_document.assert_not_called()
        self.send_message.assert_not_called()

    def test_unknown_user_or_missing_connection_cannot_export_history(self):
        self.assertTrue(self.callback(user_id=999, chat_id=999))
        self.bot_user.business_connection_id = ""
        self.bot_user.save(update_fields=["business_connection_id"])
        self.create_message(1, 200, 1791136800, business_connection_id="")

        self.assertTrue(self.callback())
        with self.assertNumQueries(0):
            self.assertEqual(_build_history_contacts_export(self.bot_user), (b"", 0))

        self.send_document.assert_not_called()
        self.send_message.assert_not_called()

    def test_no_chats_sends_explanation_instead_of_empty_file(self):
        self.assertTrue(self.callback())
        export_history_task(BackgroundTask.objects.get())
        item = TelegramOutbox.objects.get(idempotency_key="command:1000:history:contacts:empty")
        self.assertEqual(item.chat_id, 100)
        self.assertEqual(item.payload["text"], "Сохранённых переписок пока нет.")
        self.send_document.assert_not_called()

    def test_unrelated_button_is_not_handled(self):
        with self.assertNumQueries(0):
            self.assertFalse(handle_history_callback({"data": "other"}, update_id=1001))
        self.answer.assert_not_called()
