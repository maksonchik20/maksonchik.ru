from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase

from .config import OWNER_CHAT_ID
from .management.commands.setup_who_update_commands import COMMANDS, OWNER_COMMANDS


class BotCommandsTests(SimpleTestCase):
    @patch("webhook_tg.management.commands.setup_who_update_commands.set_bot_commands")
    def test_private_commands_are_configured_only_for_owner_chat(self, set_commands):
        call_command("setup_who_update_commands")

        self.assertEqual(set_commands.call_count, 2)
        set_commands.assert_any_call(COMMANDS)
        set_commands.assert_any_call(
            OWNER_COMMANDS,
            scope={"type": "chat", "chat_id": int(OWNER_CHAT_ID)},
        )
        self.assertNotIn("stat", {item["command"] for item in COMMANDS})
        self.assertIn("stat", {item["command"] for item in OWNER_COMMANDS})
