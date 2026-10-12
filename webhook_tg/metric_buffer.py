"""Bounded, best-effort buffering of technical telemetry, never business data.

Only the explicitly listed counters/timings are buffered. On abrupt process exit
the last five seconds of telemetry can be lost; incoming updates, replies,
payments and business events retain their existing durable database path.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass

from django.conf import settings
from django.db import DatabaseError, close_old_connections, connection, connections, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

BUFFERED_METRICS = frozenset({
    "who_update_webhook_request_duration_ms",
    "who_update_webhook_requests",
    "who_update_telegram_send_duration_ms",
    "who_update_telegram_messages_sent",
    "who_update_telegram_send_attempts",
    "who_update_telegram_messages_failed",
    "who_update_incoming_updates_received",
    "who_update_incoming_updates_processed",
    "who_update_incoming_processing_duration_ms",
    "who_update_incoming_end_to_end_duration_ms",
})
MAX_BUFFER_KEYS = 512


@dataclass
class Observation:
    metric_name: str
    minute: object
    labels_hash: str
    labelToValue: dict
    count: int
    total: float
    maximum: float


keyToObservation = {}
_buffer_lock = threading.Lock()
_flush_lock = threading.Lock()
_thread = None
_stop = threading.Event()


def _reset_after_fork():
    global keyToObservation, _buffer_lock, _flush_lock, _thread, _stop
    keyToObservation = {}
    _buffer_lock = threading.Lock()
    _flush_lock = threading.Lock()
    _thread = None
    _stop = threading.Event()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)


def _interval():
    default = 5.0 if connection.vendor == "postgresql" else 0.0
    return float(getattr(settings, "OPERATIONAL_METRICS_BATCH_SECONDS", default))


def _merge(targetKeyToObservation, key, observation):
    current = targetKeyToObservation.get(key)
    if current is None:
        targetKeyToObservation[key] = observation
    else:
        current.count += observation.count
        current.total += observation.total
        current.maximum = max(current.maximum, observation.maximum)


def _persist(observations):
    if not observations:
        return
    from .models import OperationalMetricBucket

    observations = sorted(observations, key=lambda item: (item.metric_name, item.minute, item.labels_hash))
    table = connection.ops.quote_name(OperationalMetricBucket._meta.db_table)
    greatest = "GREATEST" if connection.vendor == "postgresql" else "MAX"
    values_sql = ", ".join(["(%s,%s,%s,%s,%s,%s,%s,NULL)"] * len(observations))
    parameters = []
    for item in observations:
        parameters.extend([
            item.metric_name, connection.ops.adapt_datetimefield_value(item.minute), item.labels_hash,
            json.dumps(item.labelToValue, sort_keys=True, separators=(",", ":")),
            item.count, item.total, item.maximum,
        ])
    # Atomic increments: concurrent workers never overwrite one another's data.
    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {table} (metric_name, minute, labels_hash, labels, count, total, maximum, exported_at) "
            f"VALUES {values_sql} ON CONFLICT (metric_name, minute, labels_hash) DO UPDATE SET "
            f"count={table}.count+excluded.count, total={table}.total+excluded.total, "
            f"maximum={greatest}({table}.maximum, excluded.maximum) "
            f"WHERE {table}.exported_at IS NULL",
            parameters,
        )


def flush_metric_buffer(*, close_connections=False):
    """Drain without holding the producer lock during database I/O."""
    global keyToObservation
    with _flush_lock:
        with _buffer_lock:
            pendingKeyToObservation = keyToObservation
            keyToObservation = {}
        try:
            if close_connections:
                close_old_connections()
            _persist(list(pendingKeyToObservation.values()))
        except DatabaseError:
            with _buffer_lock:
                for key, observation in pendingKeyToObservation.items():
                    if key in keyToObservation or len(keyToObservation) < MAX_BUFFER_KEYS:
                        _merge(keyToObservation, key, observation)
            logger.warning("Could not flush technical telemetry; bounded retry buffer retained", exc_info=True)
        finally:
            if close_connections:
                connections.close_all()


def _run(interval):
    while not _stop.wait(interval):
        try:
            flush_metric_buffer(close_connections=True)
        except Exception:
            # Monitoring must not affect queue workers, even for unexpected errors.
            logger.exception("Technical telemetry flusher failed")


def _add(observation, interval):
    global _thread
    key = (observation.metric_name, observation.minute, observation.labels_hash)
    with _buffer_lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_run, args=(interval,), name="technical-metrics", daemon=True)
            _thread.start()
        if key in keyToObservation or len(keyToObservation) < MAX_BUFFER_KEYS:
            _merge(keyToObservation, key, observation)
            return
    # Keep memory bounded under unexpected label cardinality. This is telemetry
    # only; do not block the application's delivery path on buffer draining.
    logger.warning("Technical telemetry buffer is full; observation omitted")


def buffer_observation(metric_name, value, labels):
    if metric_name not in BUFFERED_METRICS:
        return False
    interval = _interval()
    if interval <= 0:
        return False
    labelToValue = {str(key): str(item) for key, item in sorted((labels or {}).items())}
    labels_json = json.dumps(labelToValue, sort_keys=True, separators=(",", ":"))
    observation = Observation(
        metric_name, timezone.now().replace(second=0, microsecond=0),
        hashlib.sha256(labels_json.encode("utf-8")).hexdigest(), labelToValue,
        1, float(value), float(value),
    )
    # Do not count events from a rolled-back business transaction.
    transaction.on_commit(lambda: _add_safely(observation, interval), robust=True)
    return True


def _add_safely(observation, interval):
    try:
        _add(observation, interval)
    except Exception:
        logger.warning("Could not buffer technical telemetry", exc_info=True)


def _shutdown():
    _stop.set()
    if keyToObservation:
        flush_metric_buffer(close_connections=True)


atexit.register(_shutdown)
