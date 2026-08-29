from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from webhook_tg.config import DEMO_VIDEO_FILE_IDS
from webhook_tg.telegram import download_telegram_file_bytes


DEMO_FILES = (
    "hidden-media.mp4",
    "edited-message.mp4",
    "deleted-message.mp4",
)


class Command(BaseCommand):
    help = "Скачивает демонстрационные видео WhoUpdate из Telegram на сайт"

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Перезаписать существующие видео")

    def handle(self, *args, **options):
        if len(DEMO_VIDEO_FILE_IDS) != len(DEMO_FILES):
            raise CommandError("Ожидалось три file_id демонстрационных видео")

        target_dir = Path(settings.WHO_UPDATE_DEMO_MEDIA_ROOT)
        target_dir.mkdir(parents=True, exist_ok=True)

        for file_id, filename in zip(DEMO_VIDEO_FILE_IDS, DEMO_FILES):
            target = target_dir / filename
            if target.exists() and not options["force"]:
                self.stdout.write(f"Уже существует: {target}")
                continue

            content, _ = download_telegram_file_bytes(file_id, timeout=180)
            temporary = target.with_suffix(target.suffix + ".tmp")
            temporary.write_bytes(content)
            temporary.replace(target)
            self.stdout.write(self.style.SUCCESS(f"Сохранено: {target} ({len(content)} байт)"))
