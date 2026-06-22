from django.core.management.base import BaseCommand

from webhook_tg.outbox import process_outbox


class Command(BaseCommand):
    help = "Отправляет сообщения из очереди TelegramOutbox (запускать по cron каждую минуту)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=50,
            help="Максимум сообщений за один запуск (default: 50)",
        )

    def handle(self, *args, **options):
        stats = process_outbox(limit=options["limit"])
        self.stdout.write(
            self.style.SUCCESS(
                f"Outbox: processed={stats['processed']} sent={stats['sent']} "
                f"failed={stats['failed']} pending={stats['pending']}"
            )
        )
