"""Durable Web Push outbox. Sources are polled independently and replayed safely."""

import json
from datetime import datetime, timedelta
from django.db import transaction
from django.utils import timezone
from .config import config
from .models import NotificationState, PushEvent, PushDelivery, PushSubscription


def who_events(since):
    from webhook_tg.models import WhoUpdatePaymentOrder
    from .stats import label
    from .common import money, iso

    now = timezone.now()
    result = []
    for field, category, title in [
        ("checkout_at", "checkout", "переход к оплате"),
        ("paid_at", "paid", "новая оплата"),
    ]:
        query = WhoUpdatePaymentOrder.objects.filter(
            is_test=False, **{field + "__gte": since, field + "__lte": now}
        ).select_related("user")
        if category == "paid":
            query = query.filter(status="paid")
        for order in query:
            result.append(
                dict(
                    key="who-{}-{}".format(category, order.pk),
                    category="who." + category,
                    occurred_at=iso(getattr(order, field)),
                    payload=dict(
                        title="Who-Update · " + title,
                        body="{} · {} ₽".format(label(order.user), money(order.amount)),
                        url="/panel/?project=who&tab=payments",
                    ),
                )
            )
    return result


def record_events(events, cutoff):
    for item in events:
        at = datetime.fromisoformat(item["occurred_at"])
        if not timezone.is_aware(at) or at < cutoff:
            continue
        with transaction.atomic():
            event, _ = PushEvent.objects.get_or_create(
                key=item["key"],
                defaults={
                    "category": item["category"],
                    "occurred_at": at,
                    "payload": item["payload"],
                },
            )
            for sub in PushSubscription.objects.filter(
                enabled=True,
                created_at__lte=at,
                user__is_active=True,
                user__is_superuser=True,
            ):
                PushDelivery.objects.get_or_create(event=event, subscription=sub)


def poll_sources():
    from .views import ghost_request

    for source in ("who", "ghost"):
        now = timezone.now()
        state, _ = NotificationState.objects.get_or_create(
            name=source, defaults={"activated_at": now}
        )
        since = max(
            state.activated_at,
            (state.last_poll_at or state.activated_at) - timedelta(minutes=5),
        )
        try:
            if source == "who":
                events = who_events(since)
            else:
                result, status = ghost_request("events", {"since": since.isoformat()})
                if status != 200:
                    raise RuntimeError("bridge HTTP {}".format(status))
                events = result["events"]
            record_events(events, state.activated_at)
            state.last_poll_at = now
            state.last_error = ""
        except Exception as exc:
            # Exceptions can contain endpoints/credentials; store only their class.
            state.last_error = "Не удалось обновить источник: " + type(exc).__name__
        state.save(update_fields=["last_poll_at", "last_error"])


def deliver(limit=50):
    from pywebpush import webpush

    conf = config()
    if not conf.get("vapid_private_key"):
        return 0
    sent = 0
    ids = list(
        PushDelivery.objects.filter(
            status__in=["pending", "sending"], next_attempt_at__lte=timezone.now()
        )
        .order_by("pk")
        .values_list("pk", flat=True)[:limit]
    )
    for pk in ids:
        with transaction.atomic():
            row = (
                PushDelivery.objects.select_for_update()
                .select_related("subscription__user", "event")
                .get(pk=pk)
            )
            if (
                row.status not in ("pending", "sending")
                or row.next_attempt_at > timezone.now()
            ):
                continue
            sub = row.subscription
            if not sub.enabled or not sub.user.is_active or not sub.user.is_superuser:
                row.status = "skipped"
                row.save(update_fields=["status"])
                continue
            row.status = "sending"
            row.attempts += 1
            row.next_attempt_at = timezone.now() + timedelta(minutes=3)
            row.save(update_fields=["status", "attempts", "next_attempt_at"])
        try:
            payload = dict(row.event.payload, tag=row.event.key)
            webpush(
                subscription_info=sub.subscription,
                data=json.dumps(payload, ensure_ascii=False),
                vapid_private_key=conf["vapid_private_key"],
                vapid_claims={"sub": conf["vapid_subject"]},
                ttl=86400,
                timeout=8,
            )
            row.status = "sent"
            row.sent_at = timezone.now()
            row.last_error = ""
            sent += 1
        except Exception as exc:
            response = getattr(exc, "response", None)
            code = getattr(exc, "status_code", None) or (
                getattr(response, "status_code", None) if response is not None else None
            )
            row.last_error = "HTTP {}".format(code) if code else type(exc).__name__
            if code in (404, 410):
                PushSubscription.objects.filter(pk=sub.pk).update(enabled=False)
                row.status = "skipped"
            else:
                row.status = (
                    "failed"
                    if row.attempts >= 8 or code in (400, 401, 403, 413)
                    else "pending"
                )
            row.next_attempt_at = timezone.now() + timedelta(
                seconds=min(3600, 30 * 2 ** min(row.attempts, 7))
            )
        row.save(update_fields=["status", "sent_at", "last_error", "next_attempt_at"])
    return sent
