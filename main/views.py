from pathlib import Path

from django.http import FileResponse, Http404, HttpRequest, HttpResponse
from django.shortcuts import render
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
