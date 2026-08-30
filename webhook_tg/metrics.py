from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import timedelta

from django.db import IntegrityError, OperationalError
from django.db.models import F, Value
from django.db.models.functions import Greatest
from django.utils import timezone

from .models import OperationalMetricBucket

logger = logging.getLogger(__name__)

WEBHOOK_DURATION = "who_update_webhook_request_duration_ms"
TELEGRAM_SEND_DURATION = "who_update_telegram_send_duration_ms"
TELEGRAM_MESSAGES_SENT = "who_update_telegram_messages_sent"
TELEGRAM_SEND_ATTEMPTS = "who_update_telegram_send_attempts"
TELEGRAM_MESSAGES_FAILED = "who_update_telegram_messages_failed"
TELEGRAM_SUCCESS_RATE = "who_update_telegram_success_percent"
WEBHOOK_REQUESTS = "who_update_webhook_requests"
INCOMING_UPDATES_RECEIVED = "who_update_incoming_updates_received"
INCOMING_UPDATES_PROCESSED = "who_update_incoming_updates_processed"
INCOMING_PROCESSING_DURATION = "who_update_incoming_processing_duration_ms"
INCOMING_END_TO_END_DURATION = "who_update_incoming_end_to_end_duration_ms"
BUSINESS_CONNECTION_EVENTS = "who_update_business_connection_events"
USER_STARTS = "who_update_user_starts"
PAYMENT_EVENTS = "who_update_payment_events"
REFERRAL_REWARDS = "who_update_referral_rewards"
SQLITE_LOCK_ERRORS = "who_update_sqlite_lock_errors"
POLL_ERRORS = "who_update_poll_errors"
OUTBOX_EVENTS = "who_update_outbox_events"
ONBOARDING_EVENTS = "who_update_onboarding_events"
METRIKA_OFFLINE_EVENTS = "who_update_metrika_offline_events"
BACKGROUND_JOB_HEARTBEAT = "who_update_background_job_heartbeat"

OUTBOX_SIZE = "who_update_outbox_size"
OUTBOX_DUE = "who_update_outbox_due"
OUTBOX_OLDEST_AGE = "who_update_outbox_oldest_age_seconds"
OUTBOX_MAX_ATTEMPTS = "who_update_outbox_max_attempts"
INCOMING_BACKLOG = "who_update_incoming_backlog"
INCOMING_OLDEST_AGE = "who_update_incoming_oldest_age_seconds"
INCOMING_MAX_ATTEMPTS = "who_update_incoming_max_attempts"
ACTIVE_BUSINESS_CONNECTIONS = "who_update_active_business_connections"
STARTED_USERS = "who_update_started_users"
CONNECTION_CONVERSION = "who_update_connection_conversion_percent"
EXPIRED_SUBSCRIPTIONS = "who_update_expired_subscriptions"
ACTIVE_LIMITED_SUBSCRIPTIONS = "who_update_active_limited_subscriptions"
SYSTEM_MEMORY_USED = "who_update_system_memory_used_percent"
SYSTEM_MEMORY_AVAILABLE = "who_update_system_memory_available_bytes"
ONBOARDING_FUNNEL_TOTAL = "who_update_onboarding_funnel_total"
ONBOARDING_MILESTONES_TOTAL = "who_update_onboarding_milestones_total"
ONBOARDING_CONNECTIONS_TOTAL = "who_update_onboarding_connections_total"
METRIKA_OFFLINE_QUEUE_TOTAL = "who_update_metrika_offline_queue_total"

COUNT_METRICS = {
    TELEGRAM_MESSAGES_SENT,
    TELEGRAM_SEND_ATTEMPTS,
    TELEGRAM_MESSAGES_FAILED,
    WEBHOOK_REQUESTS,
    INCOMING_UPDATES_RECEIVED,
    INCOMING_UPDATES_PROCESSED,
    BUSINESS_CONNECTION_EVENTS,
    USER_STARTS,
    PAYMENT_EVENTS,
    REFERRAL_REWARDS,
    SQLITE_LOCK_ERRORS,
    POLL_ERRORS,
    OUTBOX_EVENTS,
    ONBOARDING_EVENTS,
    METRIKA_OFFLINE_EVENTS,
}

