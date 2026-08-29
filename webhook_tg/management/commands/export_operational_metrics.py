from __future__ import annotations

import os

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from webhook_tg.metrics import (
    COUNT_METRICS,
    GAUGE_METRICS,
    closed_bucket_cutoff,
    record_heartbeat,
)
from webhook_tg.metric_snapshots import collect_database_gauges
from webhook_tg.models import OperationalMetricBucket

METADATA_TOKEN_URL = (
    "http://169.254.169.254/computeMetadata/v1/instance/"
    "service-accounts/default/token"
)
MONITORING_WRITE_URL = "https://monitoring.api.cloud.yandex.net/monitoring/v2/data/write"


def _iam_token() -> str:
    configured_token = os.environ.get("MONITORING_IAM_TOKEN", "").strip()
    if configured_token:
        return configured_token
    response = requests.get(
        METADATA_TOKEN_URL,
        headers={"Metadata-Flavor": "Google"},
        timeout=(1, 3),
    )
    response.raise_for_status()
    return response.json()["access_token"]


class Command(BaseCommand):
    help = "Отправляет закрытые минутные метрики WhoUpdate в Yandex Monitoring"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=500)

    def handle(self, *args, **options):
        collect_database_gauges()
        record_heartbeat("metrics_exporter", min_interval_seconds=0)
        buckets = list(
            OperationalMetricBucket.objects.filter(
                exported_at__isnull=True,
                minute__lte=closed_bucket_cutoff(),
            ).order_by("minute", "id")[: options["limit"]]
        )
        if not buckets:
            return

        metrics = []
        for bucket in buckets:
            timestamp = bucket.minute.isoformat().replace("+00:00", "Z")
            labels = {
                "app": "who-update",
                "environment": "production",
                **bucket.labels,
            }
            if bucket.metric_name in COUNT_METRICS:
                metrics.append(
                    {
                        "name": bucket.metric_name,
                        "labels": labels,
                        "type": "IGAUGE",
                        "ts": timestamp,
                        "value": bucket.count,
                    }
                )
            elif bucket.metric_name in GAUGE_METRICS:
                metrics.append(
                    {
                        "name": bucket.metric_name,
                        "labels": labels,
                        "type": "DGAUGE",
                        "ts": timestamp,
                        "value": bucket.total,
                    }
                )
            else:
                metrics.extend(
                    [
                        {
                            "name": bucket.metric_name,
                            "labels": {**labels, "statistic": "average"},
                            "type": "DGAUGE",
                            "ts": timestamp,
                            "value": bucket.total / bucket.count,
                        },
                        {
                            "name": bucket.metric_name,
                            "labels": {**labels, "statistic": "maximum"},
                            "type": "DGAUGE",
                            "ts": timestamp,
                            "value": bucket.maximum,
                        },
                    ]
                )

        folder_id = getattr(settings, "YANDEX_CLOUD_FOLDER_ID", "b1g3vif8js3pms3lvofi")
        try:
            response = requests.post(
                MONITORING_WRITE_URL,
                params={"folderId": folder_id, "service": "custom"},
                headers={"Authorization": f"Bearer {_iam_token()}"},
                json={"metrics": metrics},
                timeout=(3, 15),
            )
            response.raise_for_status()
        except (requests.RequestException, KeyError, ValueError) as exc:
            raise CommandError(f"Could not export operational metrics: {exc}") from exc

        exported_at = timezone.now()
        OperationalMetricBucket.objects.filter(id__in=[bucket.id for bucket in buckets]).update(
            exported_at=exported_at
        )
        self.stdout.write(self.style.SUCCESS(f"Exported {len(metrics)} metric points"))
