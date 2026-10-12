"""Private-chat withdrawal dialog, with durable drafts and atomic reservations."""
import re
import uuid
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import UserTg, TelegramOutbox, WhoUpdateWithdrawalDraft as Draft, WhoUpdatePartnerWithdrawal as Withdrawal
from .outbox import enqueue_outbox
from .partners import partner_balance, partner_percent, create_withdrawal
from .subscriptions import REFERRAL_REWARD_DAYS, referral_link
from .telegram import answer_callback_query


def rub(amount):
    return f"{amount:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def reply(user, text, key, buttons=None):
    payload = {"text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    return enqueue_outbox(chat_id=user.chat_id, method=TelegramOutbox.Method.SEND_MESSAGE,
                          payload=payload, idempotency_key=key)


def button(text, action, token):
    return {"text": text, "callback_data": f"money:{action}:{token.hex}"}


def referral_text(user):
    balance = partner_balance(user)
    rate = partner_percent(user)
    rules = (f"💰 <b>+{rub(rate)}% от каждой покупки реферала</b> — в том числе повторной. "
             "Процент считается от фактически оплаченной суммы и накапливается в рублях на вашем балансе."
             if rate else "Начисление денежных вознаграждений сейчас приостановлено. Накопленные средства доступны для вывода.")
    return ("👥 <b>Приглашайте друзей и получайте бонусы</b>\n\n"
            f"🎁 <b>+{REFERRAL_REWARD_DAYS} дней доступа</b> — когда приглашённый друг впервые подключит WhoUpdate.\n\n" + rules +
            "\n\nДруг должен перейти по вашей ссылке до первого подключения бота. "
            "Уже подключавшиеся пользователи не могут стать рефералами. "
            "Денежные начисления действуют с запуска программы; прежние покупки не пересчитываются."
            f"\n\nВаша ссылка:\n{referral_link(user)}"
            f"\n\nНачислено: <b>{rub(balance['earned'])} ₽</b>"
            f"\nДоступно к выводу: <b>{rub(balance['payable'])} ₽</b>"
            f"\nВ заявках: {rub(balance['reserved'])} ₽"
            f"\nВыплачено: {rub(balance['paid'])} ₽"
            "\n\nНажмите кнопку ниже, введите сумму и подтвердите заявку. После подтверждения сумма будет зарезервирована. "
            "Заявку обработает администратор вручную и свяжется с вами для перевода.")


def is_private(msg):
    chat, sender = msg.get("chat") or {}, msg.get("from") or {}
    return (not msg.get("business_connection_id") and chat.get("type") == "private"
            and type(chat.get("id")) is int and chat["id"] > 0 and chat["id"] == sender.get("id"))


@transaction.atomic
def handle_money_message(data):
    msg = data.get("message") or {}
    if not is_private(msg):
        return False
    text = (msg.get("text") or "").strip()
    command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
    key = f"money:message:{data['update_id']}"
    if TelegramOutbox.objects.filter(idempotency_key=key).exists():
        return True
    if command in ("/referral", "/ref"):
        from .views import _init_user_bot
        sender = msg["from"]
        user, _ = _init_user_bot(sender["id"], msg["chat"]["id"], sender.get("username"), sender.get("first_name"), notify_owner=False)
        UserTg.objects.select_for_update().get(pk=user.pk)
        draft, _ = Draft.objects.update_or_create(partner=user, defaults={"token": uuid.uuid4(), "amount": None, "active": False, "updated_at": timezone.now()})
        reply(user, referral_text(user), key, [[button("Создать заявку на вывод", "begin", draft.token)]])
        return True
    user = UserTg.objects.filter(user_id=msg["from"]["id"], chat_id=msg["chat"]["id"]).order_by("pk").first()
    if not user or not Draft.objects.filter(partner=user, active=True).exists():
        return False
    UserTg.objects.select_for_update().get(pk=user.pk)
    draft = Draft.objects.get(partner=user)
    if not draft.active:
        return False
    if command or draft.updated_at < timezone.now() - timedelta(minutes=30):
        draft.active = False
        draft.save(update_fields=["active"])
        if command == "/cancel" or not command:
            reply(user, "Создание заявки отменено. Начать заново: /referral", key)
            return True
        return False
    if not re.fullmatch(r"[0-9]{1,10}(?:[.,][0-9]{1,2})?", text):
        reply(user, "Введите сумму числом в рублях, например 100 или 100,50. Для отмены: /cancel", key)
        return True
    amount = Decimal(text.replace(",", "."))
    available = partner_balance(user)["payable"]
    if amount <= 0 or amount > available:
        reply(user, f"Введите сумму больше нуля и не больше доступного остатка: <b>{rub(available)} ₽</b>. Для отмены: /cancel", key)
        return True
    draft.amount, draft.token, draft.updated_at = amount, uuid.uuid4(), timezone.now()
    draft.save(update_fields=["amount", "token", "updated_at"])
    reply(user, f"Правда ли, что вы хотите создать заявку на вывод <b>{rub(amount)} рублей</b>?", key,
          [[button("Да", "yes", draft.token), button("Отменить", "cancel", draft.token)]])
    return True


