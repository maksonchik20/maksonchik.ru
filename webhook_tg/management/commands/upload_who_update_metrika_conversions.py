from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from webhook_tg.metrika_offline import (
    reconcile_submitted_conversions,
    sync_conversion_queue,
    upload_pending_conversions,
)


class Command(BaseCommand):
    help = "Автоматически загружает /start и подключения WhoUpdate в Яндекс Метрику"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=500)
        parser.add_argument("--status-limit", type=int, default=50)

    def handle(self, *args, **options):
        token = str(getattr(settings, "YANDEX_METRIKA_OAUTH_TOKEN", "") or "").strip()
        if not token:
            raise CommandError("YANDEX_METRIKA_OAUTH_TOKEN is not configured")
        counter_id = int(getattr(settings, "YANDEX_METRIKA_COUNTER_ID", 111680333))

        queued = sync_conversion_queue(counter_id=counter_id)
        reconciled = reconcile_submitted_conversions(
            token=token,
            limit=options["status_limit"],
        )
        submitted = upload_pending_conversions(
            token=token,
            limit=options["limit"],
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Queued {queued}; submitted {submitted}; reconciled {reconciled} conversions"
            )
        )
