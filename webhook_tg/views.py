from django.shortcuts import render
from django.http import HttpResponse, HttpRequest
from django.views.decorators.csrf import csrf_exempt
import json
from .models import UserTg, Message
import html
from .inner_models.BusinessConnection import BusinessConnection
from .telegram import tg_send_message, get_business_connection

@csrf_exempt
def index(request: HttpRequest):
    return HttpResponse(f"maksonchik website Xo-Xo")

@csrf_exempt
def webhook_tg(request: HttpRequest):
    try:
        data = json.loads(request.body.decode("utf-8"))
        print(data)
        msg = data.get("business_message") or data.get("message") or data.get("edited_message") or \
              data.get("edited_business_message") or data.get("deleted_business_messages") or data.get("deleted_messages") or {}
        text = msg.get("text")
        from_user_id = msg.get("from", {}).get("id")
        chat_id = msg.get("chat", {}).get("id")
        username = msg.get("from", {}).get("username")
        if text == "/start" and is_message_to_bot(data):
            init_user_bot(user_id=from_user_id, chat_id=chat_id, username=username)
        elif is_edited_message(data):
            business_connection = get_business_connection(msg)
            if (business_connection.user_chat_id != chat_id):
                tg_send_message(chat_id=business_connection.user_chat_id, text=build_message_update(msg))
        elif is_deleted_message(data):
            business_connection = get_business_connection(msg)
            if (business_connection.user_chat_id != chat_id):
                tg_send_message(chat_id=business_connection.user_chat_id, text=build_message_delete(msg))
        elif is_edited_message(data) or is_new_message(data):
            create_message(msg)
        print(f"text: {text}")
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        print(f"Bad JSON: {e}")
        pass
    
    return HttpResponse(f"Success")

def create_message(msg):
    message_id = msg.get("message_id")
    try:
        m = Message.objects.get(message_id=message_id)
        m.text = msg.get("text")
        m.save(update_fields=["text"])
    except Message.DoesNotExist:
        m = Message.objects.create(
            message_id=message_id,
            text=msg.get("text"),
        )

def build_message_delete(deleted: dict) -> str:
    chat = deleted.get("chat") or {}
    first_name = chat.get("first_name") or "Unknown"
    username = chat.get("username")

    msg_ids = deleted.get("message_ids") or []
    if not msg_ids:
        return f"{html.escape(first_name)} удалил(а) сообщения (ids не пришли)."

    known = Message.objects.filter(message_id__in=msg_ids)
    known_map = {m.message_id: (m.text or "") for m in known}

    user_part = html.escape(first_name)
    if username:
        user_part += f" (@{html.escape(username)})"

    lines = [f"{user_part} удалил(а) {len(msg_ids)} сообщение(й):", ""]

    for mid in msg_ids[:20]:
        old_text = known_map.get(mid) or "(текст не сохранён)"
        lines.append(f"<blockquote>{html.escape(old_text)}</blockquote>")

    if len(msg_ids) > 20:
        lines.append(f"...и ещё {len(msg_ids) - 20} сообщений")

    lines.append(f"<b>@{html.escape('who_update_bot')}</b>")
    return "\n".join(lines)

def build_message_update(msg):
    fr = msg.get("from") or {}
    first_name = fr.get("first_name") or "Unknown"
    username = fr.get("username")

    message_id = msg.get("message_id")
    old = Message.objects.filter(message_id=message_id).first()
    old_text = old.text if old is not None else "(Это сообщение было написано до подключения бота)"
    new_text = msg.get("text") or ""

    user_part = html.escape(first_name)
    if username:
        user_part += f" (@{html.escape(username)})"

    return (
        f"{user_part} изменил(а) сообщение:\n\n"
        f"<b>Old:</b>\n<blockquote>{html.escape(old_text)}</blockquote>\n"
        f"<b>New:</b>\n<blockquote>{html.escape(new_text)}</blockquote>\n\n"
        f"<b>@{html.escape('who_update_bot')}</b>"
    )

def init_user_bot(user_id: int, chat_id: int, username: str):
    UserTg.objects.get_or_create(user_id=user_id, chat_id=chat_id, username=username)

def isBusiness(data):
    return data.get("business_message") is not None or data.get("edited_business_message") is not None

def is_message_to_bot(data):
    return data.get("message") is not None or data.get("edited_message") is not None

def is_edited_message(data):
    return data.get("edited_message") is not None or data.get("edited_business_message") is not None

def is_new_message(data):
    return data.get("message") is not None or data.get("business_message") is not None

def is_deleted_message(data):
    return data.get("deleted_business_messages") is not None or data.get("deleted_messages") is not None
