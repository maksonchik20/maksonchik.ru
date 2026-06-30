from datetime import timedelta

from django.core.management.base import BaseCommand

from webhook_tg.events_chart import (
    build_outgoing_events_chart,
    format_outgoing_events_summary,
    parse_events_period,
)
from webhook_tg.telegram import send_photo_bytes, tg_send_message


class Command(BaseCommand):
    help = "Build and send /events chart to a Telegram chat."

    def add_arguments(self, parser):
        parser.add_argument("chat_id", type=int)
        parser.add_argument("period", nargs="?", default="1h")

    def handle(self, *args, **opts):
        chat_id = opts["chat_id"]
        period = parse_events_period(opts["period"])
        if period is None:
            tg_send_message(chat_id, "Неверный период.")
            return

        try:
            chart_bytes, caption = build_outgoing_events_chart(period)
        except Exception as exc:
            self.stderr.write(f"chart build failed: {exc}")
            tg_send_message(chat_id, "Не удалось построить график. Попробуйте позже.")
            return

        if send_photo_bytes(chat_id, chart_bytes, caption=caption):
            self.stdout.write(f"chart sent to {chat_id}")
            return

        summary = format_outgoing_events_summary(period)
        tg_send_message(
            chat_id,
            "Не удалось отправить график (сеть). Краткая сводка:\n\n" + summary,
        )
        self.stderr.write(f"chart send failed for {chat_id}")
