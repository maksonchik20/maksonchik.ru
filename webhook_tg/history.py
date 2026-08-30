"""Экспорт сохранённой истории сообщений по username."""

from __future__ import annotations

import html
import re

from django.db.models.functions import Lower
from django.utils import timezone

from .models import FileType, Message, UserTg
from .telegram import send_document_bytes, tg_send_message


HISTORY_HELP = (
    "Укажите username пользователя.\n\n"
    "Пример: <code>/history @username</code>"
)


def _parse_history_username(text: str) -> str:
    parts = str(text or "").strip().split()
    if len(parts) < 2:
        return ""
    username = parts[1].strip().lstrip("@").lower()
    if not re.fullmatch(r"[a-z0-9_]{1,32}", username):
        return ""
    return username


def _format_created_at(message: Message) -> str:
    if not message.created_at:
        return "время неизвестно"
    value = message.created_at
    if timezone.is_aware(value):
        value = timezone.localtime(value)
    return value.strftime("%d.%m.%Y %H:%M:%S")


def _build_history_export(username: str, messages) -> bytes:
    lines = [
        f"История сообщений от @{username}",
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


def handle_history_command(chat_id: int, bot_user: UserTg, text: str) -> bool:
    """Отправляет TXT со всеми сообщениями указанного пользователя."""
    username = _parse_history_username(text)
    if not username:
        tg_send_message(chat_id, HISTORY_HELP)
        return True

    if not bot_user.business_connection_id:
        tg_send_message(
            chat_id,
            "Сначала подключите WhoUpdate к автоматизации чатов, затем повторите команду.",
        )
        return True

    messages = list(
        Message.objects.alias(username_normalized=Lower("username_from")).filter(
            business_connection_id=bot_user.business_connection_id,
            username_normalized=username,
        ).order_by("created_at", "message_id")
    )
    if not messages:
        tg_send_message(
            chat_id,
            f"Сообщений от <b>@{html.escape(username)}</b> пока не найдено.",
        )
        return True

    sent = send_document_bytes(
        chat_id,
        _build_history_export(username, messages),
        filename=f"history_{username}.txt",
        caption=(
            f"История сообщений от <b>@{html.escape(username)}</b>: "
            f"{len(messages)} шт."
        ),
    )
    if not sent:
        tg_send_message(chat_id, "Не удалось отправить файл истории. Попробуйте позже.")
    return True
