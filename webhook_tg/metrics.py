from __future__ import annotations

import hashlib
import json
import logging
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


def closed_bucket_cutoff():
    return _current_minute() - timedelta(seconds=1)