GAUGE_METRICS = {
    BACKGROUND_JOB_HEARTBEAT,
    OUTBOX_SIZE,
    OUTBOX_DUE,
    OUTBOX_OLDEST_AGE,
    OUTBOX_MAX_ATTEMPTS,
    INCOMING_BACKLOG,
    INCOMING_OLDEST_AGE,
    INCOMING_MAX_ATTEMPTS,
    ACTIVE_BUSINESS_CONNECTIONS,
    STARTED_USERS,
    CONNECTION_CONVERSION,
    EXPIRED_SUBSCRIPTIONS,
    ACTIVE_LIMITED_SUBSCRIPTIONS,
    SYSTEM_MEMORY_USED,
    SYSTEM_MEMORY_AVAILABLE,
    ONBOARDING_FUNNEL_TOTAL,
    ONBOARDING_MILESTONES_TOTAL,
    ONBOARDING_CONNECTIONS_TOTAL,
    METRIKA_OFFLINE_QUEUE_TOTAL,
}

_last_heartbeat: dict[str, float] = {}


def _current_minute():
    return timezone.now().replace(second=0, microsecond=0)


def observe_metric(metric_name: str, value: float, labels: dict | None = None) -> None:
    """Записать наблюдение; ошибки мониторинга не должны ломать работу бота."""
    normalized_labels = {
        str(key): str(label_value)
        for key, label_value in sorted((labels or {}).items())
    }
    labels_json = json.dumps(normalized_labels, sort_keys=True, separators=(",", ":"))
    labels_hash = hashlib.sha256(labels_json.encode("utf-8")).hexdigest()
    minute = _current_minute()
    value = float(value)

    try:
        for _ in range(2):
            updated = OperationalMetricBucket.objects.filter(
                metric_name=metric_name,
                minute=minute,
                labels_hash=labels_hash,
                exported_at__isnull=True,
            ).update(
                count=F("count") + 1,
                total=F("total") + value,
                maximum=Greatest(F("maximum"), Value(value)),
            )
            if updated:
                return
            try:
                OperationalMetricBucket.objects.create(
                    metric_name=metric_name,
                    minute=minute,
                    labels_hash=labels_hash,
                    labels=normalized_labels,
                    count=1,
                    total=value,
                    maximum=value,
                )
                return
            except IntegrityError:
                continue
    except (OperationalError, IntegrityError):
        logger.warning("Could not persist operational metric %s", metric_name, exc_info=True)


def set_gauge(metric_name: str, value: float, labels: dict | None = None) -> None:
    """Установить последнее значение gauge внутри текущей минуты."""
    normalized_labels = {
        str(key): str(label_value)
        for key, label_value in sorted((labels or {}).items())
    }
    labels_json = json.dumps(normalized_labels, sort_keys=True, separators=(",", ":"))
    labels_hash = hashlib.sha256(labels_json.encode("utf-8")).hexdigest()
    minute = _current_minute()
    value = float(value)
    try:
        OperationalMetricBucket.objects.update_or_create(
            metric_name=metric_name,
            minute=minute,
            labels_hash=labels_hash,
            defaults={
                "labels": normalized_labels,
                "count": 1,
                "total": value,
                "maximum": value,
                "exported_at": None,
            },
        )
    except (OperationalError, IntegrityError):
        logger.warning("Could not persist operational gauge %s", metric_name, exc_info=True)


def record_heartbeat(job: str, *, min_interval_seconds: float = 30) -> None:
    now = time.monotonic()
    if now - _last_heartbeat.get(job, 0) < min_interval_seconds:
        return
    set_gauge(BACKGROUND_JOB_HEARTBEAT, 1, {"job": job})
    _last_heartbeat[job] = now


def observe_sqlite_lock(exc: Exception, component: str) -> None:
    if "database is locked" in str(exc).lower():
        observe_metric(SQLITE_LOCK_ERRORS, 1, {"component": component})


def closed_bucket_cutoff():
    return _current_minute() - timedelta(seconds=1)
