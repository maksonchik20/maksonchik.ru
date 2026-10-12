from __future__ import annotations

import logging

from django.utils.html import strip_tags

from .models import BotChatEvent, BotOutgoingMessage, UserTg

logger = logging.getLogger(__name__)


METHOD_EVENT_TYPES = {
    "sendMessage": BotChatEvent.EventType.MESSAGE,
    "editMessageText": BotChatEvent.EventType.EDITED_MESSAGE,
    "sendPhoto": BotChatEvent.EventType.PHOTO,
    "sendVideo": BotChatEvent.EventType.VIDEO,
    "sendAudio": BotChatEvent.EventType.AUDIO,
    "sendDocument": BotChatEvent.EventType.DOCUMENT,
    "sendMediaGroup": BotChatEvent.EventType.MEDIA_GROUP,
}


def _outgoing_text(method: str, payload: dict) -> str:
    text = payload.get("text") or payload.get("caption") or ""
    if method == "sendMediaGroup":
        captions = [item.get("caption") for item in payload.get("media", []) if item.get("caption")]
        text = "\n\n".join(captions) or f"Альбом: {len(payload.get('media', []))} файлов"
    return strip_tags(str(text)).strip()


def _telegram_message_id(result) -> int | None:
    if isinstance(result, dict):
        return result.get("message_id")
    if isinstance(result, list) and result and isinstance(result[0], dict):
        return result[0].get("message_id")
    return None


def _telegram_media(result) -> list[dict]:
    """Keep reusable attachment IDs from sent messages, not the full API reply."""
    telegram_messages = result if isinstance(result, list) else [result]
    media = []

    def positive_number(value):
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else 0

    for message in telegram_messages:
        if not isinstance(message, dict):
            continue
        attachment = None
        file_type = None
        photos = message.get("photo")
        if isinstance(photos, list):
            candidates = [
                (index, photo)
                for index, photo in enumerate(photos)
                if isinstance(photo, dict)
                and isinstance(photo.get("file_id"), str)
                and photo["file_id"].strip()
            ]
            if candidates:
                # Telegram normally orders sizes from small to large. Use the
                # dimensions when present; the last valid size is the fallback.
                _, attachment = max(
                    candidates,
                    key=lambda item: (
                        positive_number(item[1].get("width"))
                        * positive_number(item[1].get("height")),
                        positive_number(item[1].get("file_size")),
                        item[0],
                    ),
                )
                file_type = "PHOTO"
        if attachment is None:
            # Animation also includes a document field in Bot API responses.
            # Prefer the specific media type so it does not become two files.
            for field, candidate_type in (
                ("animation", "ANIMATION"),
                ("video", "VIDEO"),
                ("video_note", "VIDEO"),
                ("audio", "AUDIO"),
                ("voice", "AUDIO"),
                ("sticker", "STICKER"),
                ("document", "DOCUMENT"),
            ):
                candidate = message.get(field)
                if (
                    isinstance(candidate, dict)
                    and isinstance(candidate.get("file_id"), str)
                    and candidate["file_id"].strip()
                ):
                    attachment = candidate
                    file_type = candidate_type
                    break
        if attachment is None:
            continue
        item = {"file_id": attachment["file_id"].strip(), "file_type": file_type}
        if isinstance(attachment.get("file_name"), str) and attachment["file_name"]:
            item["file_name"] = attachment["file_name"]
        media.append(item)
    return media


def log_bot_outgoing(*, chat_id, method: str, payload: dict | None = None, result=None) -> None:
    if not chat_id:
        return
    try:
        BotOutgoingMessage.objects.create(chat_id=chat_id, method=method)
        # В журнал диалога попадают только личные чаты известных пользователей бота.
        if UserTg.objects.filter(chat_id=chat_id).exists():
            safe_payload = dict(payload or {})
            telegram_media = _telegram_media(result)
            if telegram_media:
                safe_payload.setdefault("telegram_media", telegram_media)
            BotChatEvent.objects.create(
                chat_id=chat_id,
                direction=BotChatEvent.Direction.BOT,
                event_type=METHOD_EVENT_TYPES.get(method, BotChatEvent.EventType.OTHER),
                text=_outgoing_text(method, safe_payload),
                payload=safe_payload,
                telegram_message_id=_telegram_message_id(result),
            )
    except Exception:
        logger.exception("Failed to log outgoing message chat_id=%s method=%s", chat_id, method)


def _callback_button_text(callback: dict) -> str:
    callback_data = callback.get("data") or ""
    keyboard = ((callback.get("message") or {}).get("reply_markup") or {}).get("inline_keyboard") or []
    for row in keyboard:
        for button in row:
            if button.get("callback_data") == callback_data and button.get("text"):
                return str(button["text"])
    return str(callback_data)


def _incoming_type(message: dict, *, edited: bool = False) -> str:
    if edited:
        return BotChatEvent.EventType.EDITED_MESSAGE
    for key, event_type in (
        ("photo", BotChatEvent.EventType.PHOTO),
        ("video", BotChatEvent.EventType.VIDEO),
        ("audio", BotChatEvent.EventType.AUDIO),
        ("voice", BotChatEvent.EventType.AUDIO),
        ("document", BotChatEvent.EventType.DOCUMENT),
    ):
        if message.get(key):
            return event_type
    return BotChatEvent.EventType.MESSAGE


def log_bot_incoming(data: dict) -> None:
    """Записывает только личные сообщения и callback-и, не Business-переписку."""
    try:
        update_id = data.get("update_id")
        source_key = f"telegram-update:{update_id}" if update_id is not None else None
        callback = data.get("callback_query")
        if callback:
            chat_id = ((callback.get("message") or {}).get("chat") or {}).get("id")
            if not chat_id:
                return
            label = _callback_button_text(callback)
            values = {
                    "chat_id": chat_id,
                    "direction": BotChatEvent.Direction.USER,
                    "event_type": BotChatEvent.EventType.CALLBACK,
                    "text": f"Нажата кнопка: {label}" if label else "Нажата кнопка",
                    "payload": callback,
                    "update_id": update_id,
                }
            if source_key:
                BotChatEvent.objects.get_or_create(source_key=source_key, defaults=values)
            else:
                BotChatEvent.objects.create(**values)
            return

        edited = bool(data.get("edited_message"))
        message = data.get("edited_message") or data.get("message")
        if not message:
            return
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if not chat_id or chat.get("type") not in (None, "private"):
            return
        text = message.get("text") or message.get("caption") or ""
        values = {
                "chat_id": chat_id,
                "direction": BotChatEvent.Direction.USER,
                "event_type": _incoming_type(message, edited=edited),
                "text": str(text),
                "payload": message,
                "telegram_message_id": message.get("message_id"),
                "update_id": update_id,
            }
        if source_key:
            BotChatEvent.objects.get_or_create(source_key=source_key, defaults=values)
        else:
            BotChatEvent.objects.create(**values)
    except Exception:
        logger.exception("Failed to log incoming bot chat update_id=%s", data.get("update_id"))
