from urllib.parse import quote

from django.contrib import admin
from django.db.models import Q
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from .chat_display import format_message_html
from .models import Message, UserTg, AdminChatFilter, TelegramOutbox, BotOutgoingMessage

HIDDEN_USERNAMES = {"@tamataeva86", }


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    change_list_template = "admin/webhook_tg/message/change_list.html"
    list_display = ("chat_link", "username_from", "first_name", "text_preview", "file_type", "created_at")
    list_filter = ("username_from", "business_connection_id", "chat_id")
    search_fields = ("username_from", "first_name", "text", "message_id", "business_connection_id")
    ordering = ("-created_at",)
    list_per_page = 50

    readonly_fields = ("created_at",)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        qs = qs.exclude(username_from__in=HIDDEN_USERNAMES)
        if request.user.is_superuser:
            filtered = qs
        else:
            filters = request.user.admin_chat_filters.all()
            if not filters:
                return qs.none()
            q = Q()
            for f in filters:
                if f.business_connection_id:
                    q |= Q(chat_id=f.chat_id, business_connection_id=f.business_connection_id)
                else:
                    q |= Q(chat_id=f.chat_id)
            filtered = qs.filter(q)

        chat_id = (request.GET.get("chat_id") or "").strip()
        if chat_id:
            filtered = filtered.filter(chat_id=chat_id)
            conn_id = (request.GET.get("business_connection_id") or "").strip()
            if conn_id:
                filtered = filtered.filter(business_connection_id=conn_id)

        return filtered

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        chat_id = (request.GET.get("chat_id") or "").strip()
        conn_id = (request.GET.get("business_connection_id") or "").strip()
        extra_context["wu_chat_mode"] = bool(chat_id)

        if chat_id:
            messages = list(self.get_queryset(request).order_by("-created_at"))
            extra_context["wu_chat_count"] = len(messages)

            title_parts = [f"chat_id {chat_id}"]
            if conn_id:
                title_parts.append(f"({conn_id[:12]}…)")
            if messages:
                last = messages[-1]
                if last.username_from:
                    title_parts.insert(0, f"@{last.username_from}")
                elif last.first_name:
                    title_parts.insert(0, last.first_name)
            extra_context["wu_chat_title"] = " · ".join(title_parts)

            if messages:
                extra_context["wu_chat_html"] = mark_safe(
                    "".join(format_message_html(message) for message in messages)
                )
            else:
                extra_context["wu_chat_html"] = "Сообщений пока нет."

        return super().changelist_view(request, extra_context=extra_context)

    @admin.display(description="Чат")
    def chat_link(self, obj):
        url = reverse("admin:webhook_tg_message_changelist") + f"?chat_id={obj.chat_id}"
        if obj.business_connection_id:
            url += f"&business_connection_id={quote(obj.business_connection_id, safe='')}"
        label = obj.username_from or obj.first_name or str(obj.chat_id)
        if obj.username_from and not label.startswith("@"):
            label = f"@{label}"
        return format_html('<a href="{}">Открыть чат</a>', url)

    @admin.display(description="Текст")
    def text_preview(self, obj):
        text = obj.text or obj.caption or ""
        if len(text) > 80:
            return text[:77] + "…"
        return text or "—"


@admin.register(AdminChatFilter)
class AdminChatFilterAdmin(admin.ModelAdmin):
    list_display = ("user", "chat_id", "business_connection_id", "chat_open_link")
    list_filter = ("chat_id", "business_connection_id")
    search_fields = ("user__username", "business_connection_id")
    autocomplete_fields = ("user",)

    @admin.display(description="Чат")
    def chat_open_link(self, obj):
        url = reverse("admin:webhook_tg_message_changelist") + f"?chat_id={obj.chat_id}"
        if obj.business_connection_id:
            url += f"&business_connection_id={quote(obj.business_connection_id, safe='')}"
        return format_html('<a href="{}">Открыть чат</a>', url)


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


@admin.register(BotOutgoingMessage)
class BotOutgoingMessageAdmin(admin.ModelAdmin):
    list_display = ("sent_at", "chat_id", "recipient", "method")
    list_filter = ("method", ("sent_at", admin.DateFieldListFilter))
    search_fields = ("chat_id",)
    ordering = ("-sent_at",)
    date_hierarchy = "sent_at"
    list_per_page = 100
    readonly_fields = ("chat_id", "method", "sent_at")

    @admin.display(description="Получатель")
    def recipient(self, obj):
        user = UserTg.objects.filter(chat_id=obj.chat_id).first()
        if user:
            if user.username:
                return f"@{user.username}"
            if user.first_name:
                return user.first_name
        return "—"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


admin.site.site_header = "WhoUpdateBot Admin"
admin.site.site_title = "WhoUpdateBot"
admin.site.index_title = "Панель управления"
