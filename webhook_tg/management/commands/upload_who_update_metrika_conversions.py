from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from webhook_tg.metrika_offline import (
    reconcile_failed_conversion_statuses,
    reconcile_submitted_conversions,
    recover_failed_onboarding_conversions,
    recover_failed_purchase_conversions,
    sync_conversion_queue,
    sync_purchase_conversions,
    upload_pending_conversions,
)


class Command(BaseCommand):
    help = "Автоматически загружает /start, подключения и оплаты WhoUpdate в Яндекс Метрику"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=500)
        parser.add_argument("--status-limit", type=int, default=50)
        parser.add_argument(
            "--failed-status-limit", type=int, default=20,
            help="Recheck at most this many recent failed uploads without resending them",
        )
        parser.add_argument(
            "--recover-onboarding-linkage", action="store_true",
            help="Recheck at most 20 failed onboarding rows and add a missing ClientID only after confirmed LINKAGE_FAILURE",
        )

    def handle(self, *args, **options):
        # A timer and a manual run must not upload the same pending rows at once.
        lock_id = 0x57484F4D45545249
        locked = False
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(%s)", [lock_id])
                locked = cursor.fetchone()[0]
            if not locked:
                self.stdout.write("Another Metrika upload is already running")
                return
        try:
            self._upload(**options)
        finally:
            if locked:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s)", [lock_id])

    def _upload(self, **options):
        token = str(getattr(settings, "YANDEX_METRIKA_OAUTH_TOKEN", "") or "").strip()
        if not token:
            raise CommandError("YANDEX_METRIKA_OAUTH_TOKEN is not configured")
        counter_id = int(getattr(settings, "YANDEX_METRIKA_COUNTER_ID", 111680333))

        queued = sync_conversion_queue(counter_id=counter_id)
        queued += sync_purchase_conversions(counter_id=counter_id)
        reconciled = reconcile_submitted_conversions(
            token=token,
            limit=options["status_limit"],
        )
        reconciled += reconcile_failed_conversion_statuses(
            token=token,
            limit=options["failed_status_limit"],
        )
        recovered = recover_failed_purchase_conversions(token=token)
        if options["recover_onboarding_linkage"]:
            recovered += recover_failed_onboarding_conversions(token=token)
        submitted = upload_pending_conversions(
            token=token,
            limit=options["limit"],
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Queued {queued}; recovered {recovered}; submitted {submitted}; reconciled {reconciled} conversions"
            )
        )
