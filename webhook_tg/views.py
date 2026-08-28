from __future__ import annotations

from django.conf import settings
from django.http import HttpResponse, HttpRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
import hmac
import json
import logging
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from django.utils import timezone
from .models import UserTg, Message, FileType, TelegramOutbox
from .mute import (
    handle_mute_callback,
    handle_mute_commands,
    is_username_muted,
    maybe_delete_muted_business_message,
)
import html
from .telegram import (
    tg_send_message,
    get_business_connection,
    send_photo,
    send_audio,
    send_video,
    send_document,
    send_document_bytes,
    send_photo_bytes,
    send_video_bytes,
    download_telegram_file_bytes,
    _guess_media_filename,
)
from .config import (
    START_PHOTO_ID,
    START_TEXT,
    BOT_ACTIVATED_TEXT,
    BOT_DEACTIVATED_TEXT,
    OWNER_CHAT_ID,
    ALLOWED_SEND_CHAT_IDS,
)
from .inner_models.BusinessConnection import BusinessConnection
from .idempotency import acquire_webhook_update
from .incoming import enqueue_incoming_update
from .outbox import enqueue_outbox, edit_notification_dedup_key
from .event_reporter import report_who_update_event
from .events_chart import parse_events_period, PERIOD_HELP
from .telegram import telegram_webhook_secret
from .subscriptions import (
    access_status_text,
    apply_rollout_policy,
    business_access_allowed,
    grant_referral_reward,
    referral_text,
    register_referral,
    start_trial_if_needed,
    subscription_keyboard,
)


logger = logging.getLogger(__name__)


def who_update_landing(request: HttpRequest):
    """WhoUpdate SEO-лендинг: /bot/ и /who-update-bot/."""
    landing_path = request.path if request.path.endswith("/") else f"{request.path}/"
    canonical_url = f"https://maksonchik.ru{landing_path}"
    return render(
        request,
        "webhook_tg/landing.html",
        {
            "canonical_url": canonical_url,
            "landing_path": landing_path,
        },
    )


def yandex_webmaster_verify(request: HttpRequest):
    """Файл подтверждения Яндекс.Вебмастера в корне сайта."""
    path = Path(__file__).resolve().parent.parent / "yandex_1cf6644d705ed152.html"
    return HttpResponse(path.read_text(encoding="utf-8"), content_type="text/html; charset=UTF-8")


def indexnow_key_file(request: HttpRequest):
    """Ключ IndexNow в корне сайта (UTF-8, без HTML)."""
    from .config import INDEXNOW_KEY

    path = Path(__file__).resolve().parent.parent / f"{INDEXNOW_KEY}.txt"
    return HttpResponse(path.read_text(encoding="utf-8").strip(), content_type="text/plain; charset=UTF-8")


def sitemap_xml(request: HttpRequest):
    """Sitemap для поисковиков."""
    path = Path(__file__).resolve().parent.parent / "sitemap.xml"
    return HttpResponse(
        path.read_text(encoding="utf-8"),
        content_type="application/xml; charset=UTF-8",
    )


def robots_txt(request: HttpRequest):
    path = Path(__file__).resolve().parent.parent / "robots.txt"
    return HttpResponse(
        path.read_text(encoding="utf-8"),
        content_type="text/plain; charset=UTF-8",
    )


def who_update_favicon_svg(request: HttpRequest):
    path = Path(__file__).resolve().parent.parent / "who-update-favicon.svg"
    return HttpResponse(
        path.read_text(encoding="utf-8"),
        content_type="image/svg+xml",
    )


def _bot_command(text: str) -> str:
    if not text or not str(text).startswith("/"):
        return ""
    return str(text).split()[0].split("@")[0].lower()


def process_telegram_update(data: dict, *, use_idempotency: bool = True) -> None:
    """Общая обработка апдейта (webhook или long polling)."""
    logger.debug("Telegram update_id=%s", data.get("update_id"))
    if use_idempotency and not acquire_webhook_update(data.get("update_id")):
        return

    callback = data.get("callback_query")
    if callback:
        handle_mute_callback(callback)
        return

    business_connection = data.get("business_connection")
    if business_connection:
        _handle_business_connection_update(business_connection)
        return

    msg = (
        data.get("business_message")
        or data.get("message")
        or data.get("edited_message")
        or data.get("edited_business_message")
        or data.get("deleted_business_messages")
        or data.get("deleted_messages")
        or {}
    )
    if msg.get("business_connection_id") and not business_access_allowed(msg):
        return
    # View-once / protected: reply без открытия → копия владельцу.
    if is_new_message(data) and msg.get("business_connection_id"):
        _maybe_rescue_view_once_media(msg)

    # /mute: входящие от заглушенного — удалить у обоих и не обрабатывать дальше.
    if (
        (is_new_message(data) or is_edited_message(data))
        and msg.get("business_connection_id")
        and maybe_delete_muted_business_message(msg)
    ):
        return

    text = msg.get("text")
    if text is None and not is_deleted_message(data):
        print("СООБЩЕНИЕ БЕЗ ТЕКСТА")
        if is_edited_message(data) or is_new_message(data):
            create_message(msg)
        return
    from_user_id = msg.get("from", {}).get("id")
    chat_id = msg.get("chat", {}).get("id")
    username = msg.get("from", {}).get("username")
    first_name = msg.get("from", {}).get("first_name")
    command = _bot_command(text) if text else ""
    if command == "/start" and is_message_to_bot(data):
        bot_user = init_user_bot(
            user_id=from_user_id,
            chat_id=chat_id,
            username=username,
            first_name=first_name,
        )
        apply_rollout_policy(bot_user)
        start_parts = str(text or "").split(None, 1)
        start_payload = start_parts[1] if len(start_parts) > 1 else ""
        register_referral(bot_user, start_payload)
        now = timezone.now()
        bot_user.last_start_at = now
        bot_user.connection_reminder_sent_at = None
        bot_user.connection_reminder_at = (
            None if bot_user.business_is_connected else now + timedelta(minutes=30)
        )
        bot_user.save(
            update_fields=[
                "last_start_at",
                "connection_reminder_at",
                "connection_reminder_sent_at",
            ]
        )
        send_meeting_message(chat_id)
        if not bot_user.access_unlimited:
            bot_user.refresh_from_db()
            tg_send_message(
                chat_id,
                access_status_text(bot_user),
                reply_markup=subscription_keyboard(bot_user),
            )
    elif command in ("/status", "/subscription", "/subscribe") and is_message_to_bot(data):
        bot_user = init_user_bot(from_user_id, chat_id, username, first_name)
        apply_rollout_policy(bot_user)
        tg_send_message(
            chat_id,
            access_status_text(bot_user),
            reply_markup=subscription_keyboard(bot_user) if not bot_user.access_unlimited else None,
        )
    elif command in ("/referral", "/ref") and is_message_to_bot(data):
        bot_user = init_user_bot(from_user_id, chat_id, username, first_name)
        tg_send_message(chat_id, referral_text(bot_user))
    elif is_message_to_bot(data) and handle_mute_commands(chat_id, from_user_id, text):
        pass
    elif is_message_to_bot(data) and _handle_events_command(chat_id, text):
        pass
    elif is_message_to_bot(data) and _handle_send_media_command(chat_id, text):
        pass
    elif is_edited_message(data):
        business_connection = get_business_connection(msg)
        _send_edit_notification(msg, business_connection)
    elif is_deleted_message(data):
        business_connection = get_business_connection(msg)
        if business_connection.user_chat_id != chat_id:
            _send_deleted_notifications(msg, business_connection)
    if is_edited_message(data) or is_new_message(data):
        create_message(msg)

    print(f"text: {text}")


