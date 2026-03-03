from django.contrib import admin
from .models import Message, UserTg, AdminChatFilter

HIDDEN_USERNAMES = {"@tamataeva86", }


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("username_from", "first_name", "text", "chat_id", "created_at")
    list_filter = ("username_from", "business_connection_id", "chat_id")
    search_fields = ("username_from", "first_name", "text", "message_id", "business_connection_id")
    ordering = ("-created_at",)
    list_per_page = 50

    readonly_fields = ("created_at",)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        qs = qs.exclude(username_from__in=HIDDEN_USERNAMES)
        if request.user.is_superuser:
            return qs
        try:
            flt = request.user.admin_chat_filter
            return qs.filter(chat_id=flt.chat_id)
        except AdminChatFilter.DoesNotExist:
            return qs.none()


@admin.register(AdminChatFilter)
class AdminChatFilterAdmin(admin.ModelAdmin):
    list_display = ("user", "chat_id")
    list_filter = ("chat_id",)
    search_fields = ("user__username",)
    autocomplete_fields = ("user",)


admin.site.register(UserTg)

admin.site.site_header = "WhoUpdateBot Admin"
admin.site.site_title = "WhoUpdateBot"
admin.site.index_title = "Панель управления"