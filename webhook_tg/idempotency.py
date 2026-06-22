import logging

from django.db import IntegrityError, transaction

from .models import WebhookUpdate

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
