from django.core.management.base import BaseCommand, CommandError

from webhook_tg.models import UserTg, TelegramOutbox
from webhook_tg.outbox import enqueue_outbox
from webhook_tg.telegram import _telegram_post
from webhook_tg.wheel import PILOT_USER_ID, app_url


class Command(BaseCommand):
    help = "Enable the wheel menu in the owner's private chat only; never alter the global menu."

    def handle(self, *args, **options):
        if UserTg.objects.filter(user_id=PILOT_USER_ID, chat_id=PILOT_USER_ID).count() != 1:
            raise CommandError("Expected exactly one owner private-chat record")
        default_before = _telegram_post("getChatMenuButton", json={}).json()
        if not default_before.get("ok"):
            raise CommandError("Cannot verify default menu")
        result = _telegram_post("setChatMenuButton", json={
            "chat_id": PILOT_USER_ID,
            "menu_button": {"type": "web_app", "text": "🎡 Бонус", "web_app": {"url": app_url()}},
        }).json()
        if not result.get("ok"):
            raise CommandError("Telegram rejected the owner-only menu: " + str(result.get("description", "")))
        owner_menu = _telegram_post("getChatMenuButton", json={"chat_id": PILOT_USER_ID}).json()
        default_after = _telegram_post("getChatMenuButton", json={}).json()
        if default_after != default_before or owner_menu.get("result", {}).get("web_app", {}).get("url") != app_url():
            raise CommandError("Menu verification failed")
        item = enqueue_outbox(
            chat_id=PILOT_USER_ID, method=TelegramOutbox.Method.SEND_MESSAGE,
            idempotency_key="wheel-pilot-launch-v1:1394340082",
            payload={
                "text": "🎡 <b>Колесо бонусов готово к тесту</b>\n\n"
                    "Mini App включён только для тебя. У остальных пользователей ничего не изменилось.\n\n"
                    "Одно вращение в день, новая попытка в 00:00 МСК. +3 и +7 дней начисляются сразу, бесплатно. Скидка и ×2 срока действуют на одну покупку в течение 24 часов.",
                "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": [[{"text": "🎡 Открыть колесо", "web_app": {"url": app_url()}}]]},
            },
        )
        self.stdout.write(f"Owner menu verified; default menu unchanged; invitation outbox={item.pk}")
