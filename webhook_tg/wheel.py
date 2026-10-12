"""Daily wheel for registered private-chat users; rewards are server-authoritative."""
import hashlib
import hmac
import json
import secrets
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import parse_qsl

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import UserTg, WhoUpdatePaymentOrder, WhoUpdateWheelReward

PILOT_USER_ID = 1394340082
PRIZES = (
    {"id": "discount10", "label": "−10%", "discount": 10},
    {"id": "discount15", "label": "−15%", "discount": 15},
    {"id": "discount20", "label": "−20%", "discount": 20},
    {"id": "days3", "label": "+3 дня", "extra_days": 3},
    {"id": "days7", "label": "+7 дней", "extra_days": 7},
    {"id": "double", "label": "×2 срока", "multiplier": 2},
)
PRIZE_WEIGHTS = (35, 25, 15, 10, 5, 10)
MOSCOW = ZoneInfo("Europe/Moscow")
INSTANT_DAY_PRIZES = {"days3": 3, "days7": 7}


def spin_day(at=None):
    return (at or timezone.now()).astimezone(MOSCOW).date()


def latest_reward(user):
    if not is_pilot(user):
        return None
    return WhoUpdateWheelReward.objects.select_related("reserved_order").filter(user=user).order_by("-spin_day", "-pk").first()


def spin_availability(reward, at=None):
    at = at or timezone.now()
    available = reward is None or reward.spin_reset_at is not None or reward.spin_day < spin_day(at)
    next_at = at if available else datetime.combine(reward.spin_day + timedelta(days=1), time.min, MOSCOW)
    return available, next_at


def choose_prize():
    ticket = secrets.randbelow(sum(PRIZE_WEIGHTS))
    for prize, weight in zip(PRIZES, PRIZE_WEIGHTS, strict=True):
        if ticket < weight:
            return prize
        ticket -= weight
    raise AssertionError("Invalid prize weights")


def is_pilot(user):
    # Historical import name retained for payment workers during rolling upgrades.
    return bool(user and type(user.user_id) is int and user.user_id > 0 and user.chat_id == user.user_id)


def app_url():
    return settings.WHO_UPDATE_SITE_URL.rstrip("/") + "/bot/wheel/"


def authenticate(init_data):
    from env import TOKEN_BOT

    try:
        if not init_data or len(init_data) > 8192:
            return None
        pairs = parse_qsl(init_data, strict_parsing=True, max_num_fields=30)
        data = dict(pairs)
        if len(data) != len(pairs):
            return None
        supplied = data.pop("hash")
        check = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
        secret = hmac.new(b"WebAppData", str(TOKEN_BOT).encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, supplied):
            return None
        age = timezone.now().timestamp() - int(data["auth_date"])
        if age < -30 or age > 3600:
            return None
        user = json.loads(data["user"])
        if not isinstance(user, dict) or type(user.get("id")) is not int or user["id"] <= 0:
            return None
        return UserTg.objects.filter(user_id=user["id"], chat_id=user["id"]).first()
    except (ValueError, KeyError, TypeError, UnicodeError):
        return None


def active_reward(user):
    # Group chats and invalid identities must never enter this feature.
    if not is_pilot(user):
        return None
    reward = latest_reward(user)
    if reward and reward.prize in INSTANT_DAY_PRIZES:
        return None
    if reward and not reward.used_at and (reward.expires_at > timezone.now() or (
        reward.reserved_order_id and reward.reserved_order.status == WhoUpdatePaymentOrder.Status.PENDING
    )):
        return reward
    return None


