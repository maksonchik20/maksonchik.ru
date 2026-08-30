import json
from urllib.parse import quote, urlencode

from django.contrib import admin
from django.db.models import Q
from django.http import (
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
    JsonResponse,
    StreamingHttpResponse,
)
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from .chat_display import format_message_html
from .models import (
    BackgroundTask,
    Message,
    UserTg,
    AdminChatFilter,
    TelegramOutbox,
    TelegramIncomingUpdate,
    BotOutgoingMessage,
    BotChatEvent,
    MutedPeer,
    WhoUpdatePaymentOrder,
    WhoUpdateOnboardingFunnel,
    WhoUpdateMetrikaConversion,
)
from .telegram import (
    get_telegram_file_path,
    guess_telegram_file_mime,
    open_telegram_file_stream,
)

HIDDEN_USERNAMES = {"@tamataeva86", }
MESSAGE_LIST_DEFER = ("payload",)
MESSAGE_LIST_ONLY = (
    "id",
    "chat_id",
    "username_from",
    "first_name",
    "text",
    "caption",
    "file_id",
    "file_type",
    "created_at",
    "business_connection_id",
    "message_id",
)
MESSAGE_CHAT_PAGE_SIZE = 100


@admin.register(TelegramIncomingUpdate)
class TelegramIncomingUpdateAdmin(admin.ModelAdmin):
    list_display = ("update_id", "queue", "status", "attempts", "created_at", "processed_at")
    list_filter = ("queue", "status")
    search_fields = ("update_id", "last_error")
    readonly_fields = (
        "update_id",
        "payload",
        "queue",
        "status",
        "attempts",
        "next_attempt_at",
        "started_at",
        "processed_at",
        "last_error",
        "created_at",
    )
    ordering = ("-created_at",)
    list_per_page = 50


class BusinessConnectionIdFilter(admin.SimpleListFilter):
    """Текстовый фильтр: без DISTINCT по таблице, страница без фильтра открывается быстро."""

    title = "business_connection_id"
    parameter_name = "business_connection_id"
    template = "admin/webhook_tg/business_connection_id_filter.html"

    def lookups(self, request, model_admin):
        # Нужен непустой lookups, чтобы фильтр попал в сайдбар.
        return (("__input__", "input"),)

    def queryset(self, request, queryset):
        value = (self.value() or "").strip()
        if not value:
            return queryset
        return queryset.filter(business_connection_id__icontains=value)

    def choices(self, changelist):
        params = changelist.params.copy()
        params.pop(self.parameter_name, None)
        yield {
            "selected": not self.value(),
            "query_string": changelist.get_query_string(remove=[self.parameter_name]),
            "display": "Все",
            "query_parts": list(params.items()),
        }


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    change_list_template = "admin/webhook_tg/message/change_list.html"
    list_display = (
        "chat_link",
        "username_from",
        "first_name",
        "text_preview",
        "file_id_preview",
        "file_type",
        "created_at",
    )
    list_filter = (BusinessConnectionIdFilter,)
    search_fields = (
        "username_from",
        "first_name",
        "text",
        "message_id",
        "business_connection_id",
        "file_id",
    )
    ordering = ("-created_at",)
    list_per_page = 50
    show_full_result_count = False  # не делать COUNT(*) на всю таблицу

    readonly_fields = ("created_at",)

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "chat-page/",
                self.admin_site.admin_view(self.chat_page_view),
                name="webhook_tg_message_chat_page",
            ),
            path(
                "preview-file/",
                self.admin_site.admin_view(self.preview_file_view),
                name="webhook_tg_message_preview_file",
            ),
        ]
        return custom + urls

    def _chat_page(self, request, *, before_id=None):
        queryset = self.get_queryset(request).only(*MESSAGE_LIST_ONLY).order_by("-id")
        if before_id is not None:
            queryset = queryset.filter(id__lt=before_id)

        rows = list(queryset[: MESSAGE_CHAT_PAGE_SIZE + 1])
        has_more = len(rows) > MESSAGE_CHAT_PAGE_SIZE
        rows = rows[:MESSAGE_CHAT_PAGE_SIZE]
        next_before = rows[-1].id if has_more and rows else None
        rows.reverse()
        return rows, has_more, next_before

    @staticmethod
    def _parse_positive_int(value):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    def chat_page_view(self, request):
        """Возвращает следующую порцию более старых сообщений для AJAX-чата."""
        if not self.has_view_permission(request):
            return HttpResponseForbidden("Недостаточно прав")

        chat_id = self._parse_positive_int(request.GET.get("chat_id"))
        before_id = self._parse_positive_int(request.GET.get("before_id"))
        if chat_id is None:
            return HttpResponseBadRequest("chat_id required")
        if request.GET.get("before_id") and before_id is None:
            return HttpResponseBadRequest("invalid before_id")

        messages, has_more, next_before = self._chat_page(
            request,
            before_id=before_id,
        )
        return JsonResponse(
            {
                "html": "".join(str(format_message_html(message)) for message in messages),
                "loaded": len(messages),
                "has_more": has_more,
                "next_before": next_before,
            }
        )

    def preview_file_view(self, request):
        """Проксирует файл из Telegram API в браузер без сохранения на диск."""
        file_id = (request.GET.get("file_id") or "").strip()
        file_type = (request.GET.get("file_type") or "").strip()
        if not file_id:
            return HttpResponseBadRequest("file_id required")

        # Только file_id, которые есть в доступных пользователю сообщениях.
        allowed = self.get_queryset(request).filter(file_id=file_id).exists()
        if not allowed:
            return HttpResponseForbidden("file_id not allowed")

        try:
            file_path = get_telegram_file_path(file_id)
            upstream = open_telegram_file_stream(file_path)
        except Exception as exc:
            return HttpResponse(f"Telegram file error: {exc}", status=502)

        content_type = guess_telegram_file_mime(file_path, file_type)

        def stream():
            try:
                for chunk in upstream.iter_content(64 * 1024):
                    if chunk:
                        yield chunk
            finally:
                upstream.close()

        response = StreamingHttpResponse(stream(), content_type=content_type)
        response["Cache-Control"] = "no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        # список/чат не нужны гигантские payload
        if request.resolver_match and request.resolver_match.url_name == "webhook_tg_message_changelist":
            qs = qs.only(*MESSAGE_LIST_ONLY)
        else:
            qs = qs.defer(*MESSAGE_LIST_DEFER)
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
            messages, has_more, next_before = self._chat_page(request)
            extra_context["wu_chat_shown"] = len(messages)
            extra_context["wu_chat_limited"] = has_more
            extra_context["wu_chat_next_before"] = next_before or ""

            load_query = {"chat_id": chat_id}
            if conn_id:
                load_query["business_connection_id"] = conn_id
            extra_context["wu_chat_load_url"] = (
                reverse("admin:webhook_tg_message_chat_page")
                + "?"
                + urlencode(load_query)
            )

            title_parts = [f"chat_id {chat_id}"]
            if conn_id:
                title_parts.append(f"({conn_id[:12]}…)")
            if messages:
                head = messages[-1]
                if head.username_from:
                    title_parts.insert(0, f"@{head.username_from}")
                elif head.first_name:
                    title_parts.insert(0, head.first_name)
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

    @admin.display(description="file_id")
    def file_id_preview(self, obj):
        return obj.file_id or "—"


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


