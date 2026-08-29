from __future__ import annotations

import time

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from webhook_tg.incoming import claim_next_update, process_claimed_update, recover_stale_updates
from webhook_tg.models import TelegramIncomingUpdate
from webhook_tg.metrics import record_heartbeat


class Command(BaseCommand):
    help = "Постоянный worker входящей Telegram webhook-очереди."

    def add_arguments(self, parser):
        parser.add_argument(
            "--queue",
            required=True,
            choices=[choice for choice, _ in TelegramIncomingUpdate.Queue.choices],
        )
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        queue = options["queue"]
        if queue not in TelegramIncomingUpdate.Queue.values:
            raise CommandError(f"Unknown queue: {queue}")

        recovered = recover_stale_updates(queue)
        if recovered:
            self.stdout.write(f"Recovered stale updates: {recovered}")

        idle_sleep = 0.05 if queue == TelegramIncomingUpdate.Queue.PRIORITY else 0.2
        while True:
            record_heartbeat(f"incoming_{queue}")
            close_old_connections()
            item = claim_next_update(queue)
            if item is None:
                if options["once"]:
                    return
                time.sleep(idle_sleep)
                continue

            process_claimed_update(item)
            close_old_connections()
            if options["once"]:
                return
