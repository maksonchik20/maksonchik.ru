from __future__ import annotations

import time

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from webhook_tg.incoming import claim_next_update, process_claimed_update, recover_stale_updates
from webhook_tg.incoming_notifications import IDLE_POLL_SECONDS, IncomingQueueWakeup
from webhook_tg.models import TelegramIncomingUpdate
from webhook_tg.metrics import record_heartbeat


RECOVERY_INTERVAL_SECONDS = 30


class Command(BaseCommand):
    help = "Постоянный worker входящей Telegram webhook-очереди."

    def add_arguments(self, parser):
        parser.add_argument(
            "--queue",
            required=True,
            choices=[choice for choice, _ in TelegramIncomingUpdate.Queue.choices],
        )
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **optionToValue):
        queue = optionToValue["queue"]
        if queue not in TelegramIncomingUpdate.Queue.values:
            raise CommandError(f"Unknown queue: {queue}")

        next_recovery_at = 0.0
        wakeup = IncomingQueueWakeup(queue)
        try:
            while True:
                if not optionToValue["once"]:
                    wakeup.prepare()
                close_old_connections()
                # Run under load as well as when idle. A lease that was still fresh
                # at startup must not remain stuck until the next worker restart.
                now = time.monotonic()
                if now >= next_recovery_at:
                    recovered = recover_stale_updates(queue)
                    if recovered:
                        self.stdout.write(f"Recovered stale updates: {recovered}")
                        self.stdout.flush()
                    next_recovery_at = now + RECOVERY_INTERVAL_SECONDS

                record_heartbeat(f"incoming_{queue}")
                item = claim_next_update(queue)
                if item is None:
                    if optionToValue["once"]:
                        return
                    if not wakeup.wait(IDLE_POLL_SECONDS):
                        time.sleep(IDLE_POLL_SECONDS)
                    continue

                process_claimed_update(item)
                close_old_connections()
                if optionToValue["once"]:
                    return
        finally:
            wakeup.close()
