"""Opt-in onboarding check. Progress requires confirmed notification delivery."""
import uuid
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import TelegramOutbox, UserTg, WhoUpdateConnectionCheck
from .outbox import enqueue_outbox, send_message_reliably
from .telegram import answer_callback_query


CHECK_CALLBACK = "who_check:start"
CHECK_STATUS_CALLBACK = "who_check:status"
CHECK_KEYBOARD = {"inline_keyboard": [[{"text": "✅ Проверить работу", "callback_data": CHECK_CALLBACK}]]}
PROGRESS_KEYBOARD = {"inline_keyboard": [[{"text": "Показать прогресс", "callback_data": CHECK_STATUS_CALLBACK}]]}
CHECK_PREFIX = "Проверка WhoUpdate "


def check_message(check):
    return CHECK_PREFIX + check.run_id.hex[:8].upper()


def _text(check):
    if check.edit_delivered_at and check.delete_delivered_at:
        return "✅ <b>Проверка пройдена!</b>\n\nКопии изменения и удаления доставлены в этот чат. WhoUpdate работает."
    if check.expires_at <= timezone.now():
        return "Время проверки истекло. Нажмите «Проверить работу», чтобы начать заново."
    progress = [
        f"{'✅' if check.received_at else '⬜'} Новое сообщение сохранено",
        f"{'✅' if check.edit_delivered_at else '⬜'} Копия изменения доставлена",
        f"{'✅' if check.delete_delivered_at else '⬜'} Копия удаления доставлена",
    ]
    return (
        "<b>Проверим WhoUpdate за минуту</b>\n\n"
        "1. Со второго аккаунта или с помощью знакомого, который согласен на проверку, "
        "отправьте себе <b>новое</b> сообщение с текстом:\n"
        f"<code>{check_message(check)}</code>\n\n"
        "2. После подтверждения сохранения пусть отправитель <b>изменит это сообщение</b>.\n"
        "3. Когда получите копию правки, пусть отправитель <b>удалит его у обоих</b>.\n\n"
        + "\n".join(progress)
        + "\n\nПроверка доступна 15 минут. Бот сам не пишет вашим собеседникам. "
        "Он сохраняет только сообщения, полученные после подключения в разрешённых чатах. "
        "Если нет подтверждения, проверьте доступ к выбранному чату и /status."
    )


def handle_check_callback(callback, *, update_id):
    action = callback.get("data")
    if action not in (CHECK_CALLBACK, CHECK_STATUS_CALLBACK):
        return False
    chat_id = ((callback.get("message") or {}).get("chat") or {}).get("id")
    user_id = (callback.get("from") or {}).get("id")
    if not chat_id or chat_id != user_id:
        answer_callback_query(callback.get("id"), "Откройте личный чат с ботом")
        return True
    user = UserTg.objects.filter(user_id=user_id, chat_id=chat_id).first()
    if user is None or not user.business_is_connected or not user.business_connection_id:
        text = "Сначала подключите WhoUpdate к автоматизации чатов, затем повторите проверку."
        markup = CHECK_KEYBOARD
    elif not user.has_active_access():
        text = "Доступ к WhoUpdate закончился. Проверьте /status и продлите доступ перед проверкой."
        markup = CHECK_KEYBOARD
    else:
        now = timezone.now()
        with transaction.atomic():
            user = UserTg.objects.select_for_update().get(pk=user.pk)
            check = WhoUpdateConnectionCheck.objects.filter(user=user).first()
            if check is None:
                check = WhoUpdateConnectionCheck.objects.create(
                    user=user, start_update_id=update_id, started_at=now, expires_at=now + timedelta(minutes=15),
                )
            elif (
                action == CHECK_CALLBACK and check.start_update_id != update_id
                and (check.expires_at <= now or (check.edit_delivered_at and check.delete_delivered_at))
            ):
                check.run_id = uuid.uuid4()
                check.start_update_id = update_id
                check.started_at = now
                check.expires_at = now + timedelta(minutes=15)
                check.peer_chat_id = check.peer_message_id = None
                check.received_at = check.edit_delivered_at = check.delete_delivered_at = None
                check.save()
            text = _text(check)
            markup = PROGRESS_KEYBOARD if check.expires_at > now and not (check.edit_delivered_at and check.delete_delivered_at) else CHECK_KEYBOARD
    # Save the answer before best-effort dismissal of Telegram's spinner.
    send_message_reliably(chat_id, text, idempotency_key=f"command:{update_id}:connection-check", reply_markup=markup)
    answer_callback_query(callback.get("id"), "Проверка WhoUpdate")
    return True