def reward_config(config, reward):
    config = dict(config)
    base_days = config["days"]
    if reward.prize in INSTANT_DAY_PRIZES:
        return config
    prize = next(p for p in PRIZES if p["id"] == reward.prize)
    config.update(original_amount=config["amount"], original_label=config["label"], reward_id=reward.pk)
    if prize.get("discount"):
        config["amount"] = (config["amount"] * Decimal(100 - prize["discount"]) / 100).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP,
        )
    else:
        config["days"] = config["days"] * prize.get("multiplier", 1) + prize.get("extra_days", 0)
        if prize.get("multiplier"):
            config["label"] = {30: "2 месяца", 90: "6 месяцев", 365: "2 года"}[config["days"] // 2]
        else:
            config["label"] = f"{config['days']} дн."
    if reward.reserved_order_id and reward.reserved_order.status == WhoUpdatePaymentOrder.Status.PENDING:
        from .subscriptions import PLAN_CONFIG
        order = reward.reserved_order
        if PLAN_CONFIG[order.plan]["days"] == base_days:
            # A price change must not misquote an already reserved payment.
            config["amount"] = order.amount
            config["days"] = order.duration_days
    return config


def money(amount):
    return f"{amount:.2f}".rstrip("0").rstrip(".").replace(".", ",") + " ₽"


def plan_text(config, *, button=False):
    def old(value):
        return "".join(c + "\u0336" if not c.isspace() else c for c in value) if button else f"<s>{value}</s>"

    label, price = config["label"], money(config["amount"])
    if config.get("original_label", label) != label:
        label = old(config["original_label"]) + " " + label
    if config.get("original_amount", config["amount"]) != config["amount"]:
        price = old(money(config["original_amount"])) + " " + price
    return f"{label} — {price}"


def order_text(order):
    if not is_pilot(order.user):
        return ""
    from .subscriptions import PLAN_CONFIG
    config = dict(PLAN_CONFIG[order.plan])
    if order.amount == config["amount"] and order.duration_days == config["days"]:
        return ""
    config.update(original_label=config["label"], original_amount=config["amount"], amount=order.amount)
    if order.duration_days != config["days"]:
        config["label"] = (
            {30: "2 месяца", 90: "6 месяцев", 365: "2 года"}[config["days"]]
            if order.duration_days == 2 * config["days"] else f"{order.duration_days} дн."
        )
    return plan_text(config)


@transaction.atomic
def grant_instant_days(reward, at=None):
    """Grant a free-days prize once, in the same transaction as its receipt."""
    user = UserTg.objects.select_for_update().get(pk=reward.user_id)
    if not is_pilot(user):
        return False
    reward = WhoUpdateWheelReward.objects.select_for_update().get(pk=reward.pk)
    days = INSTANT_DAY_PRIZES.get(reward.prize)
    # Legacy orders retain their signed terms and must never be granted twice.
    if not days or reward.used_at or reward.granted_days or reward.reserved_order_id:
        return False
    now = at or timezone.now()
    if not user.access_unlimited:
        user.extend_access(days, at=now)
    reward.granted_days = days
    reward.used_at = now
    reward.access_expires_at_after = user.access_expires_at
    reward.save(update_fields=["granted_days", "used_at", "access_expires_at_after"])
    from .models import TelegramOutbox
    from .outbox import enqueue_outbox
    label = next(p["label"] for p in PRIZES if p["id"] == reward.prize)
    access_text = "Твой бессрочный доступ сохранён." if user.access_unlimited else (
        f"Доступ продлён до <b>{timezone.localtime(user.access_expires_at, MOSCOW):%d.%m.%Y %H:%M}</b> МСК."
    )
    enqueue_outbox(
        chat_id=user.chat_id, method=TelegramOutbox.Method.SEND_MESSAGE,
        idempotency_key=f"wheel-free-days:{reward.pk}",
        payload={
            "text": f"🎉 <b>Твой приз: {label}</b>\n\n🎁 Дни начислены бесплатно!\n{access_text}\n\n"
                "Ничего оплачивать не нужно. Следующее вращение — после 00:00 МСК.",
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": [[{"text": "🎡 Колесо бонусов", "web_app": {"url": app_url()}}]]},
        },
    )
    return True


@transaction.atomic
def reset_spin(reward):
    """Release one daily limit without rewriting history or payment snapshots."""
    user = UserTg.objects.select_for_update().get(pk=reward.user_id)
    if not is_pilot(user):
        return False
    latest = latest_reward(user)
    now = timezone.now()
    if not latest or latest.pk != reward.pk or latest.spin_day != spin_day(now) or latest.spin_reset_at:
        return False
    latest.spin_reset_at = now
    latest.save(update_fields=["spin_reset_at"])
    return True


@transaction.atomic
def spin(user):
    if not is_pilot(user):
        raise PermissionError("Registered private chat required")
    user = UserTg.objects.select_for_update().get(pk=user.pk)
    now = timezone.now()
    day = spin_day(now)
    existing = WhoUpdateWheelReward.objects.filter(user=user, spin_day=day, spin_reset_at=None).first()
    if existing:
        return existing
    reward = WhoUpdateWheelReward.objects.create(
        user=user, spin_day=day, prize=choose_prize()["id"], expires_at=now + timezone.timedelta(hours=24),
    )
    if reward.prize in INSTANT_DAY_PRIZES:
        grant_instant_days(reward, at=now)
        reward.refresh_from_db()
        return reward
    from .models import TelegramOutbox
    from .outbox import enqueue_outbox
    from .subscriptions import subscription_keyboard, PLAN_CONFIG
    prize = next(p for p in PRIZES if p["id"] == reward.prize)
    enqueue_outbox(
        chat_id=user.chat_id, method=TelegramOutbox.Method.SEND_MESSAGE,
        idempotency_key=f"wheel-reward:{reward.pk}",
        payload={
            "text": f"🎉 <b>Твой приз: {prize['label']}</b>\n\n" +
                "\n".join(plan_text(reward_config(c, reward)) for c in PLAN_CONFIG.values()) +
                "\n\nБонус на одну покупку. Выбери тариф в течение 24 часов. Другие акции не суммируются. "
                "Новое вращение — завтра после 00:00 МСК. Новый приз заменит неиспользованный бонус.",
            "parse_mode": "HTML", "reply_markup": subscription_keyboard(user),
        },
    )
    return reward
