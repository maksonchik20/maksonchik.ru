from django.db.models import Q
from django.utils import timezone

from .models import Message, TelegramIncomingUpdate


def mark_messages_deleted(deleted: dict, *, received_at=None, update_id=None) -> int:
    """Mark archived messages only from this Telegram chat and connection.

The event has no deletion timestamp. Keep its earliest known receipt time;
never infer deletion from an unavailable Telegram file or subscription state.
"""
    if not isinstance(deleted, dict):
        return 0
    chat = deleted.get("chat") or {}
    chat_id = chat.get("id") if isinstance(chat, dict) else None
    connection_id = deleted.get("business_connection_id")
    message_ids = deleted.get("message_ids")
    if (
        type(chat_id) is not int
        or not isinstance(connection_id, str) or not connection_id
        or not isinstance(message_ids, list)
    ):
        return 0
    ids = sorted({mid for mid in message_ids if type(mid) is int and mid > 0})
    if not ids:
        return 0
    if received_at is None and update_id is not None:
        received_at = (
            TelegramIncomingUpdate.objects.filter(update_id=update_id)
            .values_list("created_at", flat=True).first()
        )
    received_at = received_at or timezone.now()
    count = 0
    for offset in range(0, len(ids), 500):
        count += Message.objects.filter(
            chat_id=chat_id, business_connection_id=connection_id,
            message_id__in=ids[offset:offset + 500],
        ).filter(
            Q(is_deleted=False) | Q(deleted_at__isnull=True) | Q(deleted_at__gt=received_at)
        ).update(is_deleted=True, deleted_at=received_at)
    return count
