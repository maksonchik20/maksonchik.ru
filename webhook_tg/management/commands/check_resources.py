import json
import os
import time
import datetime
import uuid

from django.core.management.base import BaseCommand
from django.utils import timezone

from webhook_tg.models import TelegramOutbox
from webhook_tg.outbox import enqueue_outbox
from webhook_tg.resource_metrics import collect_resource_snapshot, human_gb, resource_report_text
from webhook_tg.telegram import tg_send_message
from env import OWNER_CHAT_ID


class Command(BaseCommand):
    """
    Режимы:
      --report : всегда отправляет текущие значения
      --alert  : отправляет только при превышении порогов + антиспам
    """

    def add_arguments(self, parser):
        parser.add_argument("--report", action="store_true", help="Always send current metrics")
        parser.add_argument(
            "--daily",
            action="store_true",
            help="Queue one idempotent daily report for the current Moscow date",
        )
        parser.add_argument("--alert", action="store_true", help="Send only if limits exceeded")
        parser.add_argument("--disk", default="/", help="Disk mountpoint to check, default '/'")

        parser.add_argument("--disk-limit", type=int, default=90, help="Disk used %% threshold")
        parser.add_argument("--cpu-limit", type=int, default=90, help="CPU %% threshold")

        parser.add_argument(
            "--cooldown",
            type=int,
            default=3600,
            help="Min seconds between repeated alerts for same host (default 3600)",
        )
        parser.add_argument(
            "--state-file",
            default="/var/tmp/check_resources_state.json",
            help="Where to store last alert timestamps (default /var/tmp/...)",
        )

    def handle(self, *args, **opts):
        mode_report = opts["report"]
        mode_alert = opts["alert"]
        if not mode_report and not mode_alert:
            mode_alert = True

        disk_path = opts["disk"]
        disk_limit = opts["disk_limit"]
        cpu_limit = opts["cpu_limit"]
        cooldown = opts["cooldown"]
        state_file = opts["state_file"]

        snapshot = collect_resource_snapshot(disk_path=disk_path)
        disk_used_pct = snapshot.disk_used_pct
        cpu_pct = snapshot.cpu_pct
        host = "maksonchik.ru"

        if mode_report:
            report_key = (
                f"daily-resource-report:{timezone.localdate().isoformat()}"
                if opts["daily"]
                else f"manual-resource-report:{uuid.uuid4()}"
            )
            enqueue_outbox(
                chat_id=OWNER_CHAT_ID,
                method=TelegramOutbox.Method.SEND_MESSAGE,
                idempotency_key=report_key,
                payload={
                    "text": resource_report_text(snapshot, title="📊 Ежедневный отчёт"),
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            self.stdout.write(f"report queued: {report_key}")
            return

        # mode_alert
        exceeded = []
        if disk_used_pct >= disk_limit:
            exceeded.append(f"Disk {disk_path} {disk_used_pct}% >= {disk_limit}%")
        if cpu_pct >= cpu_limit:
            exceeded.append(f"CPU {cpu_pct}% >= {cpu_limit}%")

        if not exceeded:
            now_str = datetime.datetime.now().strftime("%d.%m.%Y %H:%M:%S")

            self.stdout.write(
                f"[{now_str}] OK | "
                f"Disk {disk_path}: {disk_used_pct}% used "
                f"(free {human_gb(snapshot.disk_free)}, total {human_gb(snapshot.disk_total)}) | "
                f"RAM: {snapshot.memory_used_pct}% | CPU: {cpu_pct}%"
            )
            return

        # антиспам (не чаще cooldown секунд)
        now = int(time.time())
        key = f"{host}:{disk_path}"

        state = {}
        try:
            with open(state_file, "r") as f:
                state = json.load(f)
        except FileNotFoundError:
            state = {}
        except Exception:
            state = {}

        last = int(state.get(key, 0))
        if now - last < cooldown:
            self.stdout.write(f"skipped: cooldown ({now-last}s < {cooldown}s)")
            return

        state[key] = now
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        with open(state_file, "w") as f:
            json.dump(state, f)

        alert_text = (
            "🚨 LIMIT EXCEEDED\n"
            + "\n".join(exceeded)
            + "\n\n"
            + resource_report_text(snapshot, title="📊 Ресурсы сервера")
        )
        tg_send_message(OWNER_CHAT_ID, alert_text)
        self.stdout.write("alert sent")
