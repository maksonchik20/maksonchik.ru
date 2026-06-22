import hashlib
import logging

from django.db import IntegrityError, transaction

from .models import EditNotificationSent, WebhookUpdate

logger = logging.getLogger(__name__)


def acquire_webhook_update(update_id) -> bool:
    """
    Регистрирует update_id Telegram. Возвращает True, если апдейт новый и его нужно обработать.
    False — апдейт уже обрабатывался (идемпотентный пропуск).
    """
    if update_id is None:
        logger.warning("Webhook без update_id — обрабатываем без идемпотентности")
        return True

    try:
        with transaction.atomic():
            WebhookUpdate.objects.create(update_id=update_id)
        return True
    except IntegrityError:
        return False


def is_edit_notification_sent(msg) -> bool:
    fr = msg.get("from") or {}
    editor_id = fr.get("id")
    edit_date = msg.get("edit_date")
    if editor_id is None or edit_date is None:
        return False

    text_hash = hashlib.sha256((msg.get("text") or "").encode()).hexdigest()[:16]
    return EditNotificationSent.objects.filter(
        editor_id=editor_id,
        edit_date=edit_date,
        text_hash=text_hash,
    ).exists()


def mark_edit_notification_sent(msg) -> None:
    """Записывает успешную отправку. Вызывается только после ok от Telegram."""
    fr = msg.get("from") or {}
    editor_id = fr.get("id")
    edit_date = msg.get("edit_date")
    if editor_id is None or edit_date is None:
        return

    text_hash = hashlib.sha256((msg.get("text") or "").encode()).hexdigest()[:16]
    try:
        with transaction.atomic():
            EditNotificationSent.objects.create(
                editor_id=editor_id,
                edit_date=edit_date,
                text_hash=text_hash,
            )
    except IntegrityError:
        pass
