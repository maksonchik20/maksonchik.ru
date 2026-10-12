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
        self.assertIn("spin", {item["command"] for item in COMMANDS})

    def test_referral_is_the_only_partner_command_and_describes_both_rewards(self):
        for commands in (COMMANDS, OWNER_COMMANDS):
            with self.subTest(owner_commands=commands is OWNER_COMMANDS):
                names = [item["command"] for item in commands]
                self.assertNotIn("money", names)
                self.assertEqual(names.count("referral"), 1)
                referral = next(item for item in commands if item["command"] == "referral")
                self.assertIn("7 дней", referral["description"])
                self.assertIn("30%", referral["description"])
                self.assertIn("покуп", referral["description"])
