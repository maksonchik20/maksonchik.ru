from __future__ import annotations

from datetime import date
import uuid

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from webhook_tg.config import OWNER_CHAT_ID
from webhook_tg.models import TelegramOutbox
from webhook_tg.outbox import enqueue_outbox
from webhook_tg.user_metrics import collect_daily_user_metrics, daily_user_metrics_text


class Command(BaseCommand):
    help = "Отправляет владельцу ежедневные пользовательские метрики WhoUpdate"

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            help="Московская дата отчёта в формате YYYY-MM-DD; по умолчанию сегодня",
        )
        parser.add_argument(
            "--preview",
            action="store_true",
            help="Отправить пробный отчёт, не занимая ежедневный idempotency_key",
        )

    def handle(self, *args, **options):
        raw_day = str(options.get("date") or "").strip()
        try:
            day = date.fromisoformat(raw_day) if raw_day else timezone.localdate()
        except ValueError as exc:
            raise CommandError("--date должна иметь формат YYYY-MM-DD") from exc

        metrics = collect_daily_user_metrics(day=day)
        idempotency_key = (
            f"preview-user-metrics:{uuid.uuid4()}"
            if options["preview"]
            else f"daily-user-metrics:{day.isoformat()}"
        )
        enqueue_outbox(
            chat_id=OWNER_CHAT_ID,
            method=TelegramOutbox.Method.SEND_MESSAGE,
            idempotency_key=idempotency_key,
            payload={
                "text": daily_user_metrics_text(metrics),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        self.stdout.write(f"report queued: {idempotency_key}")