@admin.register(UserTg)
class UserTgAdmin(admin.ModelAdmin):
    list_display = (
        "username",
        "dialog_link",
        "user_id",
        "business_is_connected",
        "last_start_at",
        "business_connected_at",
        "business_disconnected_at",
        "connection_reminder_at",
        "connection_reminder_sent_at",
        "access_unlimited",
        "access_expires_at",
        "referral_bonus_days",
        "referred_by",
    )
    list_filter = ("business_is_connected", "access_unlimited")
    search_fields = ("username", "first_name", "user_id", "chat_id", "business_connection_id", "referral_code")
    ordering = ("-last_start_at",)
    readonly_fields = ("dialog_link",)

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<path:object_id>/dialog/",
                self.admin_site.admin_view(self.dialog_view),
                name="webhook_tg_usertg_dialog",
            ),
        ]
        return custom + urls

    @admin.display(description="Диалог")
    def dialog_link(self, obj):
        if not obj or not obj.pk:
            return "—"
        url = reverse("admin:webhook_tg_usertg_dialog", args=[obj.pk])
        return format_html('<a class="button" href="{}">Открыть диалог</a>', url)

    def dialog_view(self, request, object_id):
        if not request.user.is_superuser:
            return HttpResponseForbidden("Диалоги доступны только суперпользователю")
        bot_user = self.get_object(request, object_id)
        if bot_user is None:
            return HttpResponse(status=404)

        total = BotChatEvent.objects.filter(chat_id=bot_user.chat_id).count()
        events = list(
            BotChatEvent.objects.filter(chat_id=bot_user.chat_id)
            .order_by("-created_at", "-id")[:500]
        )
        events.reverse()
        for event in events:
            event.payload_pretty = json.dumps(
                event.payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "original": bot_user,
            "bot_user": bot_user,
            "events": events,
            "event_total": total,
            "event_limited": total > len(events),
            "title": f"Диалог с @{bot_user.username}" if bot_user.username else f"Диалог {bot_user.chat_id}",
        }
        return TemplateResponse(
            request,
            "admin/webhook_tg/usertg/dialog.html",
            context,
        )


@admin.register(WhoUpdatePaymentOrder)
class WhoUpdatePaymentOrderAdmin(admin.ModelAdmin):
    list_display = ("public_id", "user", "plan", "amount", "status", "paid_at", "access_expires_at_after")
    list_filter = ("status", "plan")
    search_fields = ("public_id", "yookassa_payment_id", "user__username", "user__user_id")
    readonly_fields = ("public_id", "created_at", "paid_at", "access_expires_at_after")
    ordering = ("-created_at",)


