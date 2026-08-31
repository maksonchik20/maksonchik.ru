"""Команды /mute /unmute /mutelist, wizard с кнопками и автоудаление."""

from __future__ import annotations

import html
import re
from datetime import timedelta

from django.utils import timezone

from .models import MuteSetup, MutedPeer
from .outbox import send_message_reliably
from .telegram import (
    answer_callback_query,
    delete_business_messages,
    edit_message_text,
    get_business_connection,
    tg_send_business_message,
    tg_send_message,
)

MUTE_HELP = (
    "Использование:\n"
    "<code>/mute @username</code> — настроить mute (срок + уведомления)\n"
    "<code>/unmute @username</code> — снять mute\n"
    "<code>/mutelist</code> — список\n\n"
    "Нужны права бота: удаление сообщений и ответы от вашего имени "
    "в Автоматизации чатов."
)

DURATION_CHOICES = (
    (600, "10 минут"),
    (3600, "1 час"),
    (10800, "3 часа"),
    (86400, "1 день"),
    (0, "Навсегда"),
)
DURATION_LABELS = dict(DURATION_CHOICES)


def normalize_mute_username(value: str | None) -> str:
    return (value or "").strip().lstrip("@").lower()


def parse_mute_username(text: str) -> str | None:
    """Достаёт username из `/mute @user` или `/mute user`."""
    if not text:
        return None
    parts = text.strip().split(None, 1)
    if len(parts) < 2:
        return None
    raw = parts[1].strip().split()[0]
    raw = re.sub(r"^https?://(t\.me|telegram\.me)/", "", raw, flags=re.I)
    username = normalize_mute_username(raw)
    if not username or not re.fullmatch(r"[a-zA-Z0-9_]{4,64}", username):
        return None
    return username


def _duration_keyboard(setup_id: int) -> dict:
    rows = []
    row = []
    for seconds, label in DURATION_CHOICES:
        row.append({"text": label, "callback_data": f"ms:{setup_id}:d:{seconds}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return {"inline_keyboard": rows}


def _notify_keyboard(setup_id: int) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "Да", "callback_data": f"ms:{setup_id}:n:1"},
                {"text": "Нет", "callback_data": f"ms:{setup_id}:n:0"},
            ]
        ]
    }


def _format_expires(expires_at) -> str:
    if not expires_at:
        return "навсегда"
    local = timezone.localtime(expires_at)
    return local.strftime("%d.%m.%Y %H:%M")


def find_mute_row(owner_user_id: int, from_user: dict | None) -> MutedPeer | None:
    if not owner_user_id or not from_user:
        return None
    username = normalize_mute_username(from_user.get("username"))
    user_id = from_user.get("id")
    qs = MutedPeer.objects.filter(owner_user_id=int(owner_user_id))
    row = None
    if username:
        row = qs.filter(muted_username=username).first()
    if row is None and user_id is not None:
        row = qs.filter(muted_user_id=int(user_id)).first()
    if row is None:
        return None
    if user_id is not None and row.muted_user_id is None:
        row.muted_user_id = int(user_id)
        row.save(update_fields=["muted_user_id"])
    return row


def get_active_mute(owner_user_id: int, from_user: dict | None) -> MutedPeer | None:
    """Активный mute; если срок вышел — снимаем и возвращаем None."""
    row = find_mute_row(owner_user_id, from_user)
    if row is None:
        return None
    if row.expires_at and timezone.now() >= row.expires_at:
        uname = row.muted_username
        owner_chat = row.owner_chat_id
        row.delete()
        if owner_chat:
            tg_send_message(
                owner_chat,
                f"Mute @{html.escape(uname)} истёк и снят автоматически.",
            )
        return None
    return row


def is_peer_muted(owner_user_id: int, from_user: dict | None) -> bool:
    return get_active_mute(owner_user_id, from_user) is not None


def is_username_muted(owner_user_id: int, username: str | None) -> bool:
    uname = normalize_mute_username(username)
    if not owner_user_id or not uname:
        return False
    row = MutedPeer.objects.filter(
        owner_user_id=int(owner_user_id), muted_username=uname
    ).first()
    if row is None:
        return False
    if row.expires_at and timezone.now() >= row.expires_at:
        row.delete()
        return False
    return True


