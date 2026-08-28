from django.core.management.base import BaseCommand

from webhook_tg.telegram import get_telegram_webhook_info, set_telegram_webhook


class Command(BaseCommand):
    help = "Включает Telegram webhook для WhoUpdate и показывает итоговую конфигурацию."

    def add_arguments(self, parser):
        parser.add_argument(
            "--url",
            default="https://maksonchik.ru/webhook_tg/",
        )
        parser.add_argument("--drop-pending-updates", action="store_true")

    def handle(self, *args, **options):
        set_telegram_webhook(
            options["url"],
            drop_pending_updates=options["drop_pending_updates"],
        )
        info = (get_telegram_webhook_info().get("result") or {}).copy()
        # Telegram сейчас не возвращает secret_token, но не печатаем его и в будущем.
        info.pop("secret_token", None)
        self.stdout.write(self.style.SUCCESS(f"Webhook configured: {info}"))
