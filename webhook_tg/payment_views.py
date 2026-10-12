from __future__ import annotations

import html
import hmac
import json
import logging
from decimal import Decimal

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.http import Http404, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .config import OWNER_CHAT_ID
from .background_tasks import schedule_payment_reconciliation
from .models import TelegramOutbox, UserTg, WhoUpdateOnboardingFunnel, WhoUpdatePaymentOrder
from .outbox import enqueue_outbox
from .subscriptions import CHECKOUT_SIGNING_SALT, plan_config_for_user
from .metrics import PAYMENT_EVENTS, observe_metric
from .metrika_offline import queue_purchase_conversion
from .yookassa import YooKassaError, create_payment, get_payment, is_webhook_ip
from .wheel import active_reward, is_pilot, order_text


logger = logging.getLogger(__name__)


def _user_label(bot_user):
    username = str(bot_user.username or "").strip().lstrip("@")
    username_line = f"@{html.escape(username)}" if username else "—"
    return (
        f"Пользователь: {html.escape(str(bot_user.first_name or '—'))}\n"
        f"Username: {username_line}\n"
        f"Telegram ID: <code>{bot_user.user_id}</code>"
    )


def _enqueue_owner_checkout_notification(order):
    WhoUpdatePaymentOrder.objects.filter(pk=order.pk, checkout_at__isnull=True).update(checkout_at=timezone.now())
    enqueue_outbox(
        chat_id=OWNER_CHAT_ID,
        method=TelegramOutbox.Method.SEND_MESSAGE,
        idempotency_key=f"who-update-payment-open:{order.public_id}",
        payload={
            "text": (
                "💳 <b>WhoUpdate: переход к оплате</b>\n\n"
                f"{_user_label(order.user)}\n"
                + _order_summary(order)
                +
                f"Заказ: <code>{order.public_id}</code>"
            ),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    )


def _enqueue_owner_paid_notification(order, expires):
    enqueue_outbox(
        chat_id=OWNER_CHAT_ID,
        method=TelegramOutbox.Method.SEND_MESSAGE,
        idempotency_key=f"who-update-payment-paid:{order.public_id}",
        payload={
            "text": (
                "✅ <b>WhoUpdate: получена оплата</b>\n\n"
                f"{_user_label(order.user)}\n"
                + _order_summary(order)
                +
                f"Доступ до: <b>{expires:%d.%m.%Y %H:%M}</b> МСК\n"
                f"Заказ: <code>{order.public_id}</code>"
            ),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    )


def _enqueue_user_paid_notification(order, expires):
    enqueue_outbox(
        chat_id=order.user.chat_id,
        method=TelegramOutbox.Method.SEND_MESSAGE,
        idempotency_key=f"who-update-payment-user-paid:{order.public_id}",
        payload={
            "text": (
                "✅ <b>Оплата WhoUpdate прошла</b>\n\n"
                f"Доступ продлён до <b>{expires:%d.%m.%Y %H:%M}</b> МСК."
                + (f"\n\n{order_text(order)}" if order_text(order) else "")
            ),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    )


def _order_summary(order):
    personal = order_text(order)
    if personal:
        return personal + "\n"
    return f"Тариф: <b>{html.escape(order.get_plan_display())}</b>\nСумма: <b>{order.amount} ₽</b>\n"


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
    return forwarded or request.META.get("REMOTE_ADDR", "")


def _payment_is_valid(payment, order):
    metadata = payment.get("metadata") or {}
    amount = payment.get("amount") or {}
    return (
        payment.get("status") == "succeeded"
        and bool(payment.get("paid"))
        and metadata.get("service") == "who_update"
        and str(metadata.get("order_id")) == str(order.public_id)
        and Decimal(str(amount.get("value", "0"))) == order.amount
        and amount.get("currency") == "RUB"
    )


@transaction.atomic
def payment_checkout(request, order, confirmation_url):
    # Serialize checkout reuse without taking an unnecessary user lock.
    order = (
        WhoUpdatePaymentOrder.objects.select_for_update()
        .get(pk=order.pk)
    )
    funnel = order.metrika_funnel
    if funnel is None:
        funnel = WhoUpdateOnboardingFunnel.objects.create(
            user_id=order.user_id,
            landing_path="/bot/checkout/",
            landing_viewed_at=timezone.now(),
            metrika_counter_id=settings.WHO_UPDATE_METRIKA_COUNTER_ID,
        )
        order.metrika_funnel = funnel
        order.save(update_fields=["metrika_funnel"])
    response = render(
        request,
        "webhook_tg/payment_checkout.html",
        {
            "order": order,
            "confirmation_url": confirmation_url,
            "checkoutFieldToValue": {
                "confirmationUrl": confirmation_url,
                "trackingCode": funnel.tracking_code,
                "counterId": funnel.metrika_counter_id
                or settings.WHO_UPDATE_METRIKA_COUNTER_ID,
                "captureUrl": reverse("who_update_metrika_client_id"),
            },
        },
    )
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response


@transaction.atomic
def fulfill_order(order, payment_id):
    # Pilot checkout also locks user -> order. Keep lock order consistent.
    if is_pilot(order.user):
        UserTg.objects.select_for_update().get(pk=order.user_id)
    order = WhoUpdatePaymentOrder.objects.select_for_update().select_related("user").get(pk=order.pk)
    if order.status == WhoUpdatePaymentOrder.Status.PAID:
        queue_purchase_conversion(order)
        return order

    payment = get_payment(payment_id)
    if not _payment_is_valid(payment, order):
        return order

    bot_user = UserTg.objects.select_for_update().get(pk=order.user_id)
    now = timezone.now()
    base = bot_user.access_expires_at if bot_user.access_expires_at and bot_user.access_expires_at > now else now
    bot_user.access_unlimited = False
    bot_user.access_expires_at = base + timezone.timedelta(days=order.duration_days)
    bot_user.access_expired_notified_at = None
    bot_user.save(update_fields=["access_unlimited", "access_expires_at", "access_expired_notified_at"])

    order.status = WhoUpdatePaymentOrder.Status.PAID
    order.paid_at = now
    order.is_test = bool(payment.get("test", False))
    order.yookassa_payment_id = payment_id
    order.access_expires_at_after = bot_user.access_expires_at
    order.save(
        update_fields=[
            "status",
            "paid_at",
            "is_test",
            "yookassa_payment_id",
            "access_expires_at_after",
        ]
    )
    from .partners import accrue_commission
    accrue_commission(order)
    if is_pilot(bot_user):
        from .models import WhoUpdateWheelReward
        WhoUpdateWheelReward.objects.filter(user=bot_user, reserved_order=order, used_at=None).update(used_at=now)
    queue_purchase_conversion(order, occurred_at=now)
    observe_metric(PAYMENT_EVENTS, 1, {"status": "paid", "plan": order.plan})

    expires = timezone.localtime(bot_user.access_expires_at)

    def notify_payment_completed():
        _enqueue_user_paid_notification(order, expires)
        _enqueue_owner_paid_notification(order, expires)

    transaction.on_commit(notify_payment_completed)
    return order


@require_GET
def subscribe(request, plan, token):
    try:
        payload = signing.loads(token, salt=CHECKOUT_SIGNING_SALT, max_age=30 * 24 * 3600)
    except signing.BadSignature:
        return HttpResponseForbidden("Ссылка на оплату недействительна")
    if payload.get("plan") != plan:
        return HttpResponseForbidden("Тариф не совпадает")

    bot_user = get_object_or_404(UserTg, user_id=payload.get("user_id"))
    config = plan_config_for_user(bot_user, plan)
    if config is None:
        raise Http404
    if bot_user.access_unlimited:
        return HttpResponse("Для этого аккаунта пока включён бессрочный доступ.", status=409)

    if is_pilot(bot_user) and (payload.get("wheel_reward") is not None or active_reward(bot_user)):
        from .wheel_checkout import checkout
        return checkout(bot_user, plan, payload.get("wheel_reward"), request=request)

    order = WhoUpdatePaymentOrder.objects.create(
        user=bot_user,
        plan=plan,
        duration_days=config["days"],
        amount=config["amount"],
    )
    return_url = (
        settings.WHO_UPDATE_SITE_URL.rstrip("/")
        + reverse("who_update_payment_result", args=[order.public_id])
    )
    try:
        payment = create_payment(
            amount=order.amount,
            description=f"WhoUpdate — {config['label']}",
            return_url=return_url,
            metadata={"service": "who_update", "order_id": str(order.public_id)},
        )
    except YooKassaError:
        logger.exception("WhoUpdate payment creation failed order=%s", order.public_id)
        order.status = WhoUpdatePaymentOrder.Status.FAILED
        order.save(update_fields=["status"])
        observe_metric(PAYMENT_EVENTS, 1, {"status": "failed", "plan": order.plan})
        return HttpResponse("Не удалось создать оплату. Попробуйте позже.", status=502)

    order.yookassa_payment_id = payment["id"]
    order.save(update_fields=["yookassa_payment_id"])
    if not payment.get("confirmation_url"):
        order.status = WhoUpdatePaymentOrder.Status.FAILED
        order.save(update_fields=["status"])
        observe_metric(PAYMENT_EVENTS, 1, {"status": "failed", "plan": order.plan})
        return HttpResponse("ЮKassa не вернула ссылку на оплату.", status=502)
    schedule_payment_reconciliation(order)
    _enqueue_owner_checkout_notification(order)
    observe_metric(PAYMENT_EVENTS, 1, {"status": "checkout", "plan": order.plan})
    return payment_checkout(request, order, payment["confirmation_url"])


@require_GET
def payment_result(request, public_id):
    order = get_object_or_404(WhoUpdatePaymentOrder, public_id=public_id)
    if order.status != WhoUpdatePaymentOrder.Status.PAID and order.yookassa_payment_id:
        try:
            fulfill_order(order, order.yookassa_payment_id)
            order.refresh_from_db()
        except YooKassaError:
            logger.exception("WhoUpdate payment verification failed order=%s", order.public_id)
    return render(request, "webhook_tg/payment_result.html", {"order": order, "wheel_order_text": order_text(order)})


@csrf_exempt
@require_POST
def yookassa_webhook(request):
    configured_token = str(getattr(settings, "WHO_UPDATE_PAYMENT_WEBHOOK_TOKEN", "") or "")
    supplied_token = request.headers.get("X-Who-Update-Payment-Token", "")
    trusted_forward = bool(configured_token) and hmac.compare_digest(configured_token, supplied_token)
    if not trusted_forward and not is_webhook_ip(_client_ip(request)):
        return HttpResponseForbidden("Untrusted webhook")

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return HttpResponse(status=400)

    event = payload.get("event")
    payment = payload.get("object") or {}
    metadata = payment.get("metadata") or {}
    if metadata.get("service") != "who_update":
        return HttpResponse(status=200)
    order = WhoUpdatePaymentOrder.objects.filter(public_id=metadata.get("order_id")).first()
    if order is None:
        logger.error("WhoUpdate order not found: %s", metadata.get("order_id"))
        return HttpResponse(status=200)

    if event == "payment.succeeded":
        try:
            fulfill_order(order, payment.get("id", ""))
        except YooKassaError:
            logger.exception("WhoUpdate webhook verification failed order=%s", order.public_id)
            return HttpResponse(status=503)
    elif event == "payment.canceled":
        WhoUpdatePaymentOrder.objects.filter(
            pk=order.pk,
            status=WhoUpdatePaymentOrder.Status.PENDING,
        ).update(status=WhoUpdatePaymentOrder.Status.CANCELED)
        observe_metric(PAYMENT_EVENTS, 1, {"status": "canceled", "plan": order.plan})
    return HttpResponse(status=200)
