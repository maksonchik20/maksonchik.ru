from django.contrib import admin

from .models import Lead


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = ("name", "contact", "page_title", "notification_sent", "created_at")
    list_filter = ("notification_sent", "created_at")
    search_fields = ("name", "contact", "message", "page_url", "utm_campaign")
    readonly_fields = (
        "name",
        "contact",
        "message",
        "page_url",
        "page_title",
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_content",
        "utm_term",
        "notification_sent",
        "notification_error",
        "created_at",
    )

    def has_add_permission(self, request):
        return False
