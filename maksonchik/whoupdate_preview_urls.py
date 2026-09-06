"""Read-only presentation routes; no Telegram or payment endpoints."""
from django.conf import settings
from django.conf.urls.static import static
from django.urls import path
from django.views.generic import TemplateView
from webhook_tg import seo_views

urlpatterns = [
    path("", TemplateView.as_view(template_name="webhook_tg/landing.html", extra_context={
        "landing_url": "/", "privacy_url": "/privacy/", "terms_url": "/terms/",
        "telegram_bot_url": "https://t.me/who_update_bot", "tracking_code": "",
    }), name="who_update_home"),
    path("privacy/", TemplateView.as_view(template_name="webhook_tg/legal.html", extra_context={
        "document_type": "privacy", "landing_url": "/", "privacy_url": "/privacy/", "terms_url": "/terms/",
    })),
    path("terms/", TemplateView.as_view(template_name="webhook_tg/legal.html", extra_context={
        "document_type": "terms", "landing_url": "/", "privacy_url": "/privacy/", "terms_url": "/terms/",
    })),
    path("who-update-favicon.svg", seo_views.who_update_favicon_svg),
] + static("/who-update-demo-media/", document_root=settings.WHO_UPDATE_DEMO_MEDIA_ROOT)
