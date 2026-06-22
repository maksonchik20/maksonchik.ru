from django.contrib import admin
from django.db.models import Q
from .models import Message, UserTg, AdminChatFilter, TelegramOutbox

HIDDEN_USERNAMES = {"@tamataeva86", }


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("username_from", "first_name", "text", "file_type", "file_id", "chat_id", "created_at")
    list_filter = ("username_from", "business_connection_id", "chat_id")
    search_fields = ("username_from", "first_name", "text", "message_id", "business_connection_id")
    ordering = ("-created_at",)
    list_per_page = 50

    readonly_fields = ("created_at", )


    def get_queryset(self, request):
        qs = super().get_queryset(request)
        qs = qs.exclude(username_from__in=HIDDEN_USERNAMES)
        if request.user.is_superuser:
            return qs
        filters = request.user.admin_chat_filters.all()
        if not filters:
            return qs.none()
        q = Q()
        for f in filters:
            if f.business_connection_id:
                q |= Q(chat_id=f.chat_id, business_connection_id=f.business_connection_id)
            else:
                q |= Q(chat_id=f.chat_id)
        return qs.filter(q)


@admin.register(AdminChatFilter)
class AdminChatFilterAdmin(admin.ModelAdmin):
    list_display = ("user", "chat_id", "business_connection_id")
    list_filter = ("chat_id", "business_connection_id")
    search_fields = ("user__username", "business_connection_id")
    autocomplete_fields = ("user",)


admin.site.register(UserTg)


@admin.register(TelegramOutbox)
class TelegramOutboxAdmin(admin.ModelAdmin):
    list_display = ("id", "method", "chat_id", "attempts", "next_attempt_at", "created_at", "dedup_key")
    list_filter = ("method",)
    search_fields = ("chat_id", "dedup_key", "last_error")
    ordering = ("next_attempt_at",)
    readonly_fields = ("created_at", "payload", "last_error")

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


admin.site.site_header = "WhoUpdateBot Admin"
admin.site.site_title = "WhoUpdateBot"
admin.site.index_title = "Панель управления"