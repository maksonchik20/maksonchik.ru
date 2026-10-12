import logging

from .models import FileType, Message
from .telegram_file_ids import is_self_destructing_photo

logger = logging.getLogger(__name__)


def remember_photo_copy(source_file_id: str, telegram_message: dict) -> None:
    """Keep Telegram's reusable ID returned by an already-authorized upload."""
    if not is_self_destructing_photo(source_file_id):
        return
    photos = telegram_message.get("photo") or []
    copy_id = photos[-1].get("file_id") if photos else None
    if not copy_id or copy_id == source_file_id or is_self_destructing_photo(copy_id):
        return
    try:
        Message.objects.filter(
            file_id=source_file_id, file_type=FileType.SELF_DESTRUCT_PHOTO
        ).update(
            saved_photo_file_id=copy_id, saved_photo_source_file_id=source_file_id
        )
    except Exception:
        # A logging/storage failure must never retry an upload already accepted
        # by Telegram. The source and copy are also kept in the outgoing log.
        logger.exception("Could not store self-destructing photo copy ID")
