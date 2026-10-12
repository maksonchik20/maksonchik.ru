"""Reserve exactly one order per prize, including retries after provider timeouts."""
from requests import RequestException
from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone

from .models import UserTg, WhoUpdatePaymentOrder, WhoUpdateWheelReward
from .wheel import is_pilot, reward_config, latest_reward, INSTANT_DAY_PRIZES
from .yookassa import YooKassaError, create_payment


def checkout(user, plan, reward_id=None, *, request=None):
    from .subscriptions import PLAN_CONFIG
    from .payment_views import _enqueue_owner_checkout_notification, payment_checkout
    from .background_tasks import schedule_payment_reconciliation

    if not is_pilot(user) or plan not in PLAN_CONFIG:
        return HttpResponse(status=403)
    # Commit the reservation before contacting the provider: a timeout or crash
    # must retry with the SAME order and provider idempotence key.
    with transaction.atomic():
        UserTg.objects.select_for_update().get(pk=user.pk)
        latest = latest_reward(user)
        reward = WhoUpdateWheelReward.objects.filter(user=user, pk=reward_id).first() if reward_id is not None else latest
        if not reward or reward.used_at or (reward_id is not None and reward.pk != reward_id):
            return HttpResponse("Бонус недоступен. Открой колесо заново.", status=409)
        order = reward.reserved_order
        if order and order.status in (WhoUpdatePaymentOrder.Status.CANCELED, WhoUpdatePaymentOrder.Status.FAILED):
            order = None
        if reward.prize in INSTANT_DAY_PRIZES and not order:
            return HttpResponse("Этот приз начисляется бесплатно, без оплаты. Открой колесо заново.", status=409)
        if not order:
            if not latest or reward.pk != latest.pk:
                return HttpResponse("Этот бонус заменён новым призом. Открой актуальные тарифы в колесе.", status=409)
            if reward.expires_at <= timezone.now():
                return HttpResponse("Срок бонуса истёк. Открой актуальные тарифы в боте.", status=409)
            config = reward_config(PLAN_CONFIG[plan], reward)
            order = WhoUpdatePaymentOrder.objects.create(
                user=user, plan=plan, duration_days=config["days"], amount=config["amount"],
            )
            reward.reserved_order = order
            reward.confirmation_url = ""
            reward.save(update_fields=["reserved_order", "confirmation_url"])
        elif order.plan != plan:
            return HttpResponse(
                "Бонус уже закреплён за другим тарифом. Вернись в колесо и продолжи созданную оплату.", status=409,
            )
        elif order.status != WhoUpdatePaymentOrder.Status.PENDING:
            return HttpResponse("Заказ уже обработан. Открой колесо заново.", status=409)

    reserved_order_id = order.pk
    with transaction.atomic():
        UserTg.objects.select_for_update().get(pk=user.pk)
        reward = WhoUpdateWheelReward.objects.select_related("reserved_order").get(pk=reward.pk)
        order = reward.reserved_order
        if order.pk != reserved_order_id or order.plan != plan:
            return HttpResponse("Оплата изменилась. Открой колесо заново.", status=409)
        if reward.used_at or order.status != WhoUpdatePaymentOrder.Status.PENDING:
            return HttpResponse("Заказ уже обработан. Открой колесо заново.", status=409)
        if reward.confirmation_url:
            return (
                payment_checkout(request, order, reward.confirmation_url)
                if request is not None else redirect(reward.confirmation_url)
            )
        # YooKassa stores idempotence keys for 24h. Never retry an unknown
        # payment after that window: reconciliation/manual verification first.
        if order.created_at < timezone.now() - timezone.timedelta(hours=23):
            return HttpResponse("Проверяем ранее созданную оплату. Обратись в поддержку.", status=409)
        config = reward_config(PLAN_CONFIG[plan], reward)
        try:
            payment = create_payment(
                amount=order.amount, description=f"WhoUpdate — {config['label']}",
                return_url=settings.WHO_UPDATE_SITE_URL.rstrip("/") + reverse("who_update_payment_result", args=[order.public_id]),
                metadata={"service": "who_update", "order_id": str(order.public_id)},
                idempotence_key=str(order.public_id),
            )
        except (YooKassaError, RequestException):
            return HttpResponse("Не удалось создать оплату. Повтори попытку — бонус сохранён.", status=502)
        if not payment.get("id") or not payment.get("confirmation_url"):
            return HttpResponse("Платёж ещё не готов. Повтори попытку — бонус сохранён.", status=502)
        order.yookassa_payment_id = payment["id"]
        order.save(update_fields=["yookassa_payment_id"])
        reward.confirmation_url = payment["confirmation_url"]
        reward.save(update_fields=["confirmation_url"])
        schedule_payment_reconciliation(order)
        _enqueue_owner_checkout_notification(order)
        return (
            payment_checkout(request, order, reward.confirmation_url)
            if request is not None else redirect(reward.confirmation_url)
        )
