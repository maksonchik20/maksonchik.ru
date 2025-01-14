from django.shortcuts import render
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
import json
from .models import Messages

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
