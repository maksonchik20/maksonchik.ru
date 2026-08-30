from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import timedelta

import requests
from django.db.models import F, Q
from django.utils import timezone

from .metrics import METRIKA_OFFLINE_EVENTS, observe_metric
from .models import WhoUpdateMetrikaConversion, WhoUpdateOnboardingFunnel


UPLOAD_URL = (
    "https://api-metrika.yandex.net/management/v1/counter/"
    "{counter_id}/offline_conversions/upload"
)
UPLOAD_STATUS_URL = (
    "https://api-metrika.yandex.net/management/v1/counter/"
    "{counter_id}/offline_conversions/uploading/{upload_id}"
)
TERMINAL_SUCCESS_STATUSES = {"PROCESSED"}
TERMINAL_FAILURE_STATUSES = {"LINKAGE_FAILURE"}
METRIKA_SESSION = requests.Session()


def _identifier_for(funnel: WhoUpdateOnboardingFunnel) -> tuple[str, str] | None:
    if funnel.yclid:
        return WhoUpdateMetrikaConversion.IdentifierType.YCLID, funnel.yclid
    if funnel.metrika_client_id:
        return WhoUpdateMetrikaConversion.IdentifierType.CLIENT_ID, funnel.metrika_client_id
    return None


def sync_conversion_queue(
    *,
    counter_id: int,
    now=None,
    attribution_days: int = 20,
) -> int:
    """Добавляет новые события в очередь, включая накопленные до запуска задачи."""
    now = now or timezone.now()
    earliest = now - timedelta(days=attribution_days)
    specs = (
        (
            WhoUpdateMetrikaConversion.EventType.START,
            "telegram_started_at",
            "who_update_start",
        ),
        (
            WhoUpdateMetrikaConversion.EventType.CONNECTED,
            "connected_at",
            "who_update_connected",
        ),
    )
    created_count = 0
    identifiers = Q(yclid__gt="") | Q(metrika_client_id__gt="")

    for event_type, timestamp_field, target in specs:
        funnels = (
            WhoUpdateOnboardingFunnel.objects.filter(
                identifiers,
                **{
                    f"{timestamp_field}__isnull": False,
                    f"{timestamp_field}__gte": earliest,
                },
            )
            .only("id", "yclid", "metrika_client_id", timestamp_field)
            .order_by(timestamp_field, "id")
        )
        for funnel in funnels.iterator():
            identifier = _identifier_for(funnel)
            if identifier is None:
                continue
            identifier_type, identifier_value = identifier
            conversion, created = WhoUpdateMetrikaConversion.objects.get_or_create(
                funnel=funnel,
                event_type=event_type,
                defaults={
                    "target": target,
                    "counter_id": counter_id,
                    "occurred_at": getattr(funnel, timestamp_field),
                    "identifier_type": identifier_type,
                    "identifier": identifier_value,
                    "next_attempt_at": now,
                },
            )
            created_count += int(created)
            if not created and conversion.status == WhoUpdateMetrikaConversion.Status.PENDING:
                changes = {}
                for field, value in (
                    ("target", target),
                    ("occurred_at", getattr(funnel, timestamp_field)),
                    ("identifier_type", identifier_type),
                    ("identifier", identifier_value),
                ):
                    if getattr(conversion, field) != value:
                        changes[field] = value
                if changes:
                    WhoUpdateMetrikaConversion.objects.filter(pk=conversion.pk).update(**changes)
    return created_count


def _csv_payload(conversions: list[WhoUpdateMetrikaConversion]) -> tuple[str, bytes]:
    identifier_type = conversions[0].identifier_type
    identifier_header = (
        "Yclid"
        if identifier_type == WhoUpdateMetrikaConversion.IdentifierType.YCLID
        else "ClientId"
    )
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow((identifier_header, "Target", "DateTime"))
    for conversion in conversions:
        writer.writerow(
            (
                conversion.identifier,
                conversion.target,
                int(conversion.occurred_at.timestamp()),
            )
        )
    return identifier_header.lower(), stream.getvalue().encode("utf-8")


def _retry_delay(attempts: int) -> timedelta:
    return timedelta(minutes=min(5 * (2 ** max(attempts - 1, 0)), 360))


def _short_error(exc: Exception) -> str:
    return str(exc).strip()[:1000] or exc.__class__.__name__


