from pathlib import Path
import json

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt

from main.sitemap_urls import render_sitemap_xml, render_who_update_sitemap_xml
from .models import WhoUpdateOnboardingFunnel
from .onboarding_analytics import capture_landing_view, tracked_telegram_url


BASE_DIR = Path(__file__).resolve().parent.parent
INDEXNOW_KEY = "qIXnCp99XqCIbkmFQv6mWaNweY2n1fio"
WHO_UPDATE_ORIGIN = "https://who-update.ru"


def _is_who_update_host(request: HttpRequest) -> bool:
    return request.get_host().split(":", 1)[0].lower() in {
        "who-update.ru",
        "www.who-update.ru",
    }


def who_update_landing(request: HttpRequest):
    user_agent = str(request.headers.get("User-Agent") or "").lower()
    is_crawler = any(marker in user_agent for marker in ("bot", "crawler", "spider", "slurp"))
    funnel = None if is_crawler else capture_landing_view(request)
    return render(
        request,
        "webhook_tg/landing.html",
        {
            "canonical_url": f"{WHO_UPDATE_ORIGIN}/",
            "landing_url": f"{WHO_UPDATE_ORIGIN}/",
            "main_site_url": "https://maksonchik.ru/",
            "privacy_url": f"{WHO_UPDATE_ORIGIN}/privacy/",
            "terms_url": f"{WHO_UPDATE_ORIGIN}/terms/",
            "telegram_bot_url": (
                tracked_telegram_url(funnel)
                if funnel is not None
                else "https://t.me/who_update_bot"
            ),
            "tracking_code": funnel.tracking_code if funnel is not None else "",
        },
    )


def _legal_context(document_type: str) -> dict:
    return {
        "document_type": document_type,
        "landing_url": f"{WHO_UPDATE_ORIGIN}/",
        "main_site_url": "https://maksonchik.ru/",
        "privacy_url": f"{WHO_UPDATE_ORIGIN}/privacy/",
        "terms_url": f"{WHO_UPDATE_ORIGIN}/terms/",
    }


def who_update_privacy(request: HttpRequest):
    return render(
        request,
        "webhook_tg/legal.html",
        _legal_context("privacy"),
    )


def who_update_terms(request: HttpRequest):
    return render(
        request,
        "webhook_tg/legal.html",
        _legal_context("terms"),
    )


@csrf_exempt
def who_update_metrika_client_id(request: HttpRequest):
    if request.method != "POST":
        return HttpResponse(status=405)
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({"ok": False}, status=400)
    tracking_code = str(payload.get("tracking_code") or "").strip()
    client_id = str(payload.get("client_id") or "").strip()[:255]
    if not tracking_code or not client_id:
        return JsonResponse({"ok": False}, status=400)
    updated = WhoUpdateOnboardingFunnel.objects.filter(
        tracking_code=tracking_code,
        metrika_client_id="",
    ).update(metrika_client_id=client_id)
    return JsonResponse({"ok": True, "updated": bool(updated)})


def yandex_webmaster_verify(request: HttpRequest):
    content = (BASE_DIR / "yandex_1cf6644d705ed152.html").read_text(encoding="utf-8")
    return HttpResponse(content, content_type="text/html; charset=UTF-8")


def yandex_who_update_webmaster_verify(request: HttpRequest):
    content = (BASE_DIR / "yandex_f8f11c5f646de698.html").read_text(encoding="utf-8")
    return HttpResponse(content, content_type="text/html; charset=UTF-8")


def indexnow_key_file(request: HttpRequest):
    content = (BASE_DIR / f"{INDEXNOW_KEY}.txt").read_text(encoding="utf-8").strip()
    return HttpResponse(content, content_type="text/plain; charset=UTF-8")


def sitemap_xml(request: HttpRequest):
    return HttpResponse(
        (
            render_who_update_sitemap_xml()
            if _is_who_update_host(request)
            else render_sitemap_xml()
        ),
        content_type="application/xml; charset=UTF-8",
    )


def robots_txt(request: HttpRequest):
    if _is_who_update_host(request):
        content = (
            "User-agent: *\n"
            "Allow: /\n"
            "Disallow: /webhook_tg/\n"
            "Disallow: /bot/subscribe/\n"
            "Disallow: /bot/payment/\n"
            "Disallow: /bot/yookassa/\n\n"
            f"Sitemap: {WHO_UPDATE_ORIGIN}/sitemap.xml\n"
        )
    else:
        content = (BASE_DIR / "robots.txt").read_text(encoding="utf-8")
    return HttpResponse(content, content_type="text/plain; charset=UTF-8")


def who_update_favicon_svg(request: HttpRequest):
    content = (BASE_DIR / "who-update-favicon.svg").read_text(encoding="utf-8")
    return HttpResponse(content, content_type="image/svg+xml")
