from pathlib import Path
import json

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt

from main.sitemap_urls import render_sitemap_xml
from .models import WhoUpdateOnboardingFunnel
from .onboarding_analytics import capture_landing_view, tracked_telegram_url


BASE_DIR = Path(__file__).resolve().parent.parent
INDEXNOW_KEY = "qIXnCp99XqCIbkmFQv6mWaNweY2n1fio"


def who_update_landing(request: HttpRequest):
    user_agent = str(request.headers.get("User-Agent") or "").lower()
    is_crawler = any(marker in user_agent for marker in ("bot", "crawler", "spider", "slurp"))
    funnel = None if is_crawler else capture_landing_view(request)
    return render(
        request,
        "webhook_tg/landing.html",
        {
            "canonical_url": "https://maksonchik.ru/bot/",
            "landing_path": "/bot/",
            "telegram_bot_url": (
                tracked_telegram_url(funnel)
                if funnel is not None
                else "https://t.me/who_update_bot"
            ),
            "tracking_code": funnel.tracking_code if funnel is not None else "",
        },
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


def indexnow_key_file(request: HttpRequest):
    content = (BASE_DIR / f"{INDEXNOW_KEY}.txt").read_text(encoding="utf-8").strip()
    return HttpResponse(content, content_type="text/plain; charset=UTF-8")


def sitemap_xml(request: HttpRequest):
    return HttpResponse(
        render_sitemap_xml(),
        content_type="application/xml; charset=UTF-8",
    )


def robots_txt(request: HttpRequest):
    content = (BASE_DIR / "robots.txt").read_text(encoding="utf-8")
    return HttpResponse(content, content_type="text/plain; charset=UTF-8")


def who_update_favicon_svg(request: HttpRequest):
    content = (BASE_DIR / "who-update-favicon.svg").read_text(encoding="utf-8")
    return HttpResponse(content, content_type="image/svg+xml")
