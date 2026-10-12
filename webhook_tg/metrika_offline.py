from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import timedelta

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .metrics import METRIKA_OFFLINE_EVENTS, observe_metric
from .models import (
    WhoUpdateMetrikaConversion,
    WhoUpdateOnboardingFunnel,
    WhoUpdatePaymentOrder,
)


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
PURCHASE_TARGET = "who_update_purchase"


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
            if not created and conversion.status == WhoUpdateMetrikaConversion.Status.PENDING and conversion.attempts == 0:
                fieldToValue = {}
                for field, value in (
                    ("target", target),
                    ("occurred_at", getattr(funnel, timestamp_field)),
                    ("identifier_type", identifier_type),
                    ("identifier", identifier_value),
                ):
                    if getattr(conversion, field) != value:
                        fieldToValue[field] = value
                if fieldToValue:
                    WhoUpdateMetrikaConversion.objects.filter(
                        pk=conversion.pk, attempts=0,
                        status=WhoUpdateMetrikaConversion.Status.PENDING,
                    ).update(**fieldToValue)
    return created_count


def _purchase_funnel_for(order, *, now=None, attribution_days=21):
    # Preserve the actual acquisition visit across browsers. Never mix its
    # YCLID with a different browser's checkout ClientID.
    if not order.paid_at:
        return None
    now = now or timezone.now()
    earliest = max(now, order.paid_at) - timedelta(days=attribution_days)
    identifiers = Q(yclid__gt="") | Q(metrika_client_id__gt="")
    acquisition = (
        WhoUpdateOnboardingFunnel.objects.filter(
            identifiers,
            user_id=order.user_id,
            landing_viewed_at__gte=earliest,
            landing_viewed_at__lte=order.paid_at,
            telegram_started_at__lte=order.paid_at,
        )
        .exclude(landing_path="/bot/checkout/")
        .order_by("-landing_viewed_at", "-id")
    )
    funnel = acquisition.filter(yclid__gt="").first() or acquisition.first()
    if funnel is not None:
        return funnel
    checkout = order.metrika_funnel
    if (
        checkout is not None and checkout.user_id == order.user_id
        and checkout.landing_viewed_at
        and earliest <= checkout.landing_viewed_at <= order.paid_at
        and _identifier_for(checkout)
    ):
        return checkout
    return None


def queue_purchase_conversion(
    order: WhoUpdatePaymentOrder,
    *,
    occurred_at=None,
    counter_id: int | None = None,
    attribution_days: int = 21,
    now=None,
) -> WhoUpdateMetrikaConversion | None:
    """Ставит подтверждённую оплату в очередь Метрики ровно один раз."""
    if order.status != WhoUpdatePaymentOrder.Status.PAID or order.is_test or not order.paid_at:
        return None
    existing = WhoUpdateMetrikaConversion.objects.filter(payment_order=order).first()
    if existing is not None:
        return existing
    # Never turn a historical payment into a new conversion during backfill.
    occurred_at = order.paid_at
    funnel = _purchase_funnel_for(order, now=now, attribution_days=attribution_days)
    if funnel is None:
        return None

    identifier = _identifier_for(funnel)
    if identifier is None:
        return None
    identifier_type, identifier_value = identifier
    conversion, _ = WhoUpdateMetrikaConversion.objects.get_or_create(
        payment_order=order,
        defaults={
            "funnel": funnel,
            "event_type": WhoUpdateMetrikaConversion.EventType.PURCHASE,
            "target": PURCHASE_TARGET,
            "counter_id": funnel.metrika_counter_id or counter_id
            or int(getattr(settings, "YANDEX_METRIKA_COUNTER_ID", 112093587)),
            "occurred_at": occurred_at,
            "identifier_type": identifier_type,
            "identifier": identifier_value,
            "client_id": funnel.metrika_client_id,
            "value": order.amount,
            "currency": "RUB",
            "next_attempt_at": occurred_at,
        },
    )
    return conversion


def sync_purchase_conversions(*, counter_id: int, now=None, attribution_days: int = 20) -> int:
    """Recover late attribution without replaying fulfilled payments or notifications."""
    now = now or timezone.now()
    orders = WhoUpdatePaymentOrder.objects.filter(
        status=WhoUpdatePaymentOrder.Status.PAID,
        is_test=False,
        paid_at__gte=now - timedelta(days=attribution_days),
        paid_at__lte=now,
        metrika_conversion__isnull=True,
    ).select_related("metrika_funnel").order_by("paid_at", "id")
    created_count = 0
    for order in orders.iterator():
        conversion = queue_purchase_conversion(order, counter_id=counter_id, now=now)
        created_count += int(conversion is not None)

    # Enrich only payloads which have NEVER been attempted. On a timeout the
    # provider may already have accepted the CSV: retries must stay identical.
    pending = WhoUpdateMetrikaConversion.objects.filter(
        event_type=WhoUpdateMetrikaConversion.EventType.PURCHASE,
        status=WhoUpdateMetrikaConversion.Status.PENDING,
        attempts=0,
        payment_order__status=WhoUpdatePaymentOrder.Status.PAID,
        payment_order__is_test=False,
    ).select_related("funnel", "payment_order__metrika_funnel")
    for conversion in pending.iterator():
        order = conversion.payment_order
        funnel = _purchase_funnel_for(order, now=now)
        fieldToValue = {}
        if funnel is not None:
            identifier_type, identifier_value = _identifier_for(funnel)
            fieldToValue = {
                "funnel": funnel,
                "identifier_type": identifier_type,
                "identifier": identifier_value,
                "client_id": funnel.metrika_client_id,
                "counter_id": funnel.metrika_counter_id or (
                    conversion.counter_id if funnel.pk == conversion.funnel_id else counter_id
                ),
            }
        if fieldToValue:
            WhoUpdateMetrikaConversion.objects.filter(
                pk=conversion.pk, attempts=0,
                status=WhoUpdateMetrikaConversion.Status.PENDING,
            ).update(**fieldToValue)
    return created_count


@transaction.atomic
def recover_purchase_linkage(conversion_id: int, *, now=None) -> bool:
    """Add the previously omitted ClientID only after a definitive failed upload.

    Caller must recheck the provider status before recovery. Already processed,
    ambiguous and expired events are deliberately not replayed.
    """
    now = now or timezone.now()
    conversion = WhoUpdateMetrikaConversion.objects.select_for_update().get(pk=conversion_id)
    funnel = conversion.funnel
    order = conversion.payment_order
    if not (
        order is not None and order.status == WhoUpdatePaymentOrder.Status.PAID
        and not order.is_test and order.paid_at == conversion.occurred_at
        and conversion.event_type == WhoUpdateMetrikaConversion.EventType.PURCHASE
        and conversion.status == WhoUpdateMetrikaConversion.Status.FAILED
        and conversion.api_status == "LINKAGE_FAILURE"
        and conversion.identifier_type == WhoUpdateMetrikaConversion.IdentifierType.YCLID
        and conversion.identifier == funnel.yclid
        and not conversion.client_id and funnel.metrika_client_id
        and funnel.user_id == order.user_id
        and funnel.landing_viewed_at
        and now - timedelta(days=20) <= funnel.landing_viewed_at <= conversion.occurred_at
        and (not funnel.metrika_counter_id or funnel.metrika_counter_id == conversion.counter_id)
    ):
        return False
    previous_upload_id = conversion.api_upload_id
    conversion.upload_history = [*conversion.upload_history, {
        "id": previous_upload_id,
        "status": "LINKAGE_FAILURE",
        "action": "add_client_id",
        "at": now.isoformat(),
    }]
    conversion.client_id = funnel.metrika_client_id
    conversion.status = WhoUpdateMetrikaConversion.Status.PENDING
    conversion.next_attempt_at = now
    conversion.api_upload_id = None
    conversion.api_status = ""
    conversion.processed_at = None
    conversion.last_error = f"Confirmed LINKAGE_FAILURE; previous upload={previous_upload_id}; added ClientID"
    conversion.save(update_fields=[
        "client_id", "status", "next_attempt_at", "api_upload_id", "api_status",
        "processed_at", "last_error", "upload_history", "updated_at",
    ])
    return True


def recover_failed_purchase_conversions(*, token: str, now=None, limit: int = 20) -> int:
    now = now or timezone.now()
    failed = WhoUpdateMetrikaConversion.objects.filter(
        event_type=WhoUpdateMetrikaConversion.EventType.PURCHASE,
        status=WhoUpdateMetrikaConversion.Status.FAILED,
        api_status="LINKAGE_FAILURE",
        api_upload_id__isnull=False,
        identifier_type=WhoUpdateMetrikaConversion.IdentifierType.YCLID,
        client_id="",
        funnel__metrika_client_id__gt="",
        funnel__landing_viewed_at__gte=now - timedelta(days=20),
    ).order_by("occurred_at", "id")[:limit]
    recovered = 0
    for conversion in failed:
        try:
            response = METRIKA_SESSION.get(
                UPLOAD_STATUS_URL.format(counter_id=conversion.counter_id, upload_id=conversion.api_upload_id),
                headers={"Authorization": f"OAuth {token}"},
                timeout=(3, 15),
            )
            response.raise_for_status()
            if str(response.json()["uploading"]["status"]).upper() != "LINKAGE_FAILURE":
                continue
        except (requests.RequestException, KeyError, TypeError, ValueError):
            continue
        recovered += int(recover_purchase_linkage(conversion.pk, now=now))
    return recovered


