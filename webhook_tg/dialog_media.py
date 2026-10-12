"""Read-only media previews for the private bot dialog in Django admin."""
from dataclasses import dataclass
import re

from django.http import HttpResponse, HttpResponseForbidden, HttpResponseNotAllowed, StreamingHttpResponse
from django.urls import path
from django.utils.http import content_disposition_header

from .models import BotChatEvent, FileType, Message
from .telegram import get_telegram_file_path, guess_telegram_file_mime, open_telegram_file_stream


MEDIA_TYPES = {
    "photo": FileType.PHOTO,
    "video": FileType.VIDEO,
    "video_note": FileType.VIDEO,
    "audio": FileType.AUDIO,
    "voice": FileType.AUDIO,
    "document": FileType.DOCUMENT,
    "animation": FileType.ANIMATION,
    "sticker": FileType.STICKER,
}


@dataclass(frozen=True)
class Media:
    file_id: str
    file_type: str
    file_name: str = ""

    @property
    def label(self):
        return dict(FileType.choices).get(self.file_type, "Файл")


def _file_id(value):
    # Upload names, URLs and attach:// references are not downloadable file IDs.
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,1024}", value) else ""


def _attachment(value, file_type):
    if isinstance(value, list):
        sizes = [item for item in value if isinstance(item, dict) and _file_id(item.get("file_id"))]
        if not sizes:
            return Media("", file_type)

        def size(item):
            width, height = item.get("width"), item.get("height")
            return width * height if isinstance(width, int) and isinstance(height, int) else 0

        value = max(reversed(sizes), key=size)
    if isinstance(value, dict):
        return Media(_file_id(value.get("file_id")), file_type, str(value.get("file_name") or ""))
    return Media(_file_id(value), file_type)


def event_media(event):
    """Describe attachments without fetching Telegram files or querying the archive."""
    payload = event.payload
    if not isinstance(payload, dict) or event.event_type == BotChatEvent.EventType.CALLBACK:
        return []
    if event.direction == BotChatEvent.Direction.BOT:
        saved = payload.get("telegram_media")
        if isinstance(saved, list):
            media = [
                _attachment(item, item["file_type"])
                for item in saved
                if isinstance(item, dict) and item.get("file_type") in FileType.values
                and _file_id(item.get("file_id"))
            ]
            if media:
                return media
        if payload.get("saved_photo"):
            copy = _attachment(payload["saved_photo"], FileType.PHOTO)
            if copy.file_id:
                return [copy]
        album = payload.get("media")
        if isinstance(album, list):
            return [
                _attachment(item.get("media"), MEDIA_TYPES[item["type"]])
                for item in album
                if isinstance(item, dict) and item.get("type") in MEDIA_TYPES
            ]
    # Animation messages can also have a document field referring to the same file.
    for key in ("photo", "video", "video_note", "audio", "voice", "animation", "sticker", "document"):
        if payload.get(key):
            media = _attachment(payload[key], MEDIA_TYPES[key])
            if not media.file_id and event.direction == BotChatEvent.Direction.BOT:
                source = _file_id(payload.get("source_file_id"))
                if source:
                    media = Media(source, media.file_type, media.file_name)
            return [media]
    return []


def _legacy_source(bot_user, event, media):
    # A message_id is only unique inside a chat. Never search by it globally.
    if event.direction != BotChatEvent.Direction.BOT or not bot_user.business_connection_id:
        return None
    if media.file_type not in (FileType.PHOTO, FileType.VIDEO):
        return None
    match = re.match(
        r"\A⏱ Сохранено одноразовое / view-once медиа\n"
        r"От: [^\n]* \(@([A-Za-z0-9_]+)\)\nmessage_id=([1-9][0-9]*)(?:\n|\Z)",
        event.text or "",
    )
    if not match:
        return None
    file_types = [FileType.PHOTO, FileType.SELF_DESTRUCT_PHOTO] if media.file_type == FileType.PHOTO else [FileType.VIDEO]
    candidates = list(Message.objects.filter(
        business_connection_id=bot_user.business_connection_id,
        username_from__iexact=match[1], message_id=int(match[2]), file_type__in=file_types,
    ).only("file_id", "file_type", "saved_photo_file_id", "saved_photo_source_file_id")[:2])
    if len(candidates) != 1:
        return None
    source = candidates[0]
    file_id = _file_id(source.available_photo_copy_id or source.file_id)
    return Media(file_id, source.file_type) if file_id else None


