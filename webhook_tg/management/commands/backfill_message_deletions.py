from django.core.management.base import BaseCommand
from django.db.models import Q

from webhook_tg.message_deletions import mark_messages_deleted
from webhook_tg.models import TelegramIncomingUpdate


class Command(BaseCommand):
    help = "Отмечает удалённые сообщения по сохранённым событиям Telegram без отправки уведомлений."

    def handle(self, *args, **options):
        last_id = events = updated = 0
        while True:
            batch = list(
                TelegramIncomingUpdate.objects.filter(pk__gt=last_id).filter(
                    Q(payload__has_key="deleted_business_messages") | Q(payload__has_key="deleted_messages")
                ).order_by("pk").only("pk", "payload", "created_at")[:500]
            )
            if not batch:
                break
            for event in batch:
                deleted = event.payload.get("deleted_business_messages") or event.payload.get("deleted_messages")
                updated += mark_messages_deleted(deleted, received_at=event.created_at)
            events += len(batch)
            last_id = batch[-1].pk
            self.stdout.write(f"Событий обработано: {events}; записей обновлено: {updated}")
