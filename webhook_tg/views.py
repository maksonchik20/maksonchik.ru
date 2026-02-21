from django.shortcuts import render
from django.http import HttpResponse, JsonResponse, HttpRequest
from django.views.decorators.csrf import csrf_exempt
import json

# Create your views here.
@csrf_exempt
def index(request: HttpRequest):
    print(request.body)
    return HttpResponse(f"maksonchik website Xo-Xo: {request.body}")