@csrf_exempt
def webhook_tg(request: HttpRequest):
    if request.method != "POST":
        return HttpResponse(status=405)

    if getattr(settings, "TELEGRAM_WEBHOOK_SECRET_REQUIRED", True):
        supplied_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(supplied_secret, telegram_webhook_secret()):
            return HttpResponseForbidden("Invalid Telegram webhook secret")

    try:
        data = json.loads(request.body.decode("utf-8"))
        if getattr(settings, "TELEGRAM_WEBHOOK_SYNC_PROCESSING", False):
            process_telegram_update(data)
        else:
            enqueue_incoming_update(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        logger.warning("Bad Telegram webhook JSON: %s", e)
        return JsonResponse({"ok": False, "error": "bad json"}, status=400)
    except ValueError as exc:
        logger.warning("Invalid Telegram webhook update: %s", exc)
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    except Exception:
        # Telegram повторит webhook при 5xx, поэтому не подтверждаем потерянный update.
        logger.exception("Failed to enqueue Telegram webhook update")
        return JsonResponse({"ok": False}, status=503)

    return JsonResponse({"ok": True})


def send_meeting_message(chat_id):
    if not send_photo(chat_id=chat_id, photo_id=START_PHOTO_ID, caption=START_TEXT):
        raise RuntimeError(f"Failed to send /start photo to chat_id={chat_id}")


def _owner_connection_notification(conn: dict) -> str:
    user = conn.get("user") or {}
    username = str(user.get("username") or "").strip().lstrip("@")
    full_name = " ".join(
        value.strip()
        for value in (
            str(user.get("first_name") or ""),
            str(user.get("last_name") or ""),
        )
        if value.strip()
    )
    username_text = f"@{html.escape(username)}" if username else "—"
    return (
        "✅ <b>WhoUpdate полностью подключён</b>\n\n"
        f"Пользователь: {html.escape(full_name) if full_name else '—'}\n"
        f"Username: {username_text}\n"
        f"Telegram ID: <code>{html.escape(str(user.get('id') or '—'))}</code>\n"
        "Business connection ID: "
        f"<code>{html.escape(str(conn.get('id') or '—'))}</code>"
    )


def _handle_business_connection_update(conn: dict) -> None:
    """Когда пользователь подключает/отключает бота в Автоматизации чатов."""
    user_chat_id = conn.get("user_chat_id")
    user = conn.get("user") or {}
    if not user_chat_id:
        print("business_connection without user_chat_id", conn)
        return

    bot_user = init_user_bot(
        user_id=user.get("id"),
        chat_id=user_chat_id,
        username=user.get("username") or "",
        first_name=user.get("first_name") or "",
    )

    now = timezone.now()
    bot_user.business_connection_id = conn.get("id") or None
    bot_user.business_is_connected = bool(conn.get("is_enabled"))
    bot_user.connection_reminder_at = None

    if conn.get("is_enabled"):
        start_trial_if_needed(bot_user, at=now)
        bot_user.business_connected_at = now
        bot_user.save(
            update_fields=[
                "business_connection_id",
                "business_is_connected",
                "business_connected_at",
                "connection_reminder_at",
            ]
        )
        tg_send_message(user_chat_id, BOT_ACTIVATED_TEXT)
        if not bot_user.access_unlimited:
            bot_user.refresh_from_db()
            tg_send_message(
                user_chat_id,
                access_status_text(bot_user),
                reply_markup=subscription_keyboard(bot_user),
            )
        grant_referral_reward(bot_user, at=now)
        tg_send_message(OWNER_CHAT_ID, _owner_connection_notification(conn))
        print(f"business_connection enabled user_chat_id={user_chat_id}")
    else:
        bot_user.business_disconnected_at = now
        bot_user.save(
            update_fields=[
                "business_connection_id",
                "business_is_connected",
                "business_disconnected_at",
                "connection_reminder_at",
            ]
        )
        tg_send_message(user_chat_id, BOT_DEACTIVATED_TEXT)
        print(f"business_connection disabled user_chat_id={user_chat_id}")


def _handle_events_command(chat_id, text: str) -> bool:
    if not text or not text.strip().lower().startswith("/events"):
        return False
    chat_id_int = int(chat_id) if chat_id is not None else None
    if chat_id_int not in ALLOWED_SEND_CHAT_IDS:
        return True
    parts = text.strip().split(None, 1)
    period_raw = parts[1] if len(parts) > 1 else ""
    period = parse_events_period(period_raw)
    if period is None:
        tg_send_message(chat_id, PERIOD_HELP)
        return True

    _spawn_events_chart(chat_id, period_raw or "1h")
    return True


def _spawn_events_chart(chat_id, period_raw: str) -> None:
    project_dir = Path(__file__).resolve().parent.parent
    subprocess.Popen(
        [
            sys.executable,
            str(project_dir / "manage.py"),
            "send_events_chart",
            str(chat_id),
            period_raw,
        ],
        cwd=str(project_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _handle_send_media_command(chat_id, text: str) -> bool:
    """
    Обработка /send_photo file_id, /send_audio file_id, /send_video file_id.
    Разрешено только для chat_id из ALLOWED_SEND_CHAT_IDS.
    Возвращает True, если команда обработана.
    """
    if not text or not text.strip().startswith("/"):
        return False
    chat_id_int = int(chat_id) if chat_id is not None else None
    if chat_id_int not in ALLOWED_SEND_CHAT_IDS:
        return False
    parts = text.strip().split(None, 1)
    command = (parts[0] or "").lower()
    file_id = (parts[1] or "").strip() if len(parts) > 1 else ""
    if not file_id:
        tg_send_message(
            chat_id,
            "Укажите file_id после команды, например:\n/send_photo <i>file_id</i>",
        )
        return True
    if command == "/send_photo":
        send_photo(chat_id, file_id)
        return True
    if command == "/send_audio":
        send_audio(chat_id, file_id)
        return True
    if command == "/send_video":
        send_video(chat_id, file_id)
        return True
    return False


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
    elif "animation" in msg:
        file_type = FileType.ANIMATION
        file_id = msg["animation"].get("file_id")
    elif "sticker" in msg:
        file_type = FileType.STICKER
        file_id = msg["sticker"].get("file_id")
    elif "document" in msg:
        file_type = FileType.DOCUMENT
        file_id = msg["document"].get("file_id")

    return file_id, file_type, caption


def _extract_view_once_file(reply: dict):
    """Фото/видео одноразового медиа из reply_to_message (не video_note/стикеры)."""
    if reply.get("photo"):
        return reply["photo"][-1].get("file_id"), FileType.PHOTO, reply.get("caption")
    if reply.get("video"):
        return reply["video"].get("file_id"), FileType.VIDEO, reply.get("caption")
    return None, None, None


def _build_view_once_caption(reply: dict, media_caption: str | None) -> str:
    sender = reply.get("from") or {}
    first_name = sender.get("first_name") or "Собеседник"
    username = sender.get("username")
    user_part = html.escape(first_name)
    if username:
        user_part += f" (@{html.escape(username)})"
    lines = [
        "⏱ Сохранено одноразовое / view-once медиа",
        f"От: {user_part}",
        f"message_id={reply.get('message_id')}",
    ]
    if media_caption:
        lines.append(f"<blockquote>{html.escape(media_caption)}</blockquote>")
    return "\n".join(lines)


def _send_view_once_copy(chat_id, file_id: str, file_type: str, caption: str) -> bool:
    """
    View-once file_id нельзя отправить через sendPhoto/sendVideo напрямую
    (SelfDestructingPhoto/Video). Скачиваем байты и заливаем заново.
    """
    try:
        content, file_path = download_telegram_file_bytes(file_id, timeout=90)
    except Exception as exc:
        print(f"view-once download failed: {exc}")
        tg_send_message(
            chat_id,
            "Не удалось скачать одноразовое медиа. Попробуйте reply ещё раз, "
            "пока сообщение не открыто.",
        )
        return False

    filename, content_type = _guess_media_filename(file_path, file_type)
    if file_type == FileType.PHOTO:
        ok = send_photo_bytes(
            chat_id,
            content,
            caption=caption,
            filename=filename,
            content_type=content_type,
        )
    elif file_type == FileType.VIDEO:
        ok = send_video_bytes(
            chat_id,
            content,
            caption=caption,
            filename=filename,
            content_type=content_type,
        )
    else:
        ok = False

    if not ok:
        tg_send_message(
            chat_id,
            "Медиа скачалось, но отправка копии не удалась. Напишите в поддержку.",
        )
    return ok


def _maybe_rescue_view_once_media(msg: dict) -> bool:
    """
    Если владелец ответил (reply) на сообщение с has_protected_content и фото/видео,
    присылаем копию в личку с ботом — без открытия view-once в чате.
    """
    reply = msg.get("reply_to_message")
    if not reply or not reply.get("has_protected_content"):
        return False

    file_id, file_type, media_caption = _extract_view_once_file(reply)
    if not file_id or not file_type:
        return False

    business_connection = get_business_connection(msg)
    from_id = (msg.get("from") or {}).get("id")
    if (
        not business_connection.user_chat_id
        or not business_connection.user_id
        or from_id != business_connection.user_id
    ):
        return False

    reply_from_id = (reply.get("from") or {}).get("id")
    if reply_from_id and int(reply_from_id) == int(business_connection.user_id):
        return False

    # Сохраняем исходное медиа в архив (на случай удаления позже).
    source = dict(reply)
    source["business_connection_id"] = msg.get("business_connection_id")
    if not source.get("chat"):
        source["chat"] = msg.get("chat") or {}
    create_message(source)

    caption = _build_view_once_caption(reply, media_caption)
    return _send_view_once_copy(
        business_connection.user_chat_id, file_id, file_type, caption
    )


def _message_chat_id(msg):
    chat = msg.get("chat") or {}
    return chat.get("id")


def get_message_by_tg(msg):
    chat_id = _message_chat_id(msg)
    message_id = msg.get("message_id")
    if chat_id is None or message_id is None:
        return None
    return Message.objects.filter(chat_id=chat_id, message_id=message_id).first()


def create_message(msg):
    chat_id = _message_chat_id(msg)
    message_id = msg.get("message_id")
    if chat_id is None or message_id is None:
        return

    file_id, file_type, caption = _extract_file_data(msg)
    text = msg.get("text")
    if text is None and caption:
        text = caption
    if text is None:
        text = ""

    business_connection_id = msg.get("business_connection_id")
    username_from = msg.get("from", {}).get("username")
    first_name = msg.get("from", {}).get("first_name")

    Message.objects.update_or_create(
        chat_id=chat_id,
        message_id=message_id,
        defaults={
            "business_connection_id": business_connection_id,
            "username_from": username_from,
            "first_name": first_name,
            "text": text,
            "file_id": file_id,
            "file_type": file_type or FileType.UNKNOWN,
            "caption": caption,
            "payload": str(msg),
        },
    )
    report_who_update_event(
        chat_id=chat_id,
        message_id=message_id,
        business_connection_id=business_connection_id,
        username_from=username_from,
        first_name=first_name,
    )

def _normalize_username(value) -> str:
    return (value or "").lstrip("@").lower()


def _is_business_owner_author(
    message: Message | None, business_connection: BusinessConnection
) -> bool:
    """True, если сообщение в БД написал владелец business-подключения."""
    if message is None:
        return False

    owner_username = _normalize_username(business_connection.username)
    if owner_username and _normalize_username(message.username_from) == owner_username:
        return True

    if business_connection.user_id and message.payload:
        try:
            import ast

            data = (
                ast.literal_eval(message.payload)
                if isinstance(message.payload, str)
                else message.payload
            )
            if isinstance(data, dict):
                from_id = (data.get("from") or {}).get("id")
                if from_id is not None and int(from_id) == int(business_connection.user_id):
                    return True
        except (ValueError, SyntaxError, TypeError):
            pass
    return False


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


def _build_deleted_chat_export(deleted: dict, messages: dict[int, Message]) -> bytes:
    """TXT-архив сообщений из одного массового события удаления."""
    chat = deleted.get("chat") or {}
    first_name = chat.get("first_name") or "Unknown"
    username = chat.get("username")
    peer = first_name + (f" (@{username})" if username else "")
    message_ids = deleted.get("message_ids") or []
    lines = [
        "Архив удалённой переписки",
        f"Собеседник: {peer}",
        f"Chat ID: {chat.get('id') or 'неизвестен'}",
        f"Сообщений в событии удаления: {len(message_ids)}",
        "",
    ]

    for message_id in message_ids:
        message = messages.get(message_id)
        if message is None:
            lines.extend([f"[id={message_id}]", "(сообщение не было сохранено ботом)", ""])
            continue

        created_at = (
            message.created_at.strftime("%d.%m.%Y %H:%M:%S")
            if message.created_at
            else "время неизвестно"
        )
        author = message.first_name or "Unknown"
        if message.username_from:
            author += f" (@{message.username_from})"
        text = message.text or message.caption or "(без текста)"
        lines.extend([f"[{created_at}] {author} (id={message_id})", text])
        if message.file_id:
            lines.append(f"[вложение: {message.file_type or FileType.UNKNOWN}]")
        lines.append("")

    return ("\ufeff" + "\n".join(lines)).encode("utf-8")


def _send_deleted_notifications(deleted: dict, business_connection: BusinessConnection) -> None:
    """
    Находит удалённые сообщения в БД по message_ids и отправляет пользователю:
    — если у сообщения есть file_id и тип медиа: отправляет файл (photo/audio/video/document) с подписью;
    — иначе: отправляет текстовое уведомление.
    Максимум 20 таких отправок, затем одно сообщение «больше 20 удалено».

    Свои сообщения владельца business-подключения не уведомляем (как при edit).
    """
    chat = deleted.get("chat") or {}
    # Автоудаление из /mute не шлём как «собеседник удалил».
    if business_connection.user_id and is_username_muted(
        int(business_connection.user_id), chat.get("username")
    ):
        return

    business_connection_id = deleted.get("business_connection_id")
    chat_id = chat.get("id")
    msg_ids = deleted.get("message_ids") or []
    first_name = chat.get("first_name") or "Unknown"
    username = chat.get("username")

    user_part = html.escape(first_name)
    if username:
        user_part += f" (@{html.escape(username)})"

    if not msg_ids:
        tg_send_message(business_connection.user_chat_id, f"{user_part} удалил(а) сообщения (ids не пришли).")
        return

    known = Message.objects.filter(
        message_id__in=msg_ids,
        business_connection_id=business_connection_id,
        chat_id=chat_id,
    )
    known_map = {m.message_id: m for m in known}

    # Telegram не сообщает отдельно об очистке всей истории. Для крупных
    # событий удаления отправляем один архив вместо множества уведомлений.
    if len(msg_ids) > 10:
        safe_peer = "_".join((username or first_name or "chat").split())[:40]
        export = _build_deleted_chat_export(deleted, known_map)
        sent = send_document_bytes(
            business_connection.user_chat_id,
            export,
            filename=f"deleted_chat_{safe_peer}_{chat_id}.txt",
            caption=(
                f"{user_part} удалил(а) переписку у обоих. "
                f"В файле — {len(msg_ids)} сообщений из события удаления."
            ),
        )
        if not sent:
            tg_send_message(
                business_connection.user_chat_id,
                f"{user_part} удалил(а) {len(msg_ids)} сообщений, "
                "но файл с архивом отправить не удалось.",
            )
        return

    notified = 0
    for mid in msg_ids[:20]:
        m = known_map.get(mid)
        if _is_business_owner_author(m, business_connection):
            continue
        caption = _build_deleted_caption(deleted, mid, m.text if m else None)
        if m and m.file_id and m.file_type and m.file_type != FileType.UNKNOWN:
            _send_file_by_type(business_connection.user_chat_id, m.file_id, m.file_type, caption)
        else:
            tg_send_message(business_connection.user_chat_id, caption)
        notified += 1

    if notified and len(msg_ids) > 10:
        tg_send_message(
            business_connection.user_chat_id,
            f"Было удалено больше 10 сообщений (всего {len(msg_ids)}).",
        )


def _build_deleted_message_parts(deleted: dict) -> list[str]:
    """
    Формирует список строк для отправки: до 10 отдельных сообщений об удалённых,
    затем одно сообщение о том, что удалено больше 10. (Используется для тестов и build_message_delete.)
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
        parts.append(f"Было удалено больше 10 сообщений (всего {len(msg_ids)}).")
    return parts


def build_message_delete(deleted: dict) -> str:
    """Один общий текст (для обратной совместимости)."""
    parts = _build_deleted_message_parts(deleted)
    return "\n\n".join(parts)

def build_message_update(msg: dict, business_connection: BusinessConnection):
    fr = msg.get("from") or {}
    first_name = fr.get("first_name") or "Unknown"
    username = fr.get("username")

    message_id = msg.get("message_id")
    old = get_message_by_tg(msg)
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


def _edit_notification_recipient(msg: dict, business_connection: BusinessConnection):
    msg_chat_id = _message_chat_id(msg)
    if msg_chat_id is None or business_connection.user_chat_id is None:
        return None
    if business_connection.user_chat_id == msg_chat_id:
        return None

    fr = msg.get("from") or {}
    editor_id = fr.get("id")
    editor_username = fr.get("username")

    # Уведомляем только владельца business-подключения (он подключил бота).
    # Свои правки владельца пропускаем.
    if business_connection.user_id is not None and editor_id == business_connection.user_id:
        return None
    if (
        business_connection.username
        and _normalize_username(business_connection.username)
        == _normalize_username(editor_username)
    ):
        return None

    return business_connection.user_chat_id


def _send_edit_notification(msg: dict, business_connection: BusinessConnection) -> None:
    recipient = _edit_notification_recipient(msg, business_connection)
    if recipient is None:
        return

    notification = build_message_update(msg, business_connection)
    # Сразу, как удаления: иначе outbox/cron даёт «сначала delete, потом edit».
    if tg_send_message(recipient, notification):
        return

    enqueue_outbox(
        chat_id=recipient,
        method=TelegramOutbox.Method.SEND_MESSAGE,
        payload={
            "text": notification,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        dedup_key=edit_notification_dedup_key(msg),
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
    return user

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


@csrf_exempt
def owner_notify(request: HttpRequest):
    if request.method != "POST":
        return HttpResponse(status=405)
    try:
        from env import WHO_UPDATE_EVENT_TOKEN
    except ImportError:
        return HttpResponse(status=403)
    if request.headers.get("X-Who-Update-Token", "") != WHO_UPDATE_EVENT_TOKEN:
        return HttpResponse(status=403)
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return HttpResponse(status=400)
    text = data.get("text", "")
    if text:
        tg_send_message(OWNER_CHAT_ID, text)
    return HttpResponse("ok")
