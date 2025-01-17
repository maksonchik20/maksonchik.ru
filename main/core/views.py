from django.shortcuts import render
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
import json
from .models import Messages
from env import token, vlad, my, isaev
import telebot

bot = telebot.TeleBot(token)
names = {"vlad": vlad, "my": my, "isaev": isaev}

def send_bot_tg(id, text):
    bot.send_message(id, text)

def index(request):
    return HttpResponse("Hello, World!")

@csrf_exempt
def send_message(request):
    if request.method != "POST":
        return HttpResponse("Send POST request")
    body = dict(json.loads(request.body))
    if body.get("message", None) is not None and isinstance(body["message"], str):
        message = body["message"]
        Messages.objects.create(text=message)
        return HttpResponse("Your message delivered")
    else:
        return HttpResponse("Pass the string data type in the 'message' field")

@csrf_exempt
def get_messages(request):
    if request.method != "POST":
        return HttpResponse("Send POST request")
    messages = list(Messages.objects.all().values_list("text"))
    for i in range(len(messages)):
        messages[i] = messages[i][0]
    print(messages)
    data = {"Messages": messages}
    return JsonResponse(data)


@csrf_exempt
def send_tg(request):
    if request.method != "POST":
        return HttpResponse("Send POST request")
    body = dict(json.loads(request.body))
    if body.get("message", None) is None:
        return HttpResponse("Pass the string data type in the 'message' field")
    if not isinstance(body["message"], str):
        return HttpResponse("Pass the string data type in the 'message' field")
    if body.get("name", None) is None:
        return HttpResponse("Pass the string data type in the 'name' field")
    if not isinstance(body["name"], str):
        return HttpResponse("Pass the string data type in the 'message' field")
    message = body["message"]
    name = body["name"]
    if names.get(name, None) is None:
        return HttpResponse("name not found")
    try:
        send_bot_tg(names[name], message)
        return HttpResponse("Your message delivered")
    except Exception as ex:
        return HttpResponse(f"Error: {ex}")
