import json
from urllib.parse import quote, urlencode

from django import forms
from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.core.paginator import Paginator
from django.contrib.admin.views.main import ChangeList
from django.db.models import Case, CharField, Exists, F, OuterRef, Q, Value, When, Sum
from django.db import transaction
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
from django.utils import timezone

from .chat_display import format_message_html
from .admin_pagination import CURSOR_PARAMS, KeysetAdminMixin
from .dialog_media import DialogMediaAdminMixin, event_media
from .models import (
    BackgroundTask,
    Message,
    FileType,
    UserTg,
    AdminChatFilter,
    TelegramOutbox,
    TelegramIncomingUpdate,
    BotOutgoingMessage,
    BotChatEvent,
    MutedPeer,
    WhoUpdatePaymentOrder,
    WhoUpdatePartnerSettings,
    WhoUpdatePartnerProfile,
    WhoUpdatePartnerCommission,
    WhoUpdatePartnerWithdrawal,
    WhoUpdateWheelReward,
    WhoUpdateOnboardingFunnel,
    WhoUpdateMetrikaConversion,
)
from .telegram import (
    get_telegram_file_path,
    guess_telegram_file_mime,
    open_telegram_file_stream,
)
from .wheel import PRIZES, reset_spin
from .partners import partner_percent, referral_statistics, resolve_withdrawals, void_commissions
from .subscriptions import referral_link

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
    "saved_photo_file_id",
    "saved_photo_source_file_id",
    "is_deleted",
    "deleted_at",
    "created_at",
    "business_connection_id",
    "message_id",
)
MESSAGE_CHAT_PAGE_SIZE = 100


@admin.register(TelegramIncomingUpdate)
class TelegramIncomingUpdateAdmin(KeysetAdminMixin, admin.ModelAdmin):
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
    ordering = ("-id",)
    list_per_page = 50

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        if request.resolver_match and request.resolver_match.url_name == "webhook_tg_telegramincomingupdate_changelist":
            queryset = queryset.defer("payload", "last_error")
        return queryset


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
        paramToValue = changelist.params.copy()
        for name in (self.parameter_name, *CURSOR_PARAMS, "p", "all", "o"):
            paramToValue.pop(name, None)
        yield {
            "selected": not self.value(),
            "query_string": changelist.get_query_string(remove=[self.parameter_name]),
            "display": "Все",
            "query_parts": list(paramToValue.items()),
        }


@admin.register(Message)
class MessageAdmin(KeysetAdminMixin, admin.ModelAdmin):
    change_list_template = "admin/webhook_tg/message/change_list.html"
    list_display = (
        "chat_link",
        "username_from",
        "first_name",
        "text_preview",
        "file_id_preview",
        "saved_photo_id_preview",
        "file_type",
        "is_deleted",
        "deleted_at",
        "created_at",
    )
    list_filter = (BusinessConnectionIdFilter, "file_type", "is_deleted")
    search_fields = (
        "username_from",
        "first_name",
        "text",
        "message_id",
        "business_connection_id",
        "file_id",
        "saved_photo_file_id",
        "saved_photo_source_file_id",
    )
    ordering = ("-id",)
    list_per_page = 50
    show_full_result_count = False  # не делать COUNT(*) на всю таблицу

    readonly_fields = (
        "created_at", "saved_photo_file_id", "saved_photo_source_file_id",
        "is_deleted", "deleted_at",
    )

    def skip_journal_results(self, request):
        return bool(request.GET.get("chat_id")) and request.method == "GET"

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
        if not self.has_view_permission(request):
            return HttpResponseForbidden("Недостаточно прав")
        file_id = (request.GET.get("file_id") or "").strip()
        file_type = (request.GET.get("file_type") or "").strip()
        if not file_id:
            return HttpResponseBadRequest("file_id required")

        # Только file_id, которые есть в доступных пользователю сообщениях.
        message = self.get_queryset(request).filter(file_id=file_id).first()
        if message is None:
            return HttpResponseForbidden("file_id not allowed")

        download_id = message.available_photo_copy_id or file_id
        try:
            file_path = get_telegram_file_path(download_id)
            upstream = open_telegram_file_stream(file_path)
        except Exception as exc:
            if (
                message.file_type == FileType.SELF_DESTRUCT_PHOTO
                and not message.available_photo_copy_id
                and "wrong file_id or the file is temporarily unavailable" in str(exc)
            ):
                return HttpResponse(
                    "Самоуничтожающееся фото больше недоступно в Telegram. "
                    "Сохранённой копии нет; исходный file_id остаётся в записи.",
                    status=410,
                    content_type="text/plain; charset=utf-8",
                )
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

        # The chat and its AJAX history use this queryset without ChangeList.
        file_type = request.GET.get("file_type__exact")
        if file_type:
            filtered = filtered.filter(file_type=file_type)
        deleted_filter = request.GET.get("is_deleted__exact")
        if deleted_filter in ("0", "1"):
            filtered = filtered.filter(is_deleted=deleted_filter == "1")

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
            if request.GET.get("file_type__exact"):
                load_query["file_type__exact"] = request.GET["file_type__exact"]
            if request.GET.get("is_deleted__exact") in ("0", "1"):
                load_query["is_deleted__exact"] = request.GET["is_deleted__exact"]
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

    @admin.display(description="file_id копии фото")
    def saved_photo_id_preview(self, obj):
        return obj.available_photo_copy_id or "—"


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


