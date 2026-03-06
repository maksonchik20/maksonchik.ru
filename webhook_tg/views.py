from django.http import HttpResponse, HttpRequest
from django.views.decorators.csrf import csrf_exempt
import json
from .models import UserTg, Message, FileType
import html
from .telegram import (
    tg_send_message,
    get_business_connection,
    send_photo,
    send_audio,
    send_video,
    send_document,
)
from .config import START_PHOTO_ID, START_TEXT, OWNER_CHAT_ID


@csrf_exempt
def index(request: HttpRequest):
    return HttpResponse(f"maksonchik website Xo-Xo")

@csrf_exempt
def webhook_tg(request: HttpRequest):
    try:
        data = json.loads(request.body.decode("utf-8"))
        print(data)
        msg = data.get("business_message") or data.get("message") or data.get("edited_message") or \
              data.get("edited_business_message") or data.get("deleted_business_messages") or data.get("deleted_messages") or {}
        text = msg.get("text")
        if text is None and not is_deleted_message(data):
            print("СООБЩЕНИЕ БЕЗ ТЕКСТА")
            if is_edited_message(data) or is_new_message(data):
                create_message(msg)
            return HttpResponse("Success")
        from_user_id = msg.get("from", {}).get("id")
        chat_id = msg.get("chat", {}).get("id")
        username = msg.get("from", {}).get("username")
        first_name = msg.get("from", {}).get("first_name")
        if text == "/start" and is_message_to_bot(data):
            init_user_bot(user_id=from_user_id, chat_id=chat_id, username=username, first_name=first_name)
            send_meeting_message(chat_id)
        elif is_edited_message(data):
            business_connection = get_business_connection(msg)
            if (business_connection.user_chat_id != chat_id):
                tg_send_message(chat_id=business_connection.user_chat_id, text=build_message_update(msg))
        elif is_deleted_message(data):
            business_connection = get_business_connection(msg)
            if (business_connection.user_chat_id != chat_id):
                _send_deleted_notifications(msg, business_connection.user_chat_id)
        if is_edited_message(data) or is_new_message(data):
            create_message(msg)


        print(f"text: {text}")
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        print(f"Bad JSON: {e}")
        pass
    except NotImplementedError as e:
        pass
    
    return HttpResponse(f"Success")

def send_meeting_message(chat_id):
    send_photo(chat_id=chat_id, photo_id=START_PHOTO_ID, caption=START_TEXT)


def _extract_file_data(msg):
    """
    Извлекает file_id, file_type и caption из payload сообщения.
    В payload приходит максимум один тип файла. У фото берётся последний file_id из списка.
    """
    file_id = None
    file_type = FileType.UNKNOWN
    caption = msg.get("caption", None)

    if "photo" in msg and msg["photo"]:
        file_type = FileType.PHOTO
        file_id = msg["photo"][-1].get("file_id")
    elif "voice" in msg:
        file_type = FileType.AUDIO
        file_id = msg["voice"].get("file_id")
    elif "audio" in msg:
        file_type = FileType.AUDIO
        file_id = msg["audio"].get("file_id")
    elif "video" in msg:
        file_type = FileType.VIDEO
        file_id = msg["video"].get("file_id")
    elif "video_note" in msg:
        file_type = FileType.VIDEO
        file_id = msg["video_note"].get("file_id")
    elif "document" in msg:
        file_type = FileType.DOCUMENT
        file_id = msg["document"].get("file_id")

    return file_id, file_type, caption


def create_message(msg):
    message_id = msg.get("message_id")
    file_id, file_type, caption = _extract_file_data(msg)
    text = msg.get("text")
    if text is None and caption:
        text = caption
    if text is None:
        text = ""

    try:
        m = Message.objects.get(message_id=message_id)
        m.text = text
        m.file_id = file_id
        m.file_type = file_type or FileType.UNKNOWN
        m.caption = caption
        m.payload = str(msg)
        m.save(update_fields=["text", "file_id", "file_type", "caption", "payload"])
    except Message.DoesNotExist:
        business_connection_id = msg.get("business_connection_id")
        username_from = msg.get("from", {}).get("username")
        first_name = msg.get("from", {}).get("first_name")
        chat_id = msg.get("chat", {}).get("id")
        m = Message.objects.create(
            business_connection_id=business_connection_id,
            message_id=message_id,
            username_from=username_from,
            first_name=first_name,
            chat_id=chat_id,
            text=text,
            file_id=file_id,
            file_type=file_type or FileType.UNKNOWN,
            caption=caption,
            payload=str(msg),
        )

def _build_deleted_caption(deleted: dict, message_id: int, text: str) -> str:
    """Текст уведомления об удалении: кто удалил, id сообщения, содержимое."""
    chat = deleted.get("chat") or {}
    first_name = chat.get("first_name") or "Unknown"
    username = chat.get("username")
    user_part = html.escape(first_name)
    if username:
        user_part += f" (@{html.escape(username)})"
    old_text = text or "(текст не сохранён)"
    return (
        f"{user_part} удалил(а) сообщение (id={message_id}):\n"
        f"<blockquote>{html.escape(old_text)}</blockquote>"
    )


def _send_file_by_type(chat_id, file_id: str, file_type: str, caption: str) -> None:
    """Отправляет файл в чат в зависимости от file_type (PHOTO, AUDIO, VIDEO, DOCUMENT)."""
    if file_type == FileType.PHOTO:
        send_photo(chat_id, file_id, caption=caption)
    elif file_type == FileType.AUDIO:
        send_audio(chat_id, file_id, caption=caption)
    elif file_type == FileType.VIDEO:
        send_video(chat_id, file_id, caption=caption)
    elif file_type == FileType.DOCUMENT:
        send_document(chat_id, file_id, caption=caption)
    else:
        tg_send_message(chat_id, caption)


