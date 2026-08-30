from pathlib import Path
import os
from datetime import timedelta

BASE_DIR = Path(__file__).resolve().parent.parent


SECRET_KEY = 'django-insecure-+gt-2@0c5ty*pbm-c@ohq&f^r*%19e&4yv&f7qucq^#_i)8=ru'

DEBUG = False

ALLOWED_HOSTS = [
    "maksonchik.ru",
    "www.maksonchik.ru",
    "who-update.ru",
    "www.who-update.ru",
    "158.160.136.81",
    "213.226.124.52",
    "localhost",
    "127.0.0.1",
]

# HTTPS завершается на reverse proxy. Заголовок выставляется только SSL-vhost,
# поэтому Django может безопасно определить схему исходного запроса.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
CSRF_TRUSTED_ORIGINS = [
    "https://maksonchik.ru",
    "https://www.maksonchik.ru",
    "https://who-update.ru",
    "https://www.who-update.ru",
]
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_SECURE = True

WHO_UPDATE_SITE_URL = "https://who-update.ru"
WHO_UPDATE_TRIAL_DAYS = int(os.environ.get("WHO_UPDATE_TRIAL_DAYS", "7"))
WHO_UPDATE_METRIKA_COUNTER_ID = int(
    os.environ.get("WHO_UPDATE_METRIKA_COUNTER_ID", "112093587")
)
# Офлайн-конверсии относятся только к WhoUpdate и загружаются в его счётчик.
YANDEX_METRIKA_COUNTER_ID = int(
    os.environ.get("YANDEX_METRIKA_COUNTER_ID", str(WHO_UPDATE_METRIKA_COUNTER_ID))
)
YANDEX_METRIKA_OAUTH_TOKEN = os.environ.get("YANDEX_METRIKA_OAUTH_TOKEN", "")
try:
    from env import (
        YOOKASSA_SHOP_ID,
        YOOKASSA_SECRET_KEY,
        WHO_UPDATE_PAYMENT_WEBHOOK_TOKEN,
    )
except ImportError:
    YOOKASSA_SHOP_ID = ""
    YOOKASSA_SECRET_KEY = ""
    WHO_UPDATE_PAYMENT_WEBHOOK_TOKEN = ""

try:
    from env import YANDEX_METRIKA_OAUTH_TOKEN as ENV_YANDEX_METRIKA_OAUTH_TOKEN
except ImportError:
    pass
else:
    YANDEX_METRIKA_OAUTH_TOKEN = ENV_YANDEX_METRIKA_OAUTH_TOKEN


INSTALLED_APPS = [
    'axes',

    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    'webhook_tg.apps.WebhookTgConfig',
    'main.apps.MainConfig',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'maksonchik.host_routing.HostURLConfMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',

    "axes.middleware.AxesMiddleware",
]

AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]

AXES_ONLY_ADMIN_SITE = True
AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = lambda request: timedelta(minutes=5)
AXES_RESET_ON_SUCCESS = True

AXES_LOCKOUT_PARAMETERS = ["username", "ip_address"]

ROOT_URLCONF = 'maksonchik.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'main.context_processors.site',
            ],
        },
    },
]

WSGI_APPLICATION = 'maksonchik.wsgi.application'


DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

LANGUAGE_CODE = 'ru-ru'

TIME_ZONE = 'Europe/Moscow'

USE_I18N = True

USE_TZ = True

# Telegram webhook быстро сохраняет update в БД; обработка идёт отдельными workers.
TELEGRAM_WEBHOOK_SYNC_PROCESSING = False
TELEGRAM_WEBHOOK_SECRET_REQUIRED = True


STATIC_URL = '/static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'static')
WHO_UPDATE_DEMO_MEDIA_ROOT = BASE_DIR.parent / "demo_media"
