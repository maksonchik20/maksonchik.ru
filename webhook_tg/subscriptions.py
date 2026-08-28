from __future__ import annotations

import html
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.utils import timezone

from .models import UserTg


OWNER_TELEGRAM_ID = 1394340082
TRIAL_DAYS = 14
REFERRAL_REWARD_DAYS = 7
CHECKOUT_SIGNING_SALT = "who-update-checkout-v1"
BOT_USERNAME = "who_update_bot"

PLAN_CONFIG = {
    "month": {"label": "1 месяц", "days": 30, "amount": Decimal("99.00")},
    "three_months": {"label": "3 месяца", "days": 90, "amount": Decimal("199.00")},
    "year": {"label": "1 год", "days": 365, "amount": Decimal("599.00")},
}


def apply_rollout_policy(bot_user: UserTg) -> bool:
    """Ограниченный доступ включён только владельцу до общего запуска."""
    if int(bot_user.user_id) != OWNER_TELEGRAM_ID or not bot_user.access_unlimited:
        return False
    bot_user.access_unlimited = False
    bot_user.save(update_fields=["access_unlimited"])
    return True


def start_trial_if_needed(bot_user: UserTg, at=None) -> bool:
    apply_rollout_policy(bot_user)
    return bot_user.ensure_trial_started(at=at)


def referral_link(bot_user: UserTg) -> str:
    return f"https://t.me/{BOT_USERNAME}?start=ref_{bot_user.referral_code}"


def register_referral(invitee: UserTg, start_payload: str) -> bool:
    payload = (start_payload or "").strip()
    if not payload.lower().startswith("ref_") or invitee.referred_by_id:
        return False
    code = payload[4:]
    inviter = UserTg.objects.filter(referral_code=code).first()
    if inviter is None or inviter.pk == invitee.pk:
        return False
    invitee.referred_by = inviter
    invitee.save(update_fields=["referred_by"])
    return True


@transaction.atomic
def grant_referral_reward(invitee: UserTg, at=None) -> bool:
    at = at or timezone.now()
    invitee = UserTg.objects.select_for_update().select_related("referred_by").get(pk=invitee.pk)
    if not invitee.referred_by_id or invitee.referral_rewarded_at:
        return False

    inviter = UserTg.objects.select_for_update().get(pk=invitee.referred_by_id)
    inviter.referral_bonus_days += REFERRAL_REWARD_DAYS
    update_fields = ["referral_bonus_days"]
    if not inviter.access_unlimited:
        base = inviter.access_expires_at if inviter.access_expires_at and inviter.access_expires_at > at else at
        inviter.access_expires_at = base + timedelta(days=REFERRAL_REWARD_DAYS)
        inviter.access_expired_notified_at = None
        update_fields.extend(["access_expires_at", "access_expired_notified_at"])
    inviter.save(update_fields=update_fields)

    invitee.referral_rewarded_at = at
    invitee.save(update_fields=["referral_rewarded_at"])

    from .telegram import tg_send_message

    transaction.on_commit(
        lambda: tg_send_message(
            inviter.chat_id,
            "🎁 <b>Реферальный бонус начислен</b>\n\n"
            f"Ваш друг подключил WhoUpdate. Доступ продлён на {REFERRAL_REWARD_DAYS} дней.",
        )
    )
    return True


def checkout_token(bot_user: UserTg, plan: str) -> str:
    return signing.dumps(
        {"user_id": bot_user.user_id, "plan": plan},
        salt=CHECKOUT_SIGNING_SALT,
        compress=True,
    )


def checkout_url(bot_user: UserTg, plan: str) -> str:
    site_url = getattr(settings, "WHO_UPDATE_SITE_URL", "https://maksonchik.ru").rstrip("/")
    return f"{site_url}/bot/subscribe/{plan}/{checkout_token(bot_user, plan)}/"


def subscription_keyboard(bot_user: UserTg) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "1 месяц — 99 ₽", "url": checkout_url(bot_user, "month")}],
            [{"text": "3 месяца — 199 ₽", "url": checkout_url(bot_user, "three_months")}],
            [{"text": "1 год — 599 ₽", "url": checkout_url(bot_user, "year")}],
        ]
    }


def access_status_text(bot_user: UserTg, at=None) -> str:
    at = at or timezone.now()
    if bot_user.access_unlimited:
        access_line = "Доступ: <b>бессрочный</b>"
    elif bot_user.has_active_access(at):
        expires = timezone.localtime(bot_user.access_expires_at)
        remaining = max((bot_user.access_expires_at - at).days + 1, 1)
        access_line = f"Доступ до: <b>{expires:%d.%m.%Y %H:%M}</b> МСК\nОсталось: <b>{remaining} дн.</b>"
    else:
        access_line = "Доступ: <b>закончился</b>"
    return (
        "⏳ <b>Доступ к WhoUpdate</b>\n\n"
        f"{access_line}\n"
        f"Бонус за приглашённых: <b>{bot_user.referral_bonus_days} дн.</b>\n\n"
        "Продлить доступ можно оплатой или приглашением друга."
    )


def referral_text(bot_user: UserTg) -> str:
    link = referral_link(bot_user)
    return (
        "👥 <b>Приглашайте друзей</b>\n\n"
        f"За каждого человека, который впервые подключит WhoUpdate по вашей ссылке, "
        f"вы получите <b>{REFERRAL_REWARD_DAYS} дней</b> доступа.\n\n"
        f"Ваша ссылка:\n<code>{html.escape(link)}</code>"
    )


def business_owner_for_message(msg: dict) -> UserTg | None:
    connection_id = msg.get("business_connection_id")
    if not connection_id:
        return None
    return UserTg.objects.filter(business_connection_id=connection_id).first()


def business_access_allowed(msg: dict) -> bool:
    bot_user = business_owner_for_message(msg)
    if bot_user is None or bot_user.has_active_access():
        return True

    if bot_user.access_expired_notified_at is None:
        bot_user.access_expired_notified_at = timezone.now()
        bot_user.save(update_fields=["access_expired_notified_at"])
        from .telegram import tg_send_message

        tg_send_message(
            bot_user.chat_id,
            "⛔️ <b>Пробный период WhoUpdate закончился</b>\n\n"
            "Продлите доступ оплатой или пригласите друга по своей реферальной ссылке.",
            reply_markup=subscription_keyboard(bot_user),
        )
    return False
