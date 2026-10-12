"""Экспорт сохранённой истории по username или Telegram ID собеседника."""

from __future__ import annotations

import ast
import html
import json
import re
from datetime import datetime, timezone as datetime_timezone

from django.db.models import Count, F, Window
from django.db.models.functions import Lower, RowNumber
from django.utils import timezone

from .models import FileType, Message, UserTg
from .outbox import send_document_bytes_reliably, send_message_reliably
from .telegram import answer_callback_query


HISTORY_HELP = (
    "Укажите username пользователя или его ID.\n\n"
    "Примеры:\n"
    "<code>/history @username</code>\n"
    "<code>/history 123456789</code>\n\n"
    "123456789 — Telegram ID собеседника, не номер телефона."
)
HISTORY_CONTACTS_CALLBACK = "history:contacts"
HISTORY_CONTACTS_BUTTON = "У пользователя нет username или не знаю его ID"


def _parse_history_target(text: str) -> str | int | None:
    parts = str(text or "").strip().split()
    if len(parts) != 2:
        return None
    target = parts[1]
    if re.fullmatch(r"[0-9]+", target):
        if len(target) > 16:
            return None
        user_id = int(target)
        return user_id if 0 < user_id < 2 ** 52 else None
    username = target.removeprefix("@").lower()
    if username.isdecimal():
        return None
    if not re.fullmatch(r"[a-z0-9_]{1,32}", username):
        return None
    return username


def _format_created_at(message: Message) -> str:
    if not message.created_at:
        return "время неизвестно"
    value = message.created_at
    if timezone.is_aware(value):
        value = timezone.localtime(value)
    return value.strftime("%d.%m.%Y %H:%M:%S")


def _build_history_export(title: str, messages) -> bytes:
    lines = [
        title,
        f"Сообщений: {len(messages)}",
        "",
    ]
    for message in messages:
        author = message.first_name or "Без имени"
        if message.username_from:
            author += f" (@{message.username_from})"
        content = message.text or message.caption or "(без текста)"
        lines.extend(
            [
                f"[{_format_created_at(message)}] {author}",
                f"Chat ID: {message.chat_id}; Message ID: {message.message_id}",
                content,
            ]
        )
        if message.file_id:
            lines.append(f"[вложение: {message.file_type or FileType.UNKNOWN}]")
        lines.append("")
    return ("\ufeff" + "\n".join(lines)).encode("utf-8")


def _history_message_payload(message: Message) -> dict:
    try:
        payloadFieldToValue = json.loads(message.payload or "{}")
    except (ValueError, TypeError, RecursionError):
        try:
            payloadFieldToValue = ast.literal_eval(message.payload or "{}")
        except (ValueError, SyntaxError, TypeError, RecursionError):
            return {}
    return payloadFieldToValue if isinstance(payloadFieldToValue, dict) else {}


def _history_peer(message: Message, bot_user: UserTg, payloadFieldToValue: dict) -> tuple[str, str]:
    for field in ("chat", "from"):
        peerFieldToValue = payloadFieldToValue.get(field)
        if isinstance(peerFieldToValue, dict) and peerFieldToValue.get("id") == message.chat_id:
            name = " ".join(str(peerFieldToValue.get(key) or "") for key in ("first_name", "last_name")).strip()
            return " ".join(name.split()) or "Имя не сохранено", str(peerFieldToValue.get("username") or "")
    # Legacy rows can lack Telegram metadata. Do not label a peer with the
    # owner's name when the last message in the conversation was outgoing.
    if (
        message.username_from and bot_user.username
        and message.username_from.lower() != bot_user.username.lower()
    ):
        return " ".join((message.first_name or "Имя не сохранено").split()), message.username_from
    return "Имя не сохранено", ""


def _history_message_date(message: Message, payloadFieldToValue: dict):
    timestamp = payloadFieldToValue.get("date")
    if type(timestamp) is int and timestamp > 0:
        try:
            return datetime.fromtimestamp(timestamp, tz=datetime_timezone.utc), False
        except (ValueError, OverflowError, OSError):
            pass
    value = message.created_at
    if value and timezone.is_naive(value):
        value = timezone.make_aware(value)
    return value, True


def _build_history_contacts_export(bot_user: UserTg, *, upper_pk=None, output=None) -> tuple[bytes, int]:
    if not bot_user.business_connection_id:
        return b"", 0
    history = Message.objects.filter(business_connection_id=bot_user.business_connection_id)
    if upper_pk is not None:
        history = history.filter(pk__lte=upper_pk)
    # Rank by Telegram message ID, not insertion time: an old message may be
    # archived later via a reply. Only load the payload of each chat's last row.
    last_rows = list(history.annotate(
        message_count=Window(Count("id"), partition_by=F("chat_id")),
        position=Window(RowNumber(), partition_by=F("chat_id"), order_by=F("message_id").desc()),
    ).filter(position=1).order_by().values("id", "message_count"))
    contacts = []
    for offset in range(0, len(last_rows), 500):
        batch = last_rows[offset:offset + 500]
        recordIdToMessage = history.only(
            "chat_id", "username_from", "first_name", "text", "caption", "payload",
            "created_at", "file_id", "file_type",
        ).in_bulk([rowFieldToValue["id"] for rowFieldToValue in batch])
        for rowFieldToValue in batch:
            message = recordIdToMessage.get(rowFieldToValue["id"])
            if message is None:
                continue
            payloadFieldToValue = _history_message_payload(message)
            name, username = _history_peer(message, bot_user, payloadFieldToValue)
            date, saved_date = _history_message_date(message, payloadFieldToValue)
            # Sorting needs one compact latest-message record per peer, not
            # every archive payload. Do not retain the JSON after extracting it.
            message.payload = ""
            contacts.append((date, saved_date, message, rowFieldToValue["message_count"], name, username))
    contacts.sort(key=lambda item: (
        item[0] is None,
        -item[0].timestamp() if item[0] is not None else 0,
        item[2].chat_id,
    ))
    lines = [
        "Сохранённые переписки WhoUpdate",
        f"Чатов: {len(contacts)}",
        "Порядок: от поздней даты последнего сообщения к ранней.",
        f"Часовой пояс: {timezone.get_current_timezone_name()}.",
        "Количество сообщений включает сохранённые сообщения обеих сторон.",
        "Имена и username — последние сохранённые данные, они могли измениться.",
        "",
    ]
    if output is not None:
        output.write(("\ufeff" + "\n".join(lines) + "\n").encode("utf-8"))
        lines = []
    fileTypeToLabel = dict(FileType.choices)
    for number, (date, saved_date, message, count, name, username) in enumerate(contacts, 1):
        if message.file_id or (message.file_type and message.file_type != FileType.UNKNOWN):
            last_message = f"[Медиа: {fileTypeToLabel.get(message.file_type, 'файл')}]"
        else:
            last_message = message.text or message.caption or "(без текста)"
        date_text = timezone.localtime(date).strftime("%d.%m.%Y %H:%M:%S") if date else "неизвестна"
        if date and saved_date:
            date_text += " (дата сохранения; точная дата сообщения не сохранилась)"
        lines.extend([
            f"{number}. {name}",
            f"Username: @{username}" if username else "Username: отсутствует или не сохранён",
            f"Telegram ID: {message.chat_id}",
            f"Сообщений: {count}",
            f"Последнее сообщение: {last_message}",
            f"Дата последнего сообщения: {date_text}",
            f"Получить переписку: /history {message.chat_id}",
            "",
        ])
        if output is not None:
            output.write(("\n".join(lines) + "\n").encode("utf-8"))
            lines = []
    if output is not None:
        return b"", len(contacts)
    return ("\ufeff" + "\n".join(lines)).encode("utf-8"), len(contacts)


def handle_history_callback(callbackFieldToValue: dict, *, update_id: int) -> bool:
    if callbackFieldToValue.get("data") != HISTORY_CONTACTS_CALLBACK:
        return False
    senderFieldToValue = callbackFieldToValue.get("from") or {}
    messageFieldToValue = callbackFieldToValue.get("message") or {}
    chatFieldToValue = messageFieldToValue.get("chat") or {}
    user_id = senderFieldToValue.get("id")
    callback_id = callbackFieldToValue.get("id")
    if (
        type(user_id) is not int or user_id <= 0
        or chatFieldToValue.get("type") != "private"
        or chatFieldToValue.get("id") != user_id
    ):
        answer_callback_query(callback_id, "Откройте /history в личном чате с ботом")
        return True
    bot_user = UserTg.objects.filter(user_id=user_id, chat_id=user_id).first()
    if not bot_user or not bot_user.business_connection_id:
        answer_callback_query(callback_id, "Сначала подключите WhoUpdate к автоматизации чатов")
        return True
    from .history_exports import schedule_history_export

    schedule_history_export(bot_user, chat_id=user_id, target=None, update_id=update_id, contacts=True)
    answer_callback_query(callback_id, "Готовлю список сохранённых переписок")
    return True


def handle_history_command(
    chat_id: int,
    bot_user: UserTg,
    text: str,
    *,
    update_id: int,
) -> bool:
    """Отправляет TXT с историей, доступной только владельцу подключения."""
    key_prefix = f"command:{update_id}:history"
    target = _parse_history_target(text)
    if target is None:
        send_message_reliably(
            chat_id,
            HISTORY_HELP,
            reply_markup={"inline_keyboard": [[{
                "text": HISTORY_CONTACTS_BUTTON,
                "callback_data": HISTORY_CONTACTS_CALLBACK,
            }]]},
            idempotency_key=f"{key_prefix}:help",
        )
        return True

    if not bot_user.business_connection_id:
        send_message_reliably(
            chat_id,
            "Сначала подключите WhoUpdate к автоматизации чатов, затем повторите команду.",
            idempotency_key=f"{key_prefix}:not-connected",
        )
        return True

    from .history_exports import schedule_history_export

    schedule_history_export(bot_user, chat_id=chat_id, target=target, update_id=update_id)
    return True
