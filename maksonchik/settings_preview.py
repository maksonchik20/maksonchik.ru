"""Isolated development settings. Never use this module for a public deployment."""
import sys
import types
from pathlib import Path

import requests

# Local preview does not read real bot credentials or send external messages.
_preview_env = types.ModuleType("env")
_preview_env.TOKEN_BOT = "local-preview"
_preview_env.OWNER_CHAT_ID = 0
sys.modules["env"] = _preview_env


def _local_http_only(*args, **kwargs):
    raise requests.ConnectionError("External HTTP is disabled in the local design preview")


requests.sessions.Session.request = _local_http_only

from .settings import *  # noqa: F401,F403,E402

DEBUG = True
SECRET_KEY = "local-design-preview-not-for-production"
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]", "testserver"]
CSRF_TRUSTED_ORIGINS = ["http://localhost:8000", "http://127.0.0.1:8000"]
CSRF_COOKIE_SECURE = False
SESSION_COOKIE_SECURE = False
SECURE_PROXY_SSL_HEADER = None
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": Path(__file__).resolve().parents[2] / ".local-preview.sqlite3",
    }
}
TEMPLATES[0]["DIRS"] = [BASE_DIR / "local_preview_templates"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
YANDEX_METRIKA_OAUTH_TOKEN = ""
YOOKASSA_SHOP_ID = ""
YOOKASSA_SECRET_KEY = ""
WHO_UPDATE_PAYMENT_WEBHOOK_TOKEN = ""