def handle_mute_commands(chat_id, user_id, text: str, *, update_id: int) -> bool:
    """Обработка /mute /unmute /mutelist в личке с ботом. True если команда распознана."""
    if not text:
        return False
    command = (text.strip().split()[0] or "").split("@", 1)[0].lower()
    if command not in {"/mute", "/unmute", "/mutelist"}:
        return False

    if user_id is None or chat_id is None:
        return True

    def reply(message: str, *, reply_markup: dict | None = None):
        return send_message_reliably(
            chat_id,
            message,
            idempotency_key=f"command:{update_id}:mute-response",
            reply_markup=reply_markup,
        )

    if command == "/mutelist":
        rows = list(
            MutedPeer.objects.filter(owner_user_id=int(user_id)).order_by("muted_username")
        )
        # Лениво снимем просроченные при показе списка.
        active = []
        for row in rows:
            if row.expires_at and timezone.now() >= row.expires_at:
                row.delete()
                continue
            active.append(row)
        if not active:
            reply("Список mute пуст.\n\n" + MUTE_HELP)
            return True
        lines = ["Заглушены:"]
        for row in active:
            notify = "уведомления в боте" if row.notify_in_bot else "без уведомлений"
            lines.append(
                f"• @{row.muted_username} — до {_format_expires(row.expires_at)}, {notify}"
            )
        lines.append("\nСнять: <code>/unmute @username</code>")
        reply("\n".join(lines))
        return True

    username = parse_mute_username(text)
    if not username:
        reply(MUTE_HELP)
        return True

    if command == "/unmute":
        deleted, _ = MutedPeer.objects.filter(
            owner_user_id=int(user_id), muted_username=username
        ).delete()
        MuteSetup.objects.filter(
            owner_user_id=int(user_id), muted_username=username
        ).delete()
        if deleted:
            reply(f"@{username} снят с mute.")
        else:
            reply(f"@{username} не был в mute.\n\n" + MUTE_HELP)
        return True

    # /mute → wizard: сначала срок
    MuteSetup.objects.filter(owner_user_id=int(user_id), muted_username=username).delete()
    setup = MuteSetup.objects.create(
        owner_user_id=int(user_id),
        owner_chat_id=int(chat_id),
        muted_username=username,
    )
    reply(
        f"Mute для <b>@{html.escape(username)}</b>\n\nНа какое время?",
        reply_markup=_duration_keyboard(setup.id),
    )
    return True


def handle_mute_callback(callback: dict) -> bool:
    """Обработка callback кнопок mute-wizard. True если это наш callback."""
    data = (callback.get("data") or "").strip()
    if not data.startswith("ms:"):
        return False

    cq_id = callback.get("id")
    from_user = callback.get("from") or {}
    user_id = from_user.get("id")
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    message_id = message.get("message_id")

    parts = data.split(":")
    # ms:{setup_id}:d:{seconds}  или  ms:{setup_id}:n:{0|1}
    if len(parts) != 4:
        answer_callback_query(cq_id, "Некорректная кнопка")
        return True

    try:
        setup_id = int(parts[1])
        step = parts[2]
        value = int(parts[3])
    except ValueError:
        answer_callback_query(cq_id, "Некорректная кнопка")
        return True

    setup = MuteSetup.objects.filter(id=setup_id).first()
    if setup is None:
        answer_callback_query(cq_id, "Сессия устарела, снова /mute")
        return True
    if user_id is None or int(user_id) != int(setup.owner_user_id):
        answer_callback_query(cq_id, "Это не ваша настройка")
        return True

    if step == "d":
        if value not in DURATION_LABELS:
            answer_callback_query(cq_id, "Неизвестный срок")
            return True
        setup.duration_seconds = value
        setup.save(update_fields=["duration_seconds"])
        answer_callback_query(cq_id)
        edit_message_text(
            chat_id,
            message_id,
            f"Mute для <b>@{html.escape(setup.muted_username)}</b>\n"
            f"Срок: <b>{DURATION_LABELS[value]}</b>\n\n"
            "Получать его сообщения в боте?",
            reply_markup=_notify_keyboard(setup.id),
        )
        return True

    if step == "n":
        if setup.duration_seconds is None:
            answer_callback_query(cq_id, "Сначала выберите срок")
            return True
        notify = value == 1
        duration = int(setup.duration_seconds)
        expires_at = None
        if duration > 0:
            expires_at = timezone.now() + timedelta(seconds=duration)

        MutedPeer.objects.update_or_create(
            owner_user_id=int(setup.owner_user_id),
            muted_username=setup.muted_username,
            defaults={
                "owner_chat_id": int(setup.owner_chat_id),
                "expires_at": expires_at,
                "notify_in_bot": notify,
                "warning_sent": False,
            },
        )
        username = setup.muted_username
        setup.delete()
        answer_callback_query(cq_id, "Mute сохранён")
        notify_label = "да" if notify else "нет"
        edit_message_text(
            chat_id,
            message_id,
            f"Готово: <b>@{html.escape(username)}</b> в mute\n"
            f"Срок: <b>{_format_expires(expires_at)}</b>\n"
            f"Сообщения в боте: <b>{notify_label}</b>\n\n"
            f"Снять: <code>/unmute @{html.escape(username)}</code>",
            reply_markup={"inline_keyboard": []},
        )
        return True

    answer_callback_query(cq_id, "Неизвестное действие")
    return True


