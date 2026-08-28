from django.core.management.base import BaseCommand

from webhook_tg.telegram import set_bot_commands


COMMANDS = [
    {"command": "start", "description": "Открыть WhoUpdate"},
    {"command": "status", "description": "Срок доступа и тарифы"},
    {"command": "referral", "description": "Пригласить друга и получить 7 дней"},
    {"command": "subscribe", "description": "Продлить доступ"},
    {"command": "mute", "description": "Удалять сообщения пользователя"},
    {"command": "mutelist", "description": "Список mute"},
    {"command": "unmute", "description": "Снять mute"},
]


class Command(BaseCommand):
    help = "Обновляет меню команд @who_update_bot"

    def handle(self, *args, **options):
        set_bot_commands(COMMANDS)
        self.stdout.write(self.style.SUCCESS("Команды WhoUpdate обновлены"))
