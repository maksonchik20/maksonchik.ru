"""URL для sitemap.xml — только конечные страницы без редиректов."""

from __future__ import annotations

from .blog_articles import BLOG_ARTICLE_LIST, BLOG_INDEX
from .landing_pages import SERVICE_LANDING_SLUGS

HOST = "https://maksonchik.ru"
SITE_LASTMOD = "2026-08-23"

# Пути с 301 — в sitemap не включаем (см. main/urls.py).
SITEMAP_EXCLUDE_PATHS = frozenset(
    {
        "/for-flowers/",
        "/services/site-for-flower-store/",
    }
)


def sitemap_entries() -> list[tuple[str, str, str, str | None]]:
    """(loc, changefreq, priority, lastmod)"""
    entries: list[tuple[str, str, str, str | None]] = [
        (f"{HOST}/", "weekly", "1.0", SITE_LASTMOD),
    ]
    for slug in SERVICE_LANDING_SLUGS:
        prio = "0.95" if slug == "site-for-flower-shop" else "0.9"
        entries.append((f"{HOST}/services/{slug}/", "monthly", prio, SITE_LASTMOD))
    entries.append((BLOG_INDEX["url"], "weekly", "0.85", SITE_LASTMOD))
    for article in BLOG_ARTICLE_LIST:
        entries.append((article["url"], "monthly", "0.8", SITE_LASTMOD))
    entries.append((f"{HOST}/bot/", "weekly", "0.9", SITE_LASTMOD))
    return entries


def render_sitemap_xml() -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for loc, freq, prio, lastmod in sitemap_entries():
        lines += [
            "  <url>",
            f"    <loc>{loc}</loc>",
        ]
        if lastmod:
            lines.append(f"    <lastmod>{lastmod}</lastmod>")
        lines += [
            f"    <changefreq>{freq}</changefreq>",
            f"    <priority>{prio}</priority>",
            "  </url>",
        ]
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"
