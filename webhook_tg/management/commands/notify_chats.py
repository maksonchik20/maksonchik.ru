"""
Скрипт рассылки сообщения в указанные чаты.
Использование: python manage.py notify_chats
"""

from django.core.management.base import BaseCommand

from webhook_tg.telegram import tg_send_message

# username : chat_id (для логов; пустой username — только chat_id)
CHATS = [
]

MESSAGE = (
    "В бота добавлен функционал информирования об удалённых сообщениях:\n\n"
    "• фото\n"
    "• аудио и голосовые\n"
    "• видео и видеокружки\n"
    "• документы\n\n"
    "При удалении такого сообщения собеседником вы получите копию файла с подписью."
)


class Command(BaseCommand):
    help = "Отправляет уведомление о новом функционале в указанные чаты"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Не отправлять, только вывести список чатов",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        if dry_run:
            self.stdout.write("Режим dry-run, отправка не выполняется.\n")

        for username, chat_id in CHATS:
            label = username or str(chat_id)
            if dry_run:
                self.stdout.write(f"  {label} : {chat_id}")
                continue
            try:
                tg_send_message(chat_id=str(chat_id), text=MESSAGE)
                self.stdout.write(self.style.SUCCESS(f"OK: {label} ({chat_id})"))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"Ошибка {label} ({chat_id}): {e}"))

        if not dry_run:
            self.stdout.write(self.style.SUCCESS(f"\nОтправлено в {len(CHATS)} чатов."))