class UserTgChangeList(ChangeList):
    def get_ordering(self, request, queryset):
        ordering = super().get_ordering(request, queryset)
        # Apply after Django resolves column clicks, including reverse sorting.
        return [
            F("last_start_at").desc(nulls_last=True)
            if field == "-last_start_at"
            else F("last_start_at").asc(nulls_last=True)
            if field == "last_start_at"
            else field
            for field in ordering
        ]


class PartnerProfileInline(admin.StackedInline):
    model = WhoUpdatePartnerProfile
    extra = 1
    max_num = 1
    can_delete = False
    verbose_name_plural = "Индивидуальные условия сотрудничества"
    fields = ("is_active", "commission_percent", "notes")


class UserTgAdminForm(forms.ModelForm):
    class Meta:
        model = UserTg
        fields = "__all__"

    def clean_referred_by(self):
        inviter = self.cleaned_data.get("referred_by")
        if inviter and (inviter.pk == self.instance.pk or inviter.user_id == self.cleaned_data.get("user_id")):
            raise forms.ValidationError("Пользователь не может пригласить сам себя.")
        return inviter


@admin.register(UserTg)
class UserTgAdmin(DialogMediaAdminMixin, admin.ModelAdmin):
    form = UserTgAdminForm
    change_form_template = "admin/webhook_tg/usertg/change_form.html"
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
        "access_expires_at",
        "referral_bonus_days",
        "referred_by",
    )
    list_filter = ("business_is_connected", "access_unlimited")
    search_fields = ("username", "first_name", "user_id", "chat_id", "business_connection_id", "referral_code")
    ordering = ("-last_start_at",)
    readonly_fields = ("dialog_link", "referral_link_display")
    autocomplete_fields = ("referred_by",)

    def get_inlines(self, request, obj=None):
        return [PartnerProfileInline] if request.user.is_superuser else []

    @admin.display(description="Реферальная ссылка")
    def referral_link_display(self, obj):
        if not obj or not obj.pk:
            return "Появится после сохранения"
        link = referral_link(obj)
        return format_html('<a href="{}" target="_blank" rel="noopener">{}</a>', link, link)

    def user_sections_context(self, request, bot_user):
        total = BotChatEvent.objects.filter(chat_id=bot_user.chat_id).count()
        events = list(BotChatEvent.objects.filter(chat_id=bot_user.chat_id).order_by("-created_at", "-id")[:500])
        events.reverse()
        for event in events:
            event.dialog_media = event_media(event)
            event.payload_pretty = json.dumps(event.payload, ensure_ascii=False, indent=2, sort_keys=True)
        referrals, summary = referral_statistics(bot_user)
        referral_page = Paginator(referrals, 50).get_page(request.GET.get("ref_page", 1))
        return {
            "bot_user": bot_user, "events": events, "event_total": total,
            "event_limited": total > len(events), "referral_page": referral_page,
            "referral_summary": summary, "partner_percent": partner_percent(bot_user),
            "referral_url": referral_link(bot_user),
            "active_user_tab": "referrals" if request.GET.get("tab") == "referrals" else "chat",
            "show_user_sections": True,
            "withdrawals": bot_user.partner_withdrawals.order_by("-created_at")[:10],
        }

    def render_change_form(self, request, context, add=False, change=False, form_url="", obj=None):
        if obj and request.user.is_superuser:
            context.update(self.user_sections_context(request, obj))
        return super().render_change_form(request, context, add, change, form_url, obj)


    def get_changelist(self, request, **kwargs):
        return UserTgChangeList

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

        context = {
            **self.admin_site.each_context(request),
            **self.user_sections_context(request, bot_user),
            "opts": self.model._meta,
            "original": bot_user,
            "title": f"Пользователь @{bot_user.username}" if bot_user.username else f"Пользователь {bot_user.chat_id}",
        }
        return TemplateResponse(
            request,
            "admin/webhook_tg/usertg/dialog.html",
            context,
        )


