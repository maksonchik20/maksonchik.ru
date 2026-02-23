from django.contrib import admin
from .models import Message, UserTg

@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("username_from", "first_name", "text", "created_at")
    list_filter = ("username_from", )
    search_fields = ("username_from", "first_name", "text", "message_id")
    ordering = ("-created_at",)
    list_per_page = 50

    readonly_fields = ("created_at",)

admin.site.register(UserTg)

admin.site.site_header = "WhoUpdateBot Admin"
admin.site.site_title = "WhoUpdateBot"
admin.site.index_title = "Панель управления"