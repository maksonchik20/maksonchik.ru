from html import escape
from pathlib import Path

from django.http import FileResponse, Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST
from django.views.generic.base import RedirectView

from .blog_articles import BLOG_ARTICLE_LIST, BLOG_INDEX, get_article
from .landing_pages import (
    ALSO_AVAILABLE,
    BUSINESS_SOLUTIONS,
    LANDING_PROCESS,
    PRICING_TIERS,
    SERVICE_LANDINGS,
    TIMELINES,
    landing_also_exclude,
    landing_also_exclude_urls,
    landing_also_extra,
    landing_pricing,
    landing_timelines,
)
from .site_info import FAQ, GUARANTEES, PROJECTS, SERVICES
from .models import Lead
from webhook_tg.config import OWNER_CHAT_ID
from webhook_tg.telegram import tg_send_message

BASE_DIR = Path(__file__).resolve().parent.parent


def favicon_svg(request: HttpRequest):
    return HttpResponse(
        (BASE_DIR / "favicon.svg").read_text(encoding="utf-8"),
        content_type="image/svg+xml",
    )


def favicon_ico(request: HttpRequest):
    path = BASE_DIR / "favicon.ico"
    if not path.is_file():
        raise Http404
    return FileResponse(path.open("rb"), content_type="image/x-icon")


def favicon_png(request: HttpRequest):
    path = BASE_DIR / "favicon-120.png"
    if not path.is_file():
        raise Http404
    return FileResponse(path.open("rb"), content_type="image/png")


def index(request: HttpRequest):
    return render(
        request,
        "main/index.html",
        {
            "projects": PROJECTS,
            "services": SERVICES,
            "faq": FAQ,
            "pricing_tiers": PRICING_TIERS,
            "business_solutions": BUSINESS_SOLUTIONS,
            "guarantees": GUARANTEES,
        },
    )


def privacy(request: HttpRequest):
    return render(request, "main/privacy.html")


@require_POST
def submit_lead(request: HttpRequest):
    name = request.POST.get("name", "").strip()
    contact = request.POST.get("contact", "").strip()
    message = request.POST.get("message", "").strip()
    consent = request.POST.get("consent")

    # Honeypot: для посетителя поле скрыто, простые боты обычно его заполняют.
    if request.POST.get("company", "").strip():
        return JsonResponse({"ok": True})
    if not name or len(name) > 120:
        return JsonResponse({"ok": False, "error": "Укажите ваше имя."}, status=400)
    if not contact or len(contact) > 255:
        return JsonResponse({"ok": False, "error": "Укажите телефон, email или Telegram."}, status=400)
    if len(message) > 3000:
        return JsonResponse({"ok": False, "error": "Описание задачи должно быть короче 3000 символов."}, status=400)
    if consent != "on":
        return JsonResponse({"ok": False, "error": "Нужно согласие на обработку данных."}, status=400)

    lead = Lead.objects.create(
        name=name,
        contact=contact,
        message=message,
        page_url=request.POST.get("page_url", "")[:1000],
        page_title=request.POST.get("page_title", "")[:300],
        utm_source=request.POST.get("utm_source", "")[:255],
        utm_medium=request.POST.get("utm_medium", "")[:255],
        utm_campaign=request.POST.get("utm_campaign", "")[:255],
        utm_content=request.POST.get("utm_content", "")[:255],
        utm_term=request.POST.get("utm_term", "")[:255],
    )

    telegram_text = (
        "<b>Новая заявка с maksonchik.ru</b>\n\n"
        f"<b>Имя:</b> {escape(name)}\n"
        f"<b>Контакт:</b> {escape(contact)}\n"
        f"<b>Задача:</b> {escape(message or 'Не указана')}\n"
        f"<b>Страница:</b> {escape(lead.page_url or 'Не определена')}\n"
        f"<b>UTM campaign:</b> {escape(lead.utm_campaign or '—')}"
    )
    try:
        lead.notification_sent = tg_send_message(OWNER_CHAT_ID, telegram_text, timeout=8)
        if not lead.notification_sent:
            lead.notification_error = "Telegram API не подтвердил отправку"
    except Exception as exc:
        lead.notification_error = str(exc)[:1000]
    lead.save(update_fields=("notification_sent", "notification_error"))

    return JsonResponse({"ok": True, "lead_id": lead.pk})


def service_landing(request: HttpRequest, slug: str):
    page = SERVICE_LANDINGS.get(slug)
    if page is None:
        raise Http404
    also_available = []
    exclude = landing_also_exclude(page)
    exclude_urls = landing_also_exclude_urls(page)
    seen_urls: set[str] = set()
    for item in ALSO_AVAILABLE + landing_also_extra(page):
        item_slug = item.get("slug")
        if item_slug and item_slug == slug:
            continue
        if item_slug and item_slug in exclude:
            continue
        if item_slug:
            url = f"/services/{item_slug}/"
        else:
            url = item["url"]
        if url in exclude_urls or url in seen_urls:
            continue
        seen_urls.add(url)
        also_available.append({"url": url, "label": item["label"]})

    return render(
        request,
        "main/service_landing.html",
        {
            "page": page,
            "pricing_tiers": landing_pricing(page),
            "timelines": landing_timelines(page),
            "process": LANDING_PROCESS,
            "also_available": also_available,
        },
    )


class ForFlowersRedirect(RedirectView):
    permanent = True
    url = "/services/site-for-flower-shop/"


def blog_index(request: HttpRequest):
    business = [a for a in BLOG_ARTICLE_LIST if a["category"] == "business"]
    niche = [a for a in BLOG_ARTICLE_LIST if a["category"] == "niche"]
    process = [a for a in BLOG_ARTICLE_LIST if a["category"] == "process"]
    return render(
        request,
        "main/blog_index.html",
        {
            "blog_index": BLOG_INDEX,
            "business_articles": business,
            "niche_articles": niche,
            "process_articles": process,
        },
    )


def blog_article(request: HttpRequest, slug: str):
    article = get_article(slug)
    if article is None:
        raise Http404
    return render(
        request,
        "main/blog_article.html",
        {"article": article},
    )