def observe_check_message(msg):
    if not str(msg.get("text") or "").startswith(CHECK_PREFIX):
        return
    now = timezone.now()
    with transaction.atomic():
        check = (WhoUpdateConnectionCheck.objects.select_for_update()
                 .filter(user__business_connection_id=msg.get("business_connection_id"),
                         user__business_is_connected=True, received_at__isnull=True, expires_at__gt=now)
                 .select_related("user").first())
        if (
            check is None or msg.get("text") != check_message(check)
            or (msg.get("from") or {}).get("id") == check.user.user_id
            or (msg.get("chat") or {}).get("id") == check.user.chat_id
        ):
            return
        check.peer_chat_id = msg["chat"]["id"]
        check.peer_message_id = msg["message_id"]
        check.received_at = now
        check.save(update_fields=["peer_chat_id", "peer_message_id", "received_at"])
        enqueue_outbox(
            chat_id=check.user.chat_id, method=TelegramOutbox.Method.SEND_MESSAGE,
            payload={"text": "✅ Новое сообщение сохранено. Теперь пусть отправитель изменит его; дождитесь копии правки и удалите сообщение у обоих.", "reply_markup": PROGRESS_KEYBOARD},
            idempotency_key=f"check:{check.run_id}:received",
        )


def delivery_context(msg, *, event_type, has_original_text):
    contextFieldToValue = {"event_type": event_type, "has_original_text": bool(has_original_text)}
    if not has_original_text:
        return contextFieldToValue
    check = WhoUpdateConnectionCheck.objects.filter(
        user__business_connection_id=msg.get("business_connection_id"),
        peer_chat_id=(msg.get("chat") or {}).get("id"), peer_message_id=msg.get("message_id"),
        received_at__isnull=False, expires_at__gt=timezone.now(),
    ).first()
    if check:
        contextFieldToValue["check_run_id"] = str(check.run_id)
    return contextFieldToValue


def record_check_delivery(item, *, sent_at):
    contextFieldToValue = item.delivery_context or {}
    if not contextFieldToValue.get("check_run_id") or contextFieldToValue.get("event_type") not in ("edit", "delete_text"):
        return
    field = "edit_delivered_at" if contextFieldToValue["event_type"] == "edit" else "delete_delivered_at"
    with transaction.atomic():
        check = (WhoUpdateConnectionCheck.objects.select_for_update()
                 .filter(run_id=contextFieldToValue["check_run_id"], user__chat_id=item.chat_id,
                         expires_at__gt=sent_at, received_at__isnull=False).first())
        if check is None or getattr(check, field) is not None:
            return
        setattr(check, field, sent_at)
        check.save(update_fields=[field])
        complete = bool(check.edit_delivered_at and check.delete_delivered_at)
        text = _text(check) if complete else "✅ Копия изменения доставлена. Теперь удалите проверочное сообщение у обоих." if field == "edit_delivered_at" else "✅ Копия удаления доставлена. Для завершения ещё нужна доставленная копия изменения."
        enqueue_outbox(
            chat_id=item.chat_id, method=TelegramOutbox.Method.SEND_MESSAGE,
            payload={"text": text, "parse_mode": "HTML", "reply_markup": CHECK_KEYBOARD if complete else PROGRESS_KEYBOARD},
            idempotency_key=f"check:{check.run_id}:{field}",
        )