@admin.register(WhoUpdatePaymentOrder)
class WhoUpdatePaymentOrderAdmin(admin.ModelAdmin):
    list_display = ("public_id", "user", "plan", "amount", "status", "paid_at", "access_expires_at_after")
    list_filter = ("status", "plan", "is_test")
    search_fields = ("public_id", "yookassa_payment_id", "user__username", "user__user_id")
    readonly_fields = ("public_id", "created_at", "paid_at", "access_expires_at_after", "is_test")
    ordering = ("-created_at",)


WHEEL_REWARD_STATUSES = (
    ("granted", "Начислен бесплатно"),
    ("active", "Действует"),
    ("reserved", "Закреплён за оплатой"),
    ("used", "Использован"),
    ("expired", "Истёк"),
    ("superseded", "Заменён новым призом"),
)


class WheelPrizeFilter(admin.SimpleListFilter):
    title = "приз"
    parameter_name = "prize"

    def lookups(self, request, model_admin):
        return [(prize["id"], prize["label"]) for prize in PRIZES]

    def queryset(self, request, queryset):
        return queryset.filter(prize=self.value()) if self.value() else queryset


class WheelRewardStatusFilter(admin.SimpleListFilter):
    title = "статус бонуса"
    parameter_name = "reward_status"

    def lookups(self, request, model_admin):
        return WHEEL_REWARD_STATUSES

    def queryset(self, request, queryset):
        return queryset.filter(_reward_status=self.value()) if self.value() else queryset