def _peer_warning_text(mute: MutedPeer) -> str:
    until = _format_expires(mute.expires_at)
    return (
        "Вы в mute.\n"
        f"Ваши сообщения автоматически удаляются до: {until}.\n"
        "Повторные сообщения также будут удаляться."
    )


def _notify_owner_about_muted_message(mute: MutedPeer, msg: dict) -> None:
    if not mute.notify_in_bot or not mute.owner_chat_id:
        return
    from_user = msg.get("from") or {}
    username = from_user.get("username") or mute.muted_username
    first_name = from_user.get("first_name") or ""
    text = msg.get("text") or msg.get("caption") or ""
    file_hint = ""
    if msg.get("photo"):
        file_hint = "[фото] "
    elif msg.get("video"):
        file_hint = "[видео] "
    elif msg.get("voice") or msg.get("audio"):
        file_hint = "[аудио] "
    elif msg.get("document"):
        file_hint = "[документ] "
    elif msg.get("sticker"):
        file_hint = "[стикер] "
    body = html.escape(text) if text else "<i>без текста</i>"
    tg_send_message(
        mute.owner_chat_id,
        f"Mute @{html.escape(username)} ({html.escape(first_name)}):\n"
        f"{file_hint}<blockquote>{body}</blockquote>",
    )


def maybe_delete_muted_business_message(msg: dict) -> bool:
    """
    Если автор в mute — на первое сообщение предупреждаем от имени владельца,
    дальше только удаляем. При необходимости шлём копию владельцу в бота.
    """
    business_connection_id = msg.get("business_connection_id")
    message_id = msg.get("message_id")
    from_user = msg.get("from") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if not business_connection_id or message_id is None:
        return False

    business_connection = get_business_connection(msg)
    owner_id = business_connection.user_id
    if not owner_id:
        return False

    from_id = from_user.get("id")
    if from_id is not None and int(from_id) == int(owner_id):
        return False

    mute = get_active_mute(int(owner_id), from_user)
    if mute is None:
        return False

    _notify_owner_about_muted_message(mute, msg)

    if not mute.warning_sent:
        warn_ok, warn_err = tg_send_business_message(
            business_connection_id,
            chat_id,
            _peer_warning_text(mute),
        )
        if warn_ok:
            mute.warning_sent = True
            mute.save(update_fields=["warning_sent"])
        else:
            print(f"mute warning send failed: {warn_err}")
            if business_connection.user_chat_id:
                lower = (warn_err or "").lower()
                if "right" in lower or "forbidden" in lower or "not enough" in lower:
                    tg_send_message(
                        business_connection.user_chat_id,
                        "Не удалось отправить предупреждение о mute от вашего имени: "
                        "включите право бота <b>отвечать в чатах</b> "
                        "в Автоматизации чатов.",
                    )

    ok, error = delete_business_messages(business_connection_id, [int(message_id)])
    if not ok:
        if business_connection.user_chat_id and error:
            lower = error.lower()
            if "right" in lower or "forbidden" in lower or "not enough" in lower:
                tg_send_message(
                    business_connection.user_chat_id,
                    "Не удалось удалить сообщение из mute: у бота нет права "
                    "<b>удалять сообщения</b> в Автоматизации чатов. "
                    "Переподключите WhoUpdate с этим правом.",
                )
        print(f"mute delete failed: {error}")
        return True

    print(
        f"muted delete ok owner={owner_id} from=@{from_user.get('username')} mid={message_id} "
        f"warned={mute.warning_sent}"
    )
    return True
