"""Long polling для WhoUpdate (вместо webhook)."""

from __future__ import annotations

import logging
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from webhook_tg.incoming import enqueue_incoming_update
from webhook_tg.metrics import POLL_ERRORS, observe_metric, observe_sqlite_lock, record_heartbeat
from webhook_tg.telegram import delete_telegram_webhook, get_telegram_updates

logger = logging.getLogger(__name__)

POLL_TIMEOUT = 15
ERROR_SLEEP = 5


class Command(BaseCommand):
    help = (
        "Long polling WhoUpdate: deleteWebhook + getUpdates "
        "(message, business_*, edits/deletes)."
    )

    def handle(self, *args, **options):
        delete_telegram_webhook(drop_pending_updates=False)
        self.stdout.write(self.style.SUCCESS("Webhook отключён, запуск polling-ingest…"))

        offset = 0
        while True:
            try:
                close_old_connections()
                data = get_telegram_updates(offset=offset, poll_timeout=POLL_TIMEOUT)
                record_heartbeat("telegram_poll")
                updates = data.get("result") or []
                for update in updates:
                    update_id = update.get("update_id")
                    try:
                        close_old_connections()
                        enqueue_incoming_update(update, source="long_poll")
                        if update_id is not None:
                            offset = update_id + 1
                    except Exception as exc:
                        observe_sqlite_lock(exc, "telegram_poll_enqueue")
                        # Не двигаем offset: Telegram вернёт update повторно.
                        logger.exception("Failed to enqueue update %s", update_id)
                        break
                    finally:
                        close_old_connections()
            except KeyboardInterrupt:
                self.stdout.write("Остановка…")
                return
            except Exception as exc:
                observe_metric(POLL_ERRORS, 1)
                observe_sqlite_lock(exc, "telegram_poll")
                logger.exception("Telegram polling failed: %s", exc)
                time.sleep(ERROR_SLEEP)
