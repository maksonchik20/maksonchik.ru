import logging

from .models import BotOutgoingMessage

logger = logging.getLogger(__name__)


def log_bot_outgoing(*, chat_id, method: str) -> None:
    if not chat_id:
        return
    try:
        BotOutgoingMessage.objects.create(chat_id=chat_id, method=method)
    except Exception:
        logger.exception("Failed to log outgoing message chat_id=%s method=%s", chat_id, method)
