from django.conf import settings

from .site_info import SCHEMA, SITE


WHO_UPDATE_HOSTS = frozenset({"who-update.ru", "www.who-update.ru"})


def site(request):
    host = request.get_host().split(":", 1)[0].lower()
    if host not in WHO_UPDATE_HOSTS:
        return {"site": SITE, "schema": SCHEMA}

    who_update_site = {
        **SITE,
        "name": "WhoUpdate",
        "url": "https://who-update.ru/",
        "title": "WhoUpdate — удалённые и изменённые сообщения Telegram",
        "description": (
            "WhoUpdate сохраняет удалённые и изменённые сообщения Telegram "
            "после подключения бота."
        ),
        "telegram": "https://t.me/who_update_bot",
        "telegram_handle": "@who_update_bot",
        "metrika_id": settings.WHO_UPDATE_METRIKA_COUNTER_ID,
    }
    return {"site": who_update_site, "schema": SCHEMA}
