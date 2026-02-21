from django.contrib import admin
from .models import Message, Chat, UserTg

admin.site.register(Message)
admin.site.register(Chat)
admin.site.register(UserTg)
