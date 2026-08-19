from pathlib import Path

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from main.sitemap_urls import render_sitemap_xml


BASE_DIR = Path(__file__).resolve().parent.parent
INDEXNOW_KEY = "qIXnCp99XqCIbkmFQv6mWaNweY2n1fio"


def who_update_landing(request: HttpRequest):
    return render(
        request,
        "webhook_tg/landing.html",
        {
            "canonical_url": "https://maksonchik.ru/bot/",
            "landing_path": "/bot/",
        },
    )


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