def resolve_event_media(bot_user, event, index):
    if event.chat_id != bot_user.chat_id:
        return None
    media = event_media(event)
    if index < 0 or index >= len(media):
        return None
    attachment = media[index]
    if not attachment.file_id:
        return _legacy_source(bot_user, event, attachment)
    # Prefer a durable copy when this event only recorded the original photo ID.
    if attachment.file_type == FileType.PHOTO and bot_user.business_connection_id:
        copies = list(Message.objects.filter(
            business_connection_id=bot_user.business_connection_id,
            file_id=attachment.file_id, saved_photo_source_file_id=attachment.file_id,
        ).exclude(saved_photo_file_id="").values_list("saved_photo_file_id", flat=True).distinct()[:2])
        if len(copies) == 1 and _file_id(copies[0]):
            return Media(copies[0], attachment.file_type, attachment.file_name)
    return attachment


def _error(message, status):
    response = HttpResponse(message, status=status, content_type="text/plain; charset=utf-8")
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


class DialogMediaAdminMixin:
    def get_urls(self):
        return [path(
            "<path:object_id>/dialog/<int:event_id>/media/<int:media_index>/",
            self.admin_site.admin_view(self.dialog_media_view),
            name="webhook_tg_usertg_dialog_media",
        )] + super().get_urls()

    def dialog_media_view(self, request, object_id, event_id, media_index):
        if not request.user.is_superuser:
            return HttpResponseForbidden("Диалоги доступны только суперпользователю")
        if request.method != "GET":
            return HttpResponseNotAllowed(["GET"])
        bot_user = self.get_object(request, object_id)
        if bot_user is None:
            return _error("Пользователь не найден", 404)
        event = BotChatEvent.objects.filter(pk=event_id, chat_id=bot_user.chat_id).first()
        if event is None or not 0 <= media_index < len(event_media(event)):
            return _error("Вложение не найдено", 404)
        media = resolve_event_media(bot_user, event, media_index)
        if media is None:
            return _error(
                "В старой записи не сохранился идентификатор файла, а однозначно найти "
                "оригинал в архиве не удалось. Одного имени файла для загрузки недостаточно.", 410,
            )
        try:
            file_path = get_telegram_file_path(media.file_id)
            upstream = open_telegram_file_stream(file_path)
        except Exception as exc:
            # Exceptions from requests can contain the bot token in a URL.
            detail = str(exc).lower()
            if "file is too big" in detail:
                return _error("Этот файл превышает лимит скачивания Telegram Bot API. Откройте его в Telegram.", 413)
            if "wrong file_id" in detail or "file is temporarily unavailable" in detail:
                return _error("Telegram больше не выдаёт этот файл. Доступной сохранённой копии нет.", 410)
            return _error("Не удалось загрузить файл из Telegram. Попробуйте ещё раз немного позже.", 502)

        closed = False

        def close_upstream():
            nonlocal closed
            if not closed:
                closed = True
                upstream.close()

        def stream():
            try:
                for chunk in upstream.iter_content(64 * 1024):
                    if chunk:
                        yield chunk
            finally:
                close_upstream()

        response = StreamingHttpResponse(stream(), content_type=guess_telegram_file_mime(file_path, media.file_type))
        # Also closes the upstream if the client disconnects before iteration starts.
        response._resource_closers.append(close_upstream)
        filename = media.file_name or file_path.rsplit("/", 1)[-1] or "telegram-file"
        filename = re.sub(r"[\x00-\x1f\x7f/\\]", "_", filename)
        response["Content-Disposition"] = content_disposition_header(True, filename)
        response["Cache-Control"] = "no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response
