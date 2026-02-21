from django.shortcuts import render
from django.http import HttpResponse, JsonResponse, HttpRequest, HttpResponseBadRequest
from django.views.decorators.csrf import csrf_exempt
import json

@csrf_exempt
def index(request: HttpRequest):
    try:
        raw = request.body.decode("utf-8")
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        print(f"Bad JSON: {e}")
        pass

    msg = data.get("business_message") or data.get("message") or {}
    text = msg.get("text", "")
    print(data)
    print(f"text: {text}")
    
    return HttpResponse(f"maksonchik website Xo-Xo")
