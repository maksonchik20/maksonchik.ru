import json
import os
import shutil
import time
import datetime

import psutil
from django.core.management.base import BaseCommand

from webhook_tg.telegram import tg_send_message
from env import OWNER_CHAT_ID


def human_gb(bytes_: int) -> str:
    return f"{bytes_ / 1024 / 1024 / 1024:.1f} GB"


class Command(BaseCommand):
    """
    Режимы:
      --report : всегда отправляет текущие значения
      --alert  : отправляет только при превышении порогов + антиспам
    """

    def add_arguments(self, parser):
        parser.add_argument("--report", action="store_true", help="Always send current metrics")
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

        # Метрики
        du = shutil.disk_usage(disk_path)
        disk_used_pct = int((du.used / du.total) * 100)


        cpu_pct = int(psutil.cpu_percent(interval=1))

        host = "maksonchik.ru"

        text = (
            f"{host}\n"
            f"Disk {disk_path}: used {disk_used_pct}% (free {human_gb(du.free)} / total {human_gb(du.total)})\n"
            f"CPU: {cpu_pct}%"
        )

        if mode_report:
            tg_send_message(OWNER_CHAT_ID, "📊 Daily report\n" + text)
            self.stdout.write("report sent")
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
                f"(used {human_gb(du.used)}, free {human_gb(du.free)}, total {human_gb(du.total)}) | "
                f"CPU: {cpu_pct}%"
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

        alert_text = "🚨 LIMIT EXCEEDED\n" + "\n".join(exceeded) + "\n\n" + text
        tg_send_message(OWNER_CHAT_ID, alert_text)
        self.stdout.write("alert sent")