def _csv_payload(conversions: list[WhoUpdateMetrikaConversion]) -> tuple[str, bytes]:
    identifier_type = conversions[0].identifier_type
    identifier_header = (
        "Yclid"
        if identifier_type == WhoUpdateMetrikaConversion.IdentifierType.YCLID
        else "ClientId"
    )
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    include_client_id = identifier_header == "Yclid" and any(item.client_id for item in conversions)
    identifier_headers = (identifier_header, "ClientId") if include_client_id else (identifier_header,)
    writer.writerow((*identifier_headers, "Target", "DateTime", "Price", "Currency"))
    for conversion in conversions:
        identifier_values = (conversion.identifier, conversion.client_id) if include_client_id else (conversion.identifier,)
        writer.writerow(
            (
                *identifier_values,
                conversion.target,
                int(conversion.occurred_at.timestamp()),
                format(conversion.value, "f") if conversion.value is not None else "",
                conversion.currency,
            )
        )
    return identifier_header.lower(), stream.getvalue().encode("utf-8")


def _retry_delay(attempts: int) -> timedelta:
    return timedelta(minutes=min(5 * (2 ** max(attempts - 1, 0)), 360))


def _short_error(exc: Exception) -> str:
    return str(exc).strip()[:1000] or exc.__class__.__name__


@transaction.atomic(durable=True)
def _begin_upload_attempt(batch, *, counter_id, identifier_type, now):
    # Commit the attempt BEFORE the network request. Even a killed process
    # must leave attempts > 0, prohibiting later attribution/payload changes.
    locked = list(
        WhoUpdateMetrikaConversion.objects.select_for_update().filter(
            pk__in=[item.pk for item in batch],
            status=WhoUpdateMetrikaConversion.Status.PENDING,
            next_attempt_at__lte=now,
            counter_id=counter_id,
            identifier_type=identifier_type,
        ).order_by("occurred_at", "id")
    )
    if not locked:
        return None
    csv_kind, payload = _csv_payload(locked)
    retry_at = now + _retry_delay(max(item.attempts for item in locked) + 1)
    for conversion in locked:
        conversion.attempts += 1
        conversion.next_attempt_at = retry_at
        conversion.last_error = "Upload attempt started; provider acknowledgement not saved yet"
    WhoUpdateMetrikaConversion.objects.bulk_update(
        locked, ["attempts", "next_attempt_at", "last_error"],
    )
    return locked, csv_kind, payload


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

    counterIdentifierPurchaseToConversions = defaultdict(list)
    for conversion in conversions:
        # Upload status describes the whole file. Keep purchases isolated so a
        # matched /start cannot conceal a failed payment in the same batch.
        purchase_id = conversion.pk if conversion.event_type == WhoUpdateMetrikaConversion.EventType.PURCHASE else None
        counterIdentifierPurchaseToConversions[(conversion.counter_id, conversion.identifier_type, purchase_id)].append(conversion)

    submitted_count = 0
    for (stored_counter_id, identifier_type, _), batch in counterIdentifierPurchaseToConversions.items():
        attempt = _begin_upload_attempt(
            batch, counter_id=stored_counter_id, identifier_type=identifier_type, now=now,
        )
        if attempt is None:
            continue
        batch, csv_kind, payload = attempt
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
            WhoUpdateMetrikaConversion.objects.filter(
                pk__in=[item.pk for item in batch]
            ).update(
                last_error=_short_error(exc),
            )
            observe_metric(
                METRIKA_OFFLINE_EVENTS,
                len(batch),
                {"event": "upload_failed", "identifier": identifier_type},
            )
            continue

        fieldToUpdate = {
            "status": WhoUpdateMetrikaConversion.Status.SUBMITTED,
            "api_upload_id": upload_id,
            "api_status": api_status,
            "last_error": "",
            "submitted_at": now,
        }
        if api_status in TERMINAL_SUCCESS_STATUSES:
            fieldToUpdate.update(
                status=WhoUpdateMetrikaConversion.Status.PROCESSED,
                processed_at=now,
            )
        elif api_status in TERMINAL_FAILURE_STATUSES:
            fieldToUpdate.update(
                status=WhoUpdateMetrikaConversion.Status.FAILED,
                processed_at=now,
                last_error="Метрика не нашла визит для переданного идентификатора",
            )
        WhoUpdateMetrikaConversion.objects.filter(
            pk__in=[item.pk for item in batch]
        ).update(**fieldToUpdate)
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