def upload_pending_conversions(
    *,
    token: str,
    limit: int = 500,
    now=None,
) -> int:
    now = now or timezone.now()
    ready_before = now - timedelta(seconds=60)
    conversions = list(
        WhoUpdateMetrikaConversion.objects.filter(
            status=WhoUpdateMetrikaConversion.Status.PENDING,
            next_attempt_at__lte=now,
            occurred_at__lte=ready_before,
        ).order_by("occurred_at", "id")[:limit]
    )
    if not conversions:
        return 0

    grouped = defaultdict(list)
    for conversion in conversions:
        grouped[(conversion.counter_id, conversion.identifier_type)].append(conversion)

    submitted_count = 0
    for (stored_counter_id, identifier_type), batch in grouped.items():
        csv_kind, payload = _csv_payload(batch)
        comment = f"WhoUpdate{now:%Y%m%dT%H%M%S}{csv_kind}"
        try:
            response = METRIKA_SESSION.post(
                UPLOAD_URL.format(counter_id=stored_counter_id),
                params={"type": "BASIC", "comment": comment},
                headers={"Authorization": f"OAuth {token}"},
                files={"file": (f"who-update-{csv_kind}.csv", payload, "text/csv")},
                timeout=(3, 20),
            )
            response.raise_for_status()
            uploading = response.json()["uploading"]
            upload_id = int(uploading["id"])
            api_status = str(uploading.get("status") or "UPLOADED").upper()
        except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
            attempts = max(item.attempts for item in batch) + 1
            WhoUpdateMetrikaConversion.objects.filter(
                pk__in=[item.pk for item in batch]
            ).update(
                attempts=F("attempts") + 1,
                next_attempt_at=now + _retry_delay(attempts),
                last_error=_short_error(exc),
            )
            observe_metric(
                METRIKA_OFFLINE_EVENTS,
                len(batch),
                {"event": "upload_failed", "identifier": identifier_type},
            )
            continue

        update = {
            "status": WhoUpdateMetrikaConversion.Status.SUBMITTED,
            "attempts": F("attempts") + 1,
            "api_upload_id": upload_id,
            "api_status": api_status,
            "last_error": "",
            "submitted_at": now,
        }
        if api_status in TERMINAL_SUCCESS_STATUSES:
            update.update(
                status=WhoUpdateMetrikaConversion.Status.PROCESSED,
                processed_at=now,
            )
        elif api_status in TERMINAL_FAILURE_STATUSES:
            update.update(
                status=WhoUpdateMetrikaConversion.Status.FAILED,
                processed_at=now,
                last_error="Метрика не нашла визит для переданного идентификатора",
            )
        WhoUpdateMetrikaConversion.objects.filter(
            pk__in=[item.pk for item in batch]
        ).update(**update)
        submitted_count += len(batch)
        observe_metric(
            METRIKA_OFFLINE_EVENTS,
            len(batch),
            {"event": "submitted", "identifier": identifier_type},
        )
    return submitted_count


def reconcile_submitted_conversions(
    *,
    token: str,
    limit: int = 50,
    now=None,
) -> int:
    now = now or timezone.now()
    uploads = list(
        WhoUpdateMetrikaConversion.objects.filter(
            status=WhoUpdateMetrikaConversion.Status.SUBMITTED,
            api_upload_id__isnull=False,
        )
        .order_by("counter_id", "api_upload_id")
        .values_list("counter_id", "api_upload_id")
        .distinct()[:limit]
    )
    reconciled = 0
    for stored_counter_id, upload_id in uploads:
        try:
            response = METRIKA_SESSION.get(
                UPLOAD_STATUS_URL.format(
                    counter_id=stored_counter_id,
                    upload_id=upload_id,
                ),
                headers={"Authorization": f"OAuth {token}"},
                timeout=(3, 15),
            )
            response.raise_for_status()
            api_status = str(response.json()["uploading"]["status"]).upper()
        except (requests.RequestException, KeyError, TypeError, ValueError):
            continue

        queryset = WhoUpdateMetrikaConversion.objects.filter(
            status=WhoUpdateMetrikaConversion.Status.SUBMITTED,
            counter_id=stored_counter_id,
            api_upload_id=upload_id,
        )
        update = {"api_status": api_status}
        metric_event = "status_checked"
        if api_status in TERMINAL_SUCCESS_STATUSES:
            update.update(
                status=WhoUpdateMetrikaConversion.Status.PROCESSED,
                processed_at=now,
            )
            metric_event = "processed"
        elif api_status in TERMINAL_FAILURE_STATUSES:
            update.update(
                status=WhoUpdateMetrikaConversion.Status.FAILED,
                processed_at=now,
                last_error="Метрика не нашла визит для переданного идентификатора",
            )
            metric_event = "linkage_failure"
        changed = queryset.update(**update)
        reconciled += changed
        if changed:
            observe_metric(
                METRIKA_OFFLINE_EVENTS,
                changed,
                {"event": metric_event},
            )
    return reconciled
