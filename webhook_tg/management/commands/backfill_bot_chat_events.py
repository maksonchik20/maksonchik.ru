from django.core.management.base import BaseCommand
from django.utils import timezone

from webhook_tg.bot_outgoing_log import METHOD_EVENT_TYPES, log_bot_incoming
from webhook_tg.models import BotChatEvent, BotOutgoingMessage, TelegramIncomingUpdate, UserTg


class Command(BaseCommand):
    help = "Заполняет журнал диалогов сохранёнными ранее Telegram update и фактами отправки"

    def handle(self, *args, **options):
        started_at = timezone.now()
        incoming_before = BotChatEvent.objects.filter(
            direction=BotChatEvent.Direction.USER
        ).count()
        last_incoming_id = 0
        while True:
            # Сначала закрываем SELECT, затем пишем: SQLite не позволяет
            # обновлять таблицу при открытом streaming cursor.
            items = list(
                TelegramIncomingUpdate.objects.filter(pk__gt=last_incoming_id)
                .order_by("pk")[:500]
            )
            if not items:
                break
            for item in items:
                log_bot_incoming(item.payload)
                if item.update_id is not None:
                    BotChatEvent.objects.filter(
                        source_key=f"telegram-update:{item.update_id}"
                    ).update(created_at=item.created_at)
            last_incoming_id = items[-1].pk

        known_chat_ids = set(UserTg.objects.values_list("chat_id", flat=True))
        outgoing_added = 0
        old_outgoing = BotOutgoingMessage.objects.filter(
            chat_id__in=known_chat_ids,
            sent_at__lt=started_at,
        ).order_by("sent_at")
        last_outgoing_id = 0
        while True:
            items = list(old_outgoing.filter(pk__gt=last_outgoing_id).order_by("pk")[:500])
            if not items:
                break
            for item in items:
                event, created = BotChatEvent.objects.get_or_create(
                    source_key=f"legacy-outgoing:{item.pk}",
                    defaults={
                        "chat_id": item.chat_id,
                        "direction": BotChatEvent.Direction.BOT,
                        "event_type": METHOD_EVENT_TYPES.get(
                            item.method,
                            BotChatEvent.EventType.OTHER,
                        ),
                        "text": (
                            f"[{item.method}] Содержимое не сохранялось "
                            "до включения журнала"
                        ),
                        "payload": {"method": item.method, "legacy": True},
                    },
                )
                if created:
                    BotChatEvent.objects.filter(pk=event.pk).update(created_at=item.sent_at)
                    outgoing_added += 1
            last_outgoing_id = items[-1].pk

        incoming_added = (
            BotChatEvent.objects.filter(direction=BotChatEvent.Direction.USER).count()
            - incoming_before
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Добавлено входящих событий: {incoming_added}; "
                f"исторических исходящих: {outgoing_added}"
            )
        )
