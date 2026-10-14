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
TRIAL_DAYS = int(getattr(settings, "WHO_UPDATE_TRIAL_DAYS", 7))
REFERRAL_REWARD_DAYS = 7
CHECKOUT_SIGNING_SALT = "who-update-checkout-v1"
BOT_USERNAME = "who_update_bot"

PURCHASE_PROMOTION_TEXT = (
    "🍀 <b>А может повезёт?</b>\n"
    "Считаем оплаченные покупки <b>всех пользователей вместе</b>. "
    "Если ваша покупка окажется 5-й, 10-й, 15-й и так далее — "
    "<b>удвоим срок по выбранному тарифу</b>:\n\n"
    "• 1 месяц → <b>2 месяца</b>\n"
    "• 3 месяца → <b>6 месяцев</b>\n"
    "• 1 год → <b>2 года</b>\n\n"
    "Бонус может выпасть уже при первой покупке!"
)

PLAN_CONFIG = {
    "month": {"label": "1 месяц", "days": 30, "amount": Decimal("99.00")},
    "three_months": {"label": "3 месяца", "days": 90, "amount": Decimal("249.00")},
    "year": {"label": "1 год", "days": 365, "amount": Decimal("800.00")},
}


def is_subscription_rollout_user(bot_user: UserTg) -> bool:
    """Подписочная модель включена для всех пользователей WhoUpdate."""
    return True


def plan_config_for_user(bot_user: UserTg, plan: str) -> dict | None:
    config = PLAN_CONFIG.get(plan)
    if config is None:
        return None
    from .wheel import active_reward, reward_config
    reward = active_reward(bot_user)
    return reward_config(config, reward) if reward else dict(config)


def _rubles(amount: Decimal) -> str:
    return f"{amount.quantize(Decimal('1'))} ₽"


def apply_rollout_policy(bot_user: UserTg) -> bool:
    """Отключить устаревший бессрочный доступ для любого пользователя."""
    if not is_subscription_rollout_user(bot_user) or not bot_user.access_unlimited:
        return False
    bot_user.access_unlimited = False
    bot_user.save(update_fields=["access_unlimited"])
    return True


def start_trial_if_needed(bot_user: UserTg, at=None) -> bool:
    apply_rollout_policy(bot_user)
    return bot_user.ensure_trial_started(at=at)


def referral_link(bot_user: UserTg) -> str:
    return f"https://t.me/{BOT_USERNAME}?start=ref_{bot_user.referral_code}"


@transaction.atomic
def register_referral(invitee: UserTg, start_payload: str) -> bool:
    payload = (start_payload or "").strip()
    if not payload.lower().startswith("ref_"):
        return False
    current = UserTg.objects.select_for_update().get(pk=invitee.pk)
    if current.referred_by_id:
        invitee.referred_by_id = current.referred_by_id
        return False
    # A later referral link must not attribute an already connected user,
    # including disconnected users and legacy records without timestamps.
    if (
        current.business_is_connected
        or current.business_connected_at is not None
        or current.business_disconnected_at is not None
        or current.business_connection_id
    ):
        return False
    code = payload[4:]
    inviter = UserTg.objects.filter(referral_code=code).first()
    if inviter is None or inviter.pk == current.pk or inviter.user_id == current.user_id:
        return False
    invitee.referred_by = inviter
    invitee.save(update_fields=["referred_by"])
    return True


@transaction.atomic
def grant_referral_reward(invitee: UserTg, at=None) -> bool:
    at = at or timezone.now()
    # Lock only the invitee row. PostgreSQL cannot apply FOR UPDATE to the
    # nullable side of the LEFT JOIN produced by select_related("referred_by").
    invitee = UserTg.objects.select_for_update().get(pk=invitee.pk)
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

    from .metrics import REFERRAL_REWARDS, observe_metric
    observe_metric(REFERRAL_REWARDS, 1)

    from .telegram import tg_send_message

    referral_name = html.escape((invitee.first_name or "").strip() or "Пользователь")
    referral_username = (invitee.username or "").strip().lstrip("@")
    referral_identity = f"<b>{referral_name}</b>"
    if referral_username:
        referral_identity += f" (@{html.escape(referral_username)})"

    transaction.on_commit(
        lambda: tg_send_message(
            inviter.chat_id,
            "🎁 <b>Реферальный бонус начислен</b>\n\n"
            f"Ваш реферал {referral_identity} подключил WhoUpdate.\n"
            f"Доступ продлён на {REFERRAL_REWARD_DAYS} дней.",
        )
    )
    return True


def checkout_token(bot_user: UserTg, plan: str, config=None) -> str:
    payload = {"user_id": bot_user.user_id, "plan": plan}
    config = config if config is not None else plan_config_for_user(bot_user, plan)
    if config and config.get("reward_id"):
        payload["wheel_reward"] = config["reward_id"]
    return signing.dumps(
        payload,
        salt=CHECKOUT_SIGNING_SALT,
        compress=True,
    )


def checkout_url(bot_user: UserTg, plan: str, config=None) -> str:
    site_url = getattr(settings, "WHO_UPDATE_SITE_URL", "https://maksonchik.ru").rstrip("/")
    return f"{site_url}/bot/subscribe/{plan}/{checkout_token(bot_user, plan, config=config)}/"