def handle_money_callback(callback):
    data = str(callback.get("data") or "")
    if not data.startswith("money:"):
        return False
    # A copied/forwarded keyboard may only act on its recipient's own draft.
    msg = {**(callback.get("message") or {}), "from": callback.get("from") or {}}
    if not is_private(msg):
        answer_callback_query(callback.get("id"), "Откройте /referral в личном чате с ботом")
        return True
    with transaction.atomic():
        user = UserTg.objects.select_for_update().filter(user_id=msg["from"]["id"], chat_id=msg["chat"]["id"]).order_by("pk").first()
        if user:
            _handle_callback_locked(user, callback)
    answer_callback_query(callback.get("id"))
    return True


def _handle_callback_locked(user, callback):
    key = f"money:callback:{callback['id']}"
    if TelegramOutbox.objects.filter(idempotency_key=key).exists():
        return
    try:
        _, action, value = callback["data"].split(":")
        token = uuid.UUID(value)
    except (ValueError, TypeError):
        return
    existing = Withdrawal.objects.filter(partner=user, request_key=token).first()
    if existing:
        reply(user, f"Заявка №{existing.pk} на {rub(existing.amount)} ₽ уже создана. Статус: {existing.get_status_display()}. Баланс: /referral", key)
        return
    draft = Draft.objects.filter(partner=user, token=token).first()
    if not draft or draft.updated_at < timezone.now() - timedelta(minutes=30):
        reply(user, "Эта кнопка устарела. Начать заново: /referral", key)
        return
    if action == "begin" and draft.amount is None:
        available = partner_balance(user)["payable"]
        if available <= 0:
            reply(user, "Пока нет средств для вывода. Приглашайте пользователей по своей ссылке из /referral.", key)
            return
        draft.active, draft.updated_at = True, timezone.now()
        draft.save(update_fields=["active", "updated_at"])
        reply(user, f"Какую сумму хотите вывести? Доступно <b>{rub(available)} ₽</b>.\nВведите число в рублях. Для отмены: /cancel", key)
    elif action == "cancel" and draft.active:
        draft.active = False
        draft.save(update_fields=["active"])
        reply(user, "Создание заявки отменено. Деньги остались на балансе. /referral", key)
    elif action == "yes" and draft.active and draft.amount is not None:
        try:
            withdrawal = create_withdrawal(user, draft.amount, request_key=draft.token)
        except ValueError as exc:
            reply(user, str(exc) + " Начать заново: /referral", key)
        else:
            reply(user, f"✅ Заявка №{withdrawal.pk} на вывод <b>{rub(withdrawal.amount)} ₽</b> создана.\n\nСумма зарезервирована. Администратор обработает заявку и свяжется с вами для перевода. Баланс: /referral", key)
        draft.active = False
        draft.save(update_fields=["active"])
    else:
        reply(user, "Эта заявка уже закрыта. Начать заново: /referral", key)
