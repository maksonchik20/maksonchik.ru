from django.shortcuts import render
from django.http import HttpResponse, JsonResponse, HttpRequest, HttpResponseBadRequest
from django.views.decorators.csrf import csrf_exempt
import json
from .models import UserTg, Chat, Message
import requests
from env import TOKEN_BOT
import html

api_tg_url = f"https://api.telegram.org/bot{TOKEN_BOT}/"

@csrf_exempt
def index(request: HttpRequest):
    return HttpResponse(f"maksonchik website Xo-Xo")

@csrf_exempt
def webhook_tg(request: HttpRequest):
    try:
        data = json.loads(request.body.decode("utf-8"))
        print(data)
        msg = data.get("business_message") or data.get("message") or data.get("edited_message") or data.get("edited_business_message") or {}
        text = msg.get("text", "")
        from_user_id = msg.get("from").get("id")
        chat_id = msg.get("chat").get("id")
        if text == "/start" and is_message_bot(data):
            init_user_bot(user_id=from_user_id, chat_id=chat_id)
        elif is_edited_message(data):
            whom_send_chat_id = get_whom_send(msg)
            send_msg(chat_id=whom_send_chat_id, message=build_message_update(msg))
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
        f"<b>Old:</b>\n<pre>{html.escape(old_text)}</pre>\n"
        f"<b>New:</b>\n<pre>{html.escape(new_text)}</pre>\n"
        f"<b> @{html.escape("who_update_bot")}"
    )

def send_msg(chat_id, message):
    method = "sendMessage"
    url = api_tg_url + method
    body = {}
    body['chat_id'] = chat_id
    body['text'] = message
    body['parse_mode'] = "HTML"
    body['disable_web_page_preview'] = True

    requests.post(url=url, json=body)

def init_user_bot(user_id: int, chat_id: int):
    UserTg.objects.get_or_create(user_id=user_id, chat_id=chat_id)

def isBusiness(data):
    return data.get("business_message") is not None or data.get("edited_business_message") is not None

def is_message_bot(data):
    return data.get("message") is not None or data.get("edited_message") is not None

def is_edited_message(data):
    return data.get("edited_message") is not None or data.get("edited_business_message") is not None

def save_chat(data):
    from_id = data.get("from").get("id")
    to_id = data.get("to").get("id")
    chat_id = data.get("chat_id").get("id")
    Chat.objects.get_or_create(chat_id=chat_id, user1=from_id, user2=to_id)

def get_whom_send(msg):
    url = api_tg_url + "getBusinessConnection"
    body = {}
    body['business_connection_id'] = msg.get("business_connection_id")
    ans = requests.post(url, json=body).json()
    user_id = ans.get("result").get("user_chat_id")
    return user_id

