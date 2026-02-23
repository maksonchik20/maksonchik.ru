from django.contrib import admin
from .models import Message, UserTg

HIDDEN_USERNAMES = {"@tamataeva86", }

@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("username_from", "first_name", "text", "created_at")
    list_filter = ("username_from", "business_connection_id")
    search_fields = ("username_from", "first_name", "text", "message_id", "business_connection_id")
    ordering = ("-created_at",)
    list_per_page = 50

    readonly_fields = ("created_at",)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.exclude(username_from__in=HIDDEN_USERNAMES)

admin.site.register(UserTg)

admin.site.site_header = "WhoUpdateBot Admin"
admin.site.site_title = "WhoUpdateBot"
admin.site.index_title = "Панель управления"