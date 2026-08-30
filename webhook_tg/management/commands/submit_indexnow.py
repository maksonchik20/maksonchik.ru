"""Отправка URL в Яндекс IndexNow.

Примеры:
  python manage.py submit_indexnow
  python manage.py submit_indexnow https://who-update.ru/
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

import requests
from django.core.management.base import BaseCommand

from webhook_tg.config import INDEXNOW_ENDPOINT, INDEXNOW_KEY


from main.sitemap_urls import sitemap_entries

DEFAULT_URLS = [entry[0] for entry in sitemap_entries()]


class Command(BaseCommand):
    help = "Submit site URLs to Yandex IndexNow API."

    def add_arguments(self, parser):
        parser.add_argument(
            "urls",
            nargs="*",
            help="URLs to submit (default: maksonchik.ru sitemap URLs)",
        )

    def handle(self, *args, **options):
        urls = options["urls"] or DEFAULT_URLS
        hosts = {urlparse(url).hostname for url in urls}
        if None in hosts or len(hosts) != 1:
            self.stderr.write(self.style.ERROR("All IndexNow URLs must use one host"))
            return
        host = hosts.pop()
        payload = {
            "host": host,
            "key": INDEXNOW_KEY,
            "urlList": list(urls),
        }
        self.stdout.write(f"POST {INDEXNOW_ENDPOINT}")
        self.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
        response = requests.post(
            INDEXNOW_ENDPOINT,
            json=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=30,
        )
        self.stdout.write(f"HTTP {response.status_code}")
        body = (response.text or "").strip()
        if body:
            self.stdout.write(body[:1000])
        if response.status_code in (200, 202):
            self.stdout.write(self.style.SUCCESS("IndexNow accepted"))
        else:
            self.stderr.write(self.style.ERROR("IndexNow failed"))