def _send_deleted_notifications(deleted: dict, user_chat_id) -> None:
    """
    Находит удалённые сообщения в БД по message_ids и отправляет пользователю:
    — если у сообщения есть file_id и тип медиа: отправляет файл (photo/audio/video/document) с подписью;
    — иначе: отправляет текстовое уведомление.
    Максимум 20 таких отправок, затем одно сообщение «больше 20 удалено».
    """
    chat = deleted.get("chat") or {}
    business_connection_id = deleted.get("business_connection_id")
    chat_id = chat.get("id")
    msg_ids = deleted.get("message_ids") or []
    first_name = chat.get("first_name") or "Unknown"
    username = chat.get("username")
    user_part = html.escape(first_name)
    if username:
        user_part += f" (@{html.escape(username)})"

    if not msg_ids:
        tg_send_message(user_chat_id, f"{user_part} удалил(а) сообщения (ids не пришли).")
        return

    known = Message.objects.filter(
        message_id__in=msg_ids,
        business_connection_id=business_connection_id,
        chat_id=chat_id,
    )
    known_map = {m.message_id: m for m in known}

    for mid in msg_ids[:20]:
        m = known_map.get(mid)
        caption = _build_deleted_caption(deleted, mid, m.text if m else None)
        if m and m.file_id and m.file_type and m.file_type != FileType.UNKNOWN:
            _send_file_by_type(user_chat_id, m.file_id, m.file_type, caption)
        else:
            tg_send_message(user_chat_id, caption)

    if len(msg_ids) > 20:
        tg_send_message(user_chat_id, f"Было удалено больше 20 сообщений (всего {len(msg_ids)}).")


def _build_deleted_message_parts(deleted: dict) -> list[str]:
    """
    Формирует список строк для отправки: до 20 отдельных сообщений об удалённых,
    затем одно сообщение о том, что удалено больше 20. (Используется для тестов и build_message_delete.)
    """
    chat = deleted.get("chat") or {}
    first_name = chat.get("first_name") or "Unknown"
    username = chat.get("username")
    user_part = html.escape(first_name)
    if username:
        user_part += f" (@{html.escape(username)})"

    msg_ids = deleted.get("message_ids") or []
    if not msg_ids:
        return [f"{user_part} удалил(а) сообщения (ids не пришли)."]

    business_connection_id = deleted.get("business_connection_id")
    chat_id = deleted.get("chat", {}).get("id")
    known = Message.objects.filter(
        message_id__in=msg_ids,
        business_connection_id=business_connection_id,
        chat_id=chat_id,
    )
    known_map = {m.message_id: (m.text or "") for m in known}

    parts = []
    for mid in msg_ids[:20]:
        old_text = known_map.get(mid) or "(текст не сохранён)"
        parts.append(
            f"{user_part} удалил(а) сообщение (id={mid}):\n"
            f"<blockquote>{html.escape(old_text)}</blockquote>"
        )
    if len(msg_ids) > 20:
        parts.append(f"Было удалено больше 20 сообщений (всего {len(msg_ids)}).")
    return parts


def build_message_delete(deleted: dict) -> str:
    """Один общий текст (для обратной совместимости)."""
    parts = _build_deleted_message_parts(deleted)
    return "\n\n".join(parts)

def build_message_update(msg):
    fr = msg.get("from") or {}
    first_name = fr.get("first_name") or "Unknown"
    username = fr.get("username")

    message_id = msg.get("message_id")
    old = Message.objects.filter(message_id=message_id).first()
    old_text = old.text if old is not None else "(Это сообщение было написано до подключения бота)"
    new_text = msg.get("text") or ""

    user_part = html.escape(first_name)
    if username:
        user_part += f" (@{html.escape(username)})"

    return (
        f"{user_part} изменил(а) сообщение:\n\n"
        f"<b>Old:</b>\n<blockquote>{html.escape(old_text)}</blockquote>\n"
        f"<b>New:</b>\n<blockquote>{html.escape(new_text)}</blockquote>\n\n"
        f"<b>@{html.escape('who_update_bot')}</b>"
    )

def init_user_bot(user_id: int, chat_id: int, username: str, first_name: str):
    user, created = UserTg.objects.get_or_create(
        user_id=user_id,
        defaults={
            "chat_id": chat_id,
            "username": username or "",
            "first_name": first_name or "",
        }
    )

    if created:
        tg_send_message(OWNER_CHAT_ID, f"New user: @{username or '-'} {first_name or ''} (id={user_id})")
    else:
        updated = False
        if user.chat_id != chat_id:
            user.chat_id = chat_id
            updated = True
        if (user.username or "") != (username or ""):
            user.username = username or ""
            updated = True
        if (user.first_name or "") != (first_name or ""):
            user.first_name = first_name or ""
            updated = True
        if updated:
            user.save(update_fields=["chat_id", "username", "first_name"])

def isBusiness(data):
    return data.get("business_message") is not None or data.get("edited_business_message") is not None

def is_message_to_bot(data):
    return data.get("message") is not None or data.get("edited_message") is not None

def is_edited_message(data):
    return data.get("edited_message") is not None or data.get("edited_business_message") is not None

def is_new_message(data):
    return data.get("message") is not None or data.get("business_message") is not None

def is_deleted_message(data):
    return data.get("deleted_business_messages") is not None or data.get("deleted_messages") is not None
