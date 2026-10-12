"""Best-effort wakeups; the incoming table remains the durable queue."""

from __future__ import annotations

import logging
import time
from contextlib import closing

from django.db import connections


logger = logging.getLogger(__name__)

IDLE_POLL_SECONDS = 1.0
RECONNECT_SECONDS = 30.0
queueToChannel = {
    "priority": "who_update_incoming_priority",
    "business": "who_update_incoming_business",
}


def notify_incoming_queue(queue: str, *, using: str = "default") -> None:
    channel = queueToChannel[queue]
    try:
        connection = connections[using]
        if connection.vendor != "postgresql":
            return
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_notify(%s, %s)", [channel, ""])
    except Exception as exc:
        # A failed wakeup must not undo an already committed incoming update.
        # Do not log connection parameters or notification payloads.
        logger.warning("Incoming queue notify failed queue=%s (%s)", queue, type(exc).__name__)


class IncomingQueueWakeup:
    def __init__(self, queue: str, *, using: str = "default"):
        self.queue = queue
        self.channel = queueToChannel[queue]
        self.using = using
        self._listener = None
        self._next_retry_at = 0.0
        self._driver_supported = True

    def prepare(self) -> None:
        """Register LISTEN before inspecting the queue to avoid a lost wakeup."""
        source = connections[self.using]
        if source.vendor != "postgresql" or not self._driver_supported:
            return
        if self._listener is not None:
            raw_connection = self._listener.connection
            if raw_connection is not None and not raw_connection.closed:
                return
            self.close()
        if time.monotonic() < self._next_retry_at:
            return

        try:
            # An independent, autocommit connection: Django request cleanup
            # must not discard LISTEN, nor should a long transaction hold it.
            self._listener = source.copy(alias=f"{self.using}_incoming_{self.queue}")
            optionToValue = self._listener.settings_dict["OPTIONS"]
            optionToValue.pop("pool", None)
            optionToValue["connect_timeout"] = 2
            optionToValue["application_name"] = f"who-update-listen-{self.queue}"
            self._listener.ensure_connection()
            self._listener.set_autocommit(True)
            if not callable(getattr(self._listener.connection, "notifies", None)):
                self._driver_supported = False
                self.close()
                return
            with self._listener.cursor() as cursor:
                cursor.execute(f"LISTEN {self.channel}")
        except Exception as exc:
            self._failed(exc)

    def wait(self, timeout: float = IDLE_POLL_SECONDS) -> bool:
        """Return False when the caller should use its ordinary polling sleep."""
        if self._listener is None:
            return False
        try:
            with closing(self._listener.connection.notifies(timeout=timeout, stop_after=1)) as notifications:
                # Psycopg yields the entire already-buffered batch even with
                # stop_after=1. Consume it so stale wakeups do not cause a
                # SELECT per notification after the durable queue is drained.
                for _ in notifications:
                    pass
            return True
        except Exception as exc:
            self._failed(exc)
            return False

    def _failed(self, exc: Exception) -> None:
        self.close()
        self._next_retry_at = time.monotonic() + RECONNECT_SECONDS
        logger.warning("Incoming queue listener unavailable queue=%s (%s)", self.queue, type(exc).__name__)

    def close(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.close()
            except Exception:
                logger.warning("Could not close incoming queue listener queue=%s", self.queue)
