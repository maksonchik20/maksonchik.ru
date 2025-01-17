from django.contrib import admin
from django.urls import path, include
from . import views
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path("", views.index, name="index"),
    path("send/", views.send_message, name="send"),
    path("get/", views.get_messages, name="get"),
    path("send_tg/", views.send_tg, name="send_tg"),
]