@admin.register(WhoUpdateWheelReward)
class WhoUpdateWheelRewardAdmin(admin.ModelAdmin):
    list_display = ("id", "winner", "telegram_id", "prize_label", "spin_date", "won_at", "reward_status", "valid_until", "payment_link")
    list_display_links = ("id",)
    list_filter = (WheelPrizeFilter, WheelRewardStatusFilter, "spin_day")
    search_fields = ("user__username", "user__first_name", "=user__user_id")
    ordering = ("-created_at", "-pk")
    date_hierarchy = "spin_day"
    list_per_page = 50
    actions = ("reset_daily_spin",)
    readonly_fields = ("winner", "telegram_id", "prize_label", "spin_date", "won_at", "reward_status", "valid_until", "consumed_at", "payment_link", "granted_days", "access_expires_at_after", "spin_reset_at")
    fields = readonly_fields

    def has_reset_spin_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    @admin.action(description="Сбросить дату вращения (разрешить сегодня)", permissions=["reset_spin"])
    def reset_daily_spin(self, request, queryset):
        if not self.has_reset_spin_permission(request):
            return HttpResponseForbidden()
        count = 0
        # Lock users in stable order; re-check the selected reward under the
        # same user lock used by spin. A stale/repeated action cannot grant twice.
        for reward in queryset.order_by("user_id", "pk"):
            with transaction.atomic():
                if reset_spin(reward):
                    self.log_change(request, reward, "Сброшен дневной лимит вращения. История выигрыша и оплата сохранены.")
                    count += 1
        if count:
            self.message_user(request, f"Повторное вращение разрешено: {count}. Переоткройте Mini App.", messages.SUCCESS)
        else:
            self.message_user(request, "Выберите последний сегодняшний выигрыш. Лимит уже сброшен либо сброс для этого аккаунта недоступен.", messages.WARNING)

    def get_queryset(self, request):
        newer = WhoUpdateWheelReward.objects.filter(user_id=OuterRef("user_id")).filter(
            Q(spin_day__gt=OuterRef("spin_day")) | Q(spin_day=OuterRef("spin_day"), pk__gt=OuterRef("pk"))
        )
        return super().get_queryset(request).select_related("user", "reserved_order").annotate(
            _superseded=Exists(newer),
        ).annotate(_reward_status=Case(
            When(granted_days__gt=0, then=Value("granted")),
            When(used_at__isnull=False, then=Value("used")),
            When(reserved_order__status=WhoUpdatePaymentOrder.Status.PENDING, then=Value("reserved")),
            When(_superseded=True, then=Value("superseded")),
            When(expires_at__lte=timezone.now(), then=Value("expired")),
            default=Value("active"), output_field=CharField(),
        ))

    @admin.display(description="Пользователь", ordering="user__username")
    def winner(self, obj):
        user = obj.user
        username = str(user.username or "").strip().lstrip("@")
        label = f"@{username}" if username else user.first_name or str(user.user_id)
        return format_html('<a href="{}">{}</a>', reverse("admin:webhook_tg_usertg_change", args=[user.pk]), label)

    @admin.display(description="Telegram ID", ordering="user__user_id")
    def telegram_id(self, obj):
        return obj.user.user_id

    @admin.display(description="День вращения", ordering="spin_day")
    def spin_date(self, obj):
        return obj.spin_day

    @admin.display(description="Дата выигрыша", ordering="created_at")
    def won_at(self, obj):
        return obj.created_at

    @admin.display(description="Действует до", ordering="expires_at")
    def valid_until(self, obj):
        return obj.expires_at

    @admin.display(description="Использован", ordering="used_at")
    def consumed_at(self, obj):
        return obj.used_at

    @admin.display(description="Приз", ordering="prize")
    def prize_label(self, obj):
        return next((prize["label"] for prize in PRIZES if prize["id"] == obj.prize), obj.prize)

    @admin.display(description="Статус бонуса", ordering="_reward_status")
    def reward_status(self, obj):
        return dict(WHEEL_REWARD_STATUSES)[obj._reward_status]

    @admin.display(description="Оплата")
    def payment_link(self, obj):
        if not obj.reserved_order_id:
            return "—"
        order = obj.reserved_order
        return format_html('<a href="{}">{} · {} ₽ · {} дн.</a>',
            reverse("admin:webhook_tg_whoupdatepaymentorder_change", args=[order.pk]),
            order.get_status_display(), order.amount, order.duration_days,
        )

    # This is an audit log: editing/deleting a reward could reset daily limits
    # or change the terms of a payment that has already been created.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


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
        "value",
        "currency",
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
        "payment_order__public_id",
        "api_upload_id",
    )
    ordering = ("-created_at",)
    readonly_fields = (
        "funnel",
        "payment_order",
        "event_type",
        "target",
        "occurred_at",
        "identifier_type",
        "identifier",
        "value",
        "currency",
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
    list_display = (
        "id",
        "status",
        "method",
        "chat_id",
        "attempts",
        "next_attempt_at",
        "sent_at",
        "created_at",
        "idempotency_key",
    )
    list_filter = ("status", "method")
    search_fields = ("chat_id", "idempotency_key", "last_error")
    ordering = ("next_attempt_at",)
    readonly_fields = ("created_at", "sent_at", "payload", "last_error")

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


class PartnerAdminAccess(admin.ModelAdmin):
    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_change_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(WhoUpdatePartnerSettings)
class PartnerSettingsAdmin(PartnerAdminAccess):
    list_display = ("__str__", "is_active", "started_at")
    fields = ("is_active", "commission_percent", "started_at")
    readonly_fields = ("started_at",)


class PartnerActionAdmin(PartnerAdminAccess):
    def confirmation(self, request, queryset, operation):
        withdrawal = self.model is WhoUpdatePartnerWithdrawal
        rows = queryset.filter(status="new" if withdrawal else "earned")
        if not rows.exists():
            self.message_user(request, "Нет записей для обработки: выбранные записи уже обработаны.", messages.WARNING)
            return None
        if request.POST.get("confirm_partner_action"):
            note = (request.POST.get("note") or "").strip()
            try:
                ids = list(rows.values_list("pk", flat=True))
                if withdrawal:
                    changed = resolve_withdrawals(ids, status="paid" if operation == "mark_paid" else "rejected", actor=request.user, note=note)
                else:
                    ids = void_commissions(ids, actor=request.user, reason=note)
                    changed = list(self.model.objects.filter(pk__in=ids))
            except ValueError as exc:
                self.message_user(request, str(exc), messages.ERROR)
            else:
                for row in changed:
                    self.log_change(request, row, f"{operation}: {note}")
                self.message_user(request, f"Обработано: {len(changed)}.", messages.SUCCESS)
                return None
        titles = {"mark_paid": "Отметить заявки выплаченными", "reject": "Отклонить заявки", "cancel_earned": "Аннулировать начисления"}
        return TemplateResponse(request, "admin/webhook_tg/partners/confirm_action.html", {
            **self.admin_site.each_context(request), "opts": self.model._meta,
            "title": titles[operation], "rows": rows.select_related("partner"),
            "total": rows.aggregate(total=Sum("amount"))["total"],
            "selected": list(rows.values_list("pk", flat=True)),
            "action_name": operation, "action_checkbox_name": helpers.ACTION_CHECKBOX_NAME,
            "is_payout": operation == "mark_paid", "note": request.POST.get("note", ""),
        })


@admin.register(WhoUpdatePartnerCommission)
class PartnerCommissionAdmin(PartnerActionAdmin):
    list_display = ("id", "partner", "buyer", "purchase_amount", "commission_percent", "amount", "status", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("partner__username", "=partner__user_id", "buyer__username", "=buyer__user_id", "order__public_id")
    list_select_related = ("partner", "buyer", "order")
    readonly_fields = tuple(field.name for field in WhoUpdatePartnerCommission._meta.fields)
    actions = ("cancel_earned",)

    @admin.action(description="Аннулировать выбранные начисления")
    def cancel_earned(self, request, queryset):
        if not self.has_change_permission(request):
            return HttpResponseForbidden()
        return self.confirmation(request, queryset, "cancel_earned")


@admin.register(WhoUpdatePartnerWithdrawal)
class PartnerWithdrawalAdmin(PartnerActionAdmin):
    list_display = ("id", "partner", "amount", "status", "created_at", "resolved_at", "resolved_by", "note")
    list_filter = ("status", "created_at")
    search_fields = ("partner__username", "=partner__user_id", "note")
    list_select_related = ("partner", "resolved_by")
    readonly_fields = tuple(field.name for field in WhoUpdatePartnerWithdrawal._meta.fields) + ("partner_card",)
    actions = ("mark_paid", "reject")

    @admin.display(description="Баланс и рефералы")
    def partner_card(self, obj):
        url = reverse("admin:webhook_tg_usertg_change", args=[obj.partner_id]) + "?tab=referrals"
        return format_html('<a href="{}">Открыть карточку партнёра</a>', url)

    @admin.action(description="Отметить выбранные заявки выплаченными")
    def mark_paid(self, request, queryset):
        if not self.has_change_permission(request):
            return HttpResponseForbidden()
        return self.confirmation(request, queryset, "mark_paid")

    @admin.action(description="Отклонить выбранные заявки и снять резерв")
    def reject(self, request, queryset):
        if not self.has_change_permission(request):
            return HttpResponseForbidden()
        return self.confirmation(request, queryset, "reject")
