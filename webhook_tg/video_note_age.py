"""Owner-only pilot: flag media with an available date well before sending."""

from __future__ import annotations

import hashlib
import html
import io
import re
import struct
import time
import warnings
from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from django.utils import timezone
from PIL import Image

from .config import OWNER_CHAT_ID
from .models import BackgroundTask, TelegramOutbox, UserTg
from .outbox import enqueue_outbox
from .telegram import get_telegram_file_path, open_telegram_file_stream

VIDEO_NOTE_AGE_TASK = "check_video_note_age"
PILOT_USER_ID = int(OWNER_CHAT_ID)
AGE_THRESHOLD = timedelta(minutes=3)
MAX_VIDEO_BYTES = 20 * 1024 * 1024
MOSCOW = ZoneInfo("Europe/Moscow")
MP4_EPOCH = datetime(1904, 1, 1, tzinfo=dt_timezone.utc)


def _message_media(msg: dict) -> tuple[dict, str, str] | None:
    """Return media, its type for analysis, and its Telegram sending type."""
    for kind in ("video_note", "video", "animation"):
        if isinstance(msg.get(kind), dict):
            return msg[kind], kind, kind
    if msg.get("photo"):
        photos = [p for p in msg["photo"] if isinstance(p, dict)]
        if photos:
            return max(photos, key=lambda p: p.get("width", 0) * p.get("height", 0)), "photo", "photo"
    for field in ("document", "audio"):
        media = msg.get(field)
        if not isinstance(media, dict):
            continue
        mime = str(media.get("mime_type") or "").lower()
        extension = str(media.get("file_name") or "").lower().rsplit(".", 1)[-1]
        if mime.startswith("image/") or extension in ("jpg", "jpeg", "png", "webp", "tif", "tiff", "heic", "heif"):
            return media, "photo", field
        if mime.startswith("video/") or extension in ("mp4", "mov", "m4v"):
            return media, "video", field
        if mime in ("audio/mp4", "audio/x-m4a") or extension == "m4a":
            return media, "audio", field
    return None


def _pilot_owner(connection_id: str) -> UserTg | None:
    if not connection_id:
        return None
    user = UserTg.objects.filter(
        user_id=PILOT_USER_ID,
        chat_id=PILOT_USER_ID,
        business_connection_id=connection_id,
        business_is_connected=True,
    ).first()
    return user if user and user.has_active_access() else None


def schedule_video_note_age_check(msg: dict) -> BackgroundTask | None:
    # Keep the existing task name/key so pending video-note jobs remain valid.
    selected = _message_media(msg)
    if selected is None:
        return None
    note, media_kind, send_kind = selected
    sender = msg.get("from") or {}
    chat = msg.get("chat") or {}
    if (
        not isinstance(note, dict)
        or not note.get("file_id")
        or sender.get("id") in (None, PILOT_USER_ID)
        or chat.get("type") != "private"
        or not chat.get("id")
        or not msg.get("message_id")
        or not isinstance(msg.get("date"), int)
        or msg.get("forward_origin")
        or msg.get("forward_date")
        or (note.get("file_size") or 0) > MAX_VIDEO_BYTES
    ):
        return None
    connection_id = msg.get("business_connection_id")
    owner = _pilot_owner(connection_id)
    if owner is None:
        return None

    from .background_tasks import enqueue_background_task

    connection_key = hashlib.sha256(connection_id.encode()).hexdigest()[:16]
    task, _ = enqueue_background_task(
        task_type=VIDEO_NOTE_AGE_TASK,
        payload={
            "connection_id": connection_id,
            "chat_id": chat["id"],
            "message_id": msg["message_id"],
            "sent_at": msg["date"],
            "file_id": note["file_id"],
            "media_kind": media_kind,
            "send_kind": send_kind,
            "sender_id": sender["id"],
            "sender_name": str(sender.get("first_name") or "Собеседник")[:128],
            "sender_username": str(sender.get("username") or "")[:64],
        },
        run_at=timezone.now(),
        idempotency_key=(
            f"video-note-age:{owner.pk}:{connection_key}:"
            f"{chat['id']}:{msg['message_id']}"
        ),
        priority=100,
        max_attempts=3,
    )
    return task


def _atoms(data: bytes, start: int, end: int):
    """Read bounded MP4 box headers; never decode audio or video."""
    count = 0
    while start < end:
        count += 1
        if count > 4096 or start + 8 > end:
            raise ValueError("Invalid MP4 box sequence")
        size, kind = struct.unpack_from(">I4s", data, start)
        header = 8
        if size == 1:
            if start + 16 > end:
                raise ValueError("Truncated MP4 box")
            size = struct.unpack_from(">Q", data, start + 8)[0]
            header = 16
        elif size == 0:
            size = end - start
        if size < header or size > end - start:
            raise ValueError("Invalid MP4 box size")
        yield kind, start + header, start + size
        start += size


def mp4_creation_time(data: bytes) -> datetime | None:
    """Use the movie header's file-creation date, not an asserted recording date."""
    if not data or len(data) > MAX_VIDEO_BYTES:
        return None
    values = []
    has_file_type = False
    try:
        for kind, start, end in _atoms(data, 0, len(data)):
            if kind == b"ftyp":
                has_file_type = True
            if kind != b"moov":
                continue
            for child, body, child_end in _atoms(data, start, end):
                if child != b"mvhd":
                    continue
                if body + 4 > child_end:
                    return None
                version = data[body]
                if version not in (0, 1):
                    return None
                width = 8 if version == 1 else 4
                # FullBox flags, creation/modification time, timescale and duration.
                if body + 4 + width * 3 + 4 > child_end:
                    return None
                raw = int.from_bytes(data[body + 4:body + 4 + width], "big")
                if not raw:
                    return None
                values.append(MP4_EPOCH + timedelta(seconds=raw))
    except (ValueError, OverflowError):
        return None
    if not has_file_type or len(values) != 1:
        return None
    return values[0]


