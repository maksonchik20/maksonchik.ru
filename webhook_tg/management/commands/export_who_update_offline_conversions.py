from __future__ import annotations

import csv
import sys

from django.core.management.base import BaseCommand, CommandError

from webhook_tg.models import WhoUpdateOnboardingFunnel


class Command(BaseCommand):
    help = "Выгружает /start или подключения WhoUpdate для офлайн-конверсий Метрики"

    def add_arguments(self, parser):
        parser.add_argument("--event", choices=("start", "connected"), default="connected")
        parser.add_argument("--identifier", choices=("yclid", "client_id"), default="yclid")
        parser.add_argument("--output", default="-")

    def handle(self, *args, **options):
        event = options["event"]
        identifier = options["identifier"]
        timestamp_field = "telegram_started_at" if event == "start" else "connected_at"
        target = "who_update_start" if event == "start" else "who_update_connected"
        identifier_field = "yclid" if identifier == "yclid" else "metrika_client_id"
        identifier_header = "Yclid" if identifier == "yclid" else "ClientId"

        queryset = (
            WhoUpdateOnboardingFunnel.objects.filter(
                **{
                    f"{timestamp_field}__isnull": False,
                    f"{identifier_field}__gt": "",
                }
            )
            .order_by(timestamp_field, "id")
            .values_list(identifier_field, timestamp_field)
        )

        output_path = options["output"]
        stream = sys.stdout if output_path == "-" else open(output_path, "w", newline="", encoding="utf-8")
        try:
            writer = csv.writer(stream)
            writer.writerow((identifier_header, "Target", "DateTime"))
            count = 0
            for external_id, occurred_at in queryset.iterator():
                writer.writerow((external_id, target, int(occurred_at.timestamp())))
                count += 1
        except OSError as exc:
            raise CommandError(str(exc)) from exc
        finally:
            if stream is not sys.stdout:
                stream.close()

        if output_path != "-":
            self.stdout.write(self.style.SUCCESS(f"Exported {count} conversions to {output_path}"))
