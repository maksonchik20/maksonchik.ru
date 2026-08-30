from django.urls import path
from django.views.generic import RedirectView

from webhook_tg import payment_views, seo_views, views


urlpatterns = [
    path("", seo_views.who_update_landing, name="who_update_home"),
    path("privacy/", seo_views.who_update_privacy, name="who_update_privacy"),
    path("terms/", seo_views.who_update_terms, name="who_update_terms"),
    path(
        "bot/",
        RedirectView.as_view(url="/", permanent=True),
        name="who_update_legacy_bot",
    ),
    path(
        "who-update-bot/",
        RedirectView.as_view(url="/", permanent=True),
        name="who_update_legacy_alias",
    ),
    path(
        "bot/analytics/client-id/",
        seo_views.who_update_metrika_client_id,
        name="who_update_metrika_client_id",
    ),
    path("webhook_tg/", views.webhook_tg, name="webhook_tg"),
    path("webhook_tg/owner-notify/", views.owner_notify, name="owner_notify"),
    path(
        "bot/subscribe/<str:plan>/<str:token>/",
        payment_views.subscribe,
        name="who_update_subscribe",
    ),
    path(
        "bot/payment/<uuid:public_id>/",
        payment_views.payment_result,
        name="who_update_payment_result",
    ),
    path(
        "bot/yookassa/webhook/",
        payment_views.yookassa_webhook,
        name="who_update_yookassa_webhook",
    ),
    path(
        "yandex_1cf6644d705ed152.html",
        seo_views.yandex_webmaster_verify,
        name="yandex_webmaster_verify",
    ),
    path(
        f"{seo_views.INDEXNOW_KEY}.txt",
        seo_views.indexnow_key_file,
        name="indexnow_key_file",
    ),
    path("sitemap.xml", seo_views.sitemap_xml, name="sitemap_xml"),
    path("robots.txt", seo_views.robots_txt, name="robots_txt"),
    path(
        "who-update-favicon.svg",
        seo_views.who_update_favicon_svg,
        name="who_update_favicon_svg",
    ),
]