def photo_creation_time(data: bytes) -> datetime | None:
    """Read original/digitized EXIF time only with an explicit UTC offset."""
    if not data or len(data) > MAX_VIDEO_BYTES:
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                exif = image.getexif()
                tags = dict(exif)
                if 34665 in exif:
                    tags.update(exif.get_ifd(34665))
        for date_tag, offset_tag in ((36867, 36881), (36868, 36882)):
            date = tags.get(date_tag)
            offset = tags.get(offset_tag)
            if not isinstance(date, str) or not isinstance(offset, str):
                continue
            date, offset = date.strip(" \x00"), offset.strip(" \x00")
            if not re.fullmatch(r"[+-]\d{2}:\d{2}", offset):
                continue
            hours, minutes = int(offset[1:3]), int(offset[4:6])
            if minutes >= 60 or hours * 60 + minutes > 14 * 60:
                continue
            delta = timedelta(hours=hours, minutes=minutes)
            if offset[0] == "-":
                delta = -delta
            local = datetime.strptime(date, "%Y:%m:%d %H:%M:%S")
            return local.replace(tzinfo=dt_timezone(delta)).astimezone(dt_timezone.utc)
    except Exception:
        # Unsupported formats and malformed metadata cannot establish an age.
        return None
    return None


def _download_video(file_id: str) -> bytes | None:
    """Bound download size and keep private media in memory only."""
    try:
        deadline = time.monotonic() + 30
        path = get_telegram_file_path(file_id, timeout=15)
        response = open_telegram_file_stream(path, timeout=15)
        try:
            data = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if time.monotonic() > deadline:
                    raise TimeoutError("Video note download exceeded time limit")
                if len(data) + len(chunk) > MAX_VIDEO_BYTES:
                    return None
                data.extend(chunk)
            return bytes(data)
        finally:
            response.close()
    except Exception:
        # Download exceptions can contain a credential-bearing URL.
        raise RuntimeError("Video note download failed") from None


def _warning_text(payload: dict, created_at: datetime, sent_at: datetime) -> str:
    media_kind = payload.get("media_kind", "video_note")
    title, subject = {
        "video_note": ("Возможно, этот кружок записан заранее", "видео"),
        "video": ("Возможно, это видео записано заранее", "видео"),
        "photo": ("Возможно, это фото сделано заранее", "фото"),
        "animation": ("Возможно, эта анимация создана заранее", "анимацию"),
        "audio": ("Возможно, это аудио записано заранее", "аудио"),
    }[media_kind]
    sender = html.escape(payload["sender_name"])
    if payload.get("sender_username"):
        sender += f" (@{html.escape(payload['sender_username'].lstrip('@'))})"
    else:
        sender += f" (ID: <code>{int(payload['sender_id'])}</code>)"
    seconds = int((sent_at - created_at).total_seconds())
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    age = " ".join(
        f"{value} {label}"
        for value, label in ((days, "дн."), (hours, "ч."), (minutes, "мин."), (seconds, "сек."))
        if value
    )
    return (
        f"⚠️ <b>{title}</b>\n\n"
        f"Отправитель: {sender}\n"
        f"Создан по данным файла: <b>{created_at.astimezone(MOSCOW):%d.%m.%Y %H:%M:%S}</b> МСК\n"
        f"Отправлен: <b>{sent_at.astimezone(MOSCOW):%d.%m.%Y %H:%M:%S}</b> МСК\n"
        f"Разница: <b>{age}</b>\n\n"
        f"Возможно, {subject} переслали или отправили спустя время после создания. "
        "Дата в файле может быть неточной — это не доказательство пересылки.\n\n"
        '<a href="https://t.me/who_update_bot">@who_update_bot</a>'
    )


def check_video_note_age(task: BackgroundTask) -> None:
    payload = task.payload
    if payload.get("sender_id") in (None, PILOT_USER_ID):
        return
    if _pilot_owner(payload.get("connection_id")) is None:
        return
    if TelegramOutbox.objects.filter(idempotency_key=task.idempotency_key).exists():
        return
    data = _download_video(payload["file_id"])
    parser = photo_creation_time if payload.get("media_kind") == "photo" else mp4_creation_time
    created_at = parser(data) if data else None
    sent_at = datetime.fromtimestamp(payload["sent_at"], dt_timezone.utc)
    if (
        created_at is None
        or created_at < datetime(2000, 1, 1, tzinfo=dt_timezone.utc)
        or created_at > sent_at
        or sent_at - created_at <= AGE_THRESHOLD
    ):
        return
    # Recheck after network I/O: the owner may have disconnected the bot.
    if _pilot_owner(payload.get("connection_id")) is None:
        return
    send_kind = payload.get("send_kind", "video_note")
    media_payload = (
        {"video_note": payload["file_id"]}
        if send_kind == "video_note"
        else {"media_kind": send_kind, "file_id": payload["file_id"]}
    )
    item = enqueue_outbox(
        chat_id=PILOT_USER_ID,
        method=(
            TelegramOutbox.Method.SEND_VIDEO_NOTE_WITH_TEXT
            if send_kind == "video_note"
            else TelegramOutbox.Method.SEND_MEDIA_WITH_TEXT
        ),
        payload={
            **media_payload,
            "text": _warning_text(payload, created_at, sent_at),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        idempotency_key=task.idempotency_key,
    )
    if item is None:
        raise RuntimeError("Could not queue video note age warning")