@admin.register(WhoUpdateOnboardingFunnel)
class WhoUpdateOnboardingFunnelAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "tracking_code",
        "user",
        "utm_source",
        "utm_campaign",
        "last_step",
        "connection_stage",
    )
    list_filter = (
        "connection_stage",
        "utm_source",
        "utm_campaign",
        ("created_at", admin.DateFieldListFilter),
    )
    search_fields = (
        "tracking_code",
        "yclid",
        "metrika_client_id",
        "utm_term",
        "user__username",
        "user__user_id",
    )
    ordering = ("-created_at",)
    readonly_fields = (
        "tracking_code",
        "user",
        "landing_path",
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_content",
        "utm_term",
        "utm_device",
        "utm_region",
        "yclid",
        "metrika_client_id",
        "landing_viewed_at",
        "telegram_started_at",
        "start_update_id",
        "demo_opened_at",
        "first_reminder_sent_at",
        "second_reminder_sent_at",
        "connected_at",
        "connection_stage",
        "created_at",
        "updated_at",
    )

    @admin.display(description="Последний этап")
    def last_step(self, obj):
        if obj.connected_at:
            return "Подключился"
        if obj.second_reminder_sent_at:
            return "После второго напоминания"
        if obj.first_reminder_sent_at:
            return "После первого напоминания"
        if obj.telegram_started_at:
            if obj.demo_opened_at:
                return "/start, открыл демонстрацию"
            return "/start, не подключился"
        return "Лендинг, без /start"

    def has_add_permission(self, request):
        return False


@admin.register(WhoUpdateMetrikaConversion)
class WhoUpdateMetrikaConversionAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "funnel",
        "event_type",
        "identifier_type",
        "status",
        "api_status",
        "attempts",
        "submitted_at",
        "processed_at",
    )
    list_filter = ("status", "event_type", "identifier_type", "api_status")
    search_fields = (
        "funnel__tracking_code",
        "funnel__user__username",
        "funnel__user__user_id",
        "api_upload_id",
    )
    ordering = ("-created_at",)
    readonly_fields = (
        "funnel",
        "event_type",
        "target",
        "occurred_at",
        "identifier_type",
        "identifier",
        "status",
        "attempts",
        "next_attempt_at",
        "api_upload_id",
        "api_status",
        "last_error",
        "submitted_at",
        "processed_at",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False


@admin.register(MutedPeer)
class MutedPeerAdmin(admin.ModelAdmin):
    list_display = (
        "owner_user_id",
        "owner_chat_id",
        "muted_username",
        "muted_user_id",
        "expires_at",
        "notify_in_bot",
        "warning_sent",
        "created_at",
    )
    list_filter = ("notify_in_bot", "warning_sent")
    search_fields = ("muted_username", "owner_user_id", "muted_user_id")
    ordering = ("-created_at",)


@admin.register(BackgroundTask)
class BackgroundTaskAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "task_type",
        "status",
        "run_at",
        "attempts",
        "max_attempts",
        "locked_by",
        "created_at",
    )
    list_filter = ("status", "task_type")
    search_fields = ("idempotency_key", "last_error")
    ordering = ("run_at", "priority", "created_at")
    readonly_fields = (
        "task_type",
        "payload",
        "status",
        "priority",
        "run_at",
        "attempts",
        "max_attempts",
        "locked_at",
        "locked_by",
        "last_error",
        "idempotency_key",
        "created_at",
        "updated_at",
        "completed_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser


@admin.register(TelegramOutbox)
class TelegramOutboxAdmin(admin.ModelAdmin):
    list_display = ("id", "method", "chat_id", "attempts", "next_attempt_at", "created_at", "idempotency_key")
    list_filter = ("method",)
    search_fields = ("chat_id", "idempotency_key", "last_error")
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
        # кэш на queryset, чтобы не бить UserTg на каждую строку
        cache = getattr(self, "_recipient_cache", None)
        if cache is None:
            cache = {
                u.chat_id: u
                for u in UserTg.objects.only("chat_id", "username", "first_name")
            }
            self._recipient_cache = cache
        user = cache.get(obj.chat_id)
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


@admin.register(BotChatEvent)
class BotChatEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "chat_id", "recipient", "direction", "event_type", "text_preview")
    list_filter = ("direction", "event_type", ("created_at", admin.DateFieldListFilter))
    search_fields = ("chat_id", "text", "update_id", "telegram_message_id")
    ordering = ("-created_at", "-id")
    readonly_fields = (
        "chat_id",
        "direction",
        "event_type",
        "text",
        "payload",
        "telegram_message_id",
        "update_id",
        "source_key",
        "created_at",
    )

    @admin.display(description="Пользователь")
    def recipient(self, obj):
        user = UserTg.objects.filter(chat_id=obj.chat_id).only("id", "username", "first_name").first()
        if user is None:
            return "—"
        label = f"@{user.username}" if user.username else user.first_name or str(user.chat_id)
        url = reverse("admin:webhook_tg_usertg_dialog", args=[user.pk])
        return format_html('<a href="{}">{}</a>', url, label)

    @admin.display(description="Текст")
    def text_preview(self, obj):
        text = obj.text or "—"
        return text if len(text) <= 100 else text[:97] + "…"

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


admin.site.site_header = "WhoUpdateBot Admin"
admin.site.site_title = "WhoUpdateBot"
admin.site.index_title = "Панель управления"
