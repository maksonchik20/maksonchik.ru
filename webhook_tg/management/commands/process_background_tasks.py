from __future__ import annotations

import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from webhook_tg.background_tasks import (
    claim_next_task,
    process_claimed_task,
    recover_stale_tasks,
    worker_name,
)
from webhook_tg.metrics import record_heartbeat


class Command(BaseCommand):
    help = "Постоянный worker надёжной очереди фоновых задач."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--poll-interval", type=float, default=1.0)

    def handle(self, *args, **options):
        claimed_by = worker_name()
        poll_interval = max(0.1, options["poll_interval"])
        last_recovery_at = 0.0

        while True:
            now_monotonic = time.monotonic()
            if now_monotonic - last_recovery_at >= 60:
                recovered = recover_stale_tasks()
                if recovered:
                    self.stdout.write(f"Recovered stale tasks: {recovered}")
                last_recovery_at = now_monotonic

            record_heartbeat("background_tasks")
            close_old_connections()
            task = claim_next_task(claimed_by=claimed_by)
            if task is None:
                if options["once"]:
                    return
                time.sleep(poll_interval)
                continue

            process_claimed_task(task)
            close_old_connections()
            if options["once"]:
                return