def subscription_keyboard(bot_user: UserTg) -> dict:
    from .wheel import is_pilot, app_url, plan_text
    if is_pilot(bot_user):
        return {"inline_keyboard": [
            [{"text": plan_text(config, button=True), "url": checkout_url(bot_user, plan, config=config)}]
            for plan in PLAN_CONFIG for config in [plan_config_for_user(bot_user, plan)]
        ] + [[{"text": "🎡 Колесо бонусов", "web_app": {"url": app_url()}}]]}
    month = plan_config_for_user(bot_user, "month")
    three_months = plan_config_for_user(bot_user, "three_months")
    year = plan_config_for_user(bot_user, "year")
    return {
        "inline_keyboard": [
            [{"text": f"1 месяц — {_rubles(month['amount'])}", "url": checkout_url(bot_user, "month")}],
            [
                {
                    "text": f"3 месяца — {_rubles(three_months['amount'])}",
                    "url": checkout_url(bot_user, "three_months"),
                }
            ],
            [{"text": f"1 год — {_rubles(year['amount'])}", "url": checkout_url(bot_user, "year")}],
        ]
    }


def access_status_text(bot_user: UserTg, at=None) -> str:
    at = at or timezone.now()
    referral_section = ""
    if bot_user.access_unlimited:
        access_line = "Доступ: <b>бессрочный</b>"
    elif bot_user.has_active_access(at):
        expires = timezone.localtime(bot_user.access_expires_at)
        remaining = max((bot_user.access_expires_at - at).days + 1, 1)
        access_line = f"Доступ до: <b>{expires:%d.%m.%Y %H:%M}</b> МСК\nОсталось: <b>{remaining} дн.</b>"
    elif bot_user.trial_started_at is None:
        access_line = (
            f"Пробный период: <b>{TRIAL_DAYS} дней бесплатно</b>.\n"
            "Он начнётся после первого успешного подключения бота "
            "в разделе «Автоматизация чатов».\n\n"
            "Нажмите /start, чтобы открыть инструкцию подключения.\n"
        )
    else:
        access_line = "Доступ: <b>закончился</b>"
        referral_section = (
            f"\n\nЗа каждого друга, который впервые полностью подключит WhoUpdate "
            f"по вашей ссылке, вы получите <b>+{REFERRAL_REWARD_DAYS} дней доступа</b>.\n\n"
            "Ваша реферальная ссылка:\n"
            f"<code>{html.escape(referral_link(bot_user))}</code>"
        )
    return (
        "⏳ <b>Доступ к WhoUpdate</b>\n\n"
        f"{access_line}\n"
        f"Бонус за приглашённых: <b>{bot_user.referral_bonus_days} дн.</b>\n\n"
        "Получить дополнительный доступ можно оплатой или приглашением друга.\n\n"
        f"{purchase_promotion_text(bot_user)}"
        f"{referral_section}"
    )


def expired_access_text(bot_user: UserTg) -> str:
    link = referral_link(bot_user)
    return (
        "🚨 <b>Новые сообщения больше не сохраняются!</b>\n\n"
        "Доступ к WhoUpdate закончился. Если собеседник удалит новое сообщение сейчас, "
        "<b>бот не сможет его восстановить.</b>\n\n"
        "Продлить доступ можно одним из двух способов:\n\n"
        "💳 <b>Оплатить подписку</b>\n"
        f"{subscription_prices_text(bot_user)}.\n\n"
        f"{purchase_promotion_text(bot_user)}\n\n"
        f"👥 <b>Пригласить друга и получить {REFERRAL_REWARD_DAYS} дней</b>\n"
        "Бонус будет начислен, когда приглашённый пользователь полностью подключит бота.\n\n"
        f"Ваша реферальная ссылка:\n<code>{html.escape(link)}</code>\n\n"
        "Ссылка также доступна по команде /referral."
    )


def subscription_prices_text(bot_user):
    from .wheel import plan_text
    return ", ".join(plan_text(plan_config_for_user(bot_user, plan)) for plan in PLAN_CONFIG)


def purchase_promotion_text(bot_user):
    from .wheel import active_reward
    if active_reward(bot_user):
        return "🎁 <b>Твой бонус колеса</b>\n" + subscription_prices_text(bot_user) + "\nБонус на одну покупку; акции не суммируются."
    return PURCHASE_PROMOTION_TEXT


def notify_access_expired(bot_user: UserTg, at=None) -> bool:
    at = at or timezone.now()
    from .background_tasks import schedule_access_expiry_reminders

    with transaction.atomic():
        bot_user = UserTg.objects.select_for_update().filter(pk=bot_user.pk).first()
        if (
            bot_user is None
            or bot_user.has_active_access(at)
            or bot_user.trial_started_at is None
            or bot_user.access_expired_notified_at is not None
        ):
            return False

        # Очередь и отметка первого уведомления сохраняются вместе: сбой БД
        # не должен оставить пользователя без последующих напоминаний.
        schedule_access_expiry_reminders(bot_user, at=at)
        claimed = UserTg.objects.filter(
            pk=bot_user.pk,
            access_expired_notified_at__isnull=True,
        ).update(access_expired_notified_at=at)
        if not claimed:
            return False

    from .telegram import tg_send_message

    sent = tg_send_message(
        bot_user.chat_id,
        expired_access_text(bot_user),
        reply_markup=subscription_keyboard(bot_user),
    )
    if not sent:
        UserTg.objects.filter(pk=bot_user.pk, access_expired_notified_at=at).update(
            access_expired_notified_at=None
        )
    return sent


def business_owner_for_message(msg: dict) -> UserTg | None:
    connection_id = msg.get("business_connection_id")
    if not connection_id:
        return None
    return UserTg.objects.filter(business_connection_id=connection_id).first()


def business_access_allowed(msg: dict) -> bool:
    bot_user = business_owner_for_message(msg)
    if bot_user is None or bot_user.has_active_access():
        return True

    notify_access_expired(bot_user)
    return False
