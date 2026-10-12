import logging
import signal
import threading

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from webhook_tg.outbox import process_outbox
from webhook_tg.metrics import record_heartbeat


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Отправляет сообщения из TelegramOutbox однократно или постоянно с --loop."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=50,
            help="Максимум сообщений за один запуск (default: 50)",
        )
        parser.add_argument("--loop", action="store_true")
        parser.add_argument("--poll-interval", type=float, default=2.0)

    def handle(self, *args, **optionToValue):
        limit = optionToValue["limit"]
        if not optionToValue["loop"]:
            metricToCount = process_outbox(limit=limit)
            record_heartbeat("outbox", min_interval_seconds=0)
            self._write_stats(metricToCount)
            return

        stop_requested = threading.Event()
        poll_interval = max(0.1, optionToValue["poll_interval"])
        signalToHandler = {}

        def request_stop(signum, frame):
            stop_requested.set()

        try:
            for signum in (signal.SIGTERM, signal.SIGINT):
                signalToHandler[signum] = signal.signal(signum, request_stop)
            while not stop_requested.is_set():
                try:
                    close_old_connections()
                    metricToCount = process_outbox(limit=limit)
                    record_heartbeat("outbox", min_interval_seconds=30)
                    if metricToCount["processed"]:
                        self._write_stats(metricToCount)
                except Exception:
                    logger.exception("Outbox worker batch failed; will retry")
                finally:
                    try:
                        close_old_connections()
                    except Exception:
                        logger.exception("Could not close stale outbox database connections")
                stop_requested.wait(poll_interval)
        finally:
            for signum, handler in signalToHandler.items():
                signal.signal(signum, handler)

    def _write_stats(self, metricToCount):
        self.stdout.write(
            self.style.SUCCESS(
                f"Outbox: processed={metricToCount['processed']} sent={metricToCount['sent']} "
                f"failed={metricToCount['failed']} pending={metricToCount['pending']}"
            )
        )
        self.stdout.flush()
