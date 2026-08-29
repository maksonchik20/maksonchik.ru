from __future__ import annotations

import logging
import threading
import time
from hashlib import sha256

import requests
from requests.adapters import HTTPAdapter

from env import TOKEN_BOT
from .inner_models.BusinessConnection import BusinessConnection
from .bot_outgoing_log import log_bot_outgoing
from .metrics import (
    TELEGRAM_MESSAGES_FAILED,
    TELEGRAM_MESSAGES_SENT,
    TELEGRAM_SEND_ATTEMPTS,
    TELEGRAM_SEND_DURATION,
    TELEGRAM_SUCCESS_RATE,
    observe_metric,
)

logger = logging.getLogger(__name__)

# Сначала используем канонический Bot API. Прямые IP оставляем как fallback
# на случай проблем с DNS или маршрутизацией до api.telegram.org.
TELEGRAM_API_ENDPOINTS = (
    {"base": "https://api.telegram.org", "host": None, "verify": True, "connect_timeout": 2},
    {"base": "https://149.154.167.220", "host": "api.telegram.org", "verify": False, "connect_timeout": 0.75},
    {"base": "https://149.154.167.99", "host": "api.telegram.org", "verify": False, "connect_timeout": 0.75},
)
TELEGRAM_API_TIMEOUT = 8
TELEGRAM_CONNECT_TIMEOUT = 2
TELEGRAM_CIRCUIT_COOLDOWN = 30

TELEGRAM_SESSION = requests.Session()
TELEGRAM_SESSION.mount("https://", HTTPAdapter(pool_connections=10, pool_maxsize=20))

_endpoint_failures: dict[str, float] = {}
_endpoint_failures_lock = threading.Lock()


def _safe_telegram_error(error) -> str:
    """requests включает полный URL (и bot token) в текст исключения."""
    return str(error).replace(str(TOKEN_BOT), "<redacted>")

# Для тестов/обратной совместимости: канонический URL без обхода DNS.
api_tg_url = f"https://api.telegram.org/bot{TOKEN_BOT}"


def _endpoint_is_available(base: str, now: float) -> bool:
    with _endpoint_failures_lock:
        return _endpoint_failures.get(base, 0) <= now


def _mark_endpoint_failed(base: str) -> None:
    with _endpoint_failures_lock:
        _endpoint_failures[base] = time.monotonic() + TELEGRAM_CIRCUIT_COOLDOWN


def _mark_endpoint_healthy(base: str) -> None:
    with _endpoint_failures_lock:
        _endpoint_failures.pop(base, None)


def telegram_webhook_secret() -> str:
    """Стабильный secret_token webhook без хранения дополнительного секрета."""
    return sha256(f"who-update:{TOKEN_BOT}".encode()).hexdigest()


def _telegram_post(method: str, *, json=None, data=None, files=None, timeout: int = TELEGRAM_API_TIMEOUT):
    """POST к Bot API с connection pool и circuit breaker для endpoint'ов."""
    started_at = time.monotonic()
    is_send = method.startswith("send")
    if is_send:
        observe_metric(TELEGRAM_SEND_ATTEMPTS, 1, {"method": method})
    last_error = None
    attempted = False
    now = time.monotonic()
    for endpoint in TELEGRAM_API_ENDPOINTS:
        if not _endpoint_is_available(endpoint["base"], now):
            continue
        attempted = True
        url = f"{endpoint['base'].rstrip('/')}/bot{TOKEN_BOT}/{method}"
        headers = {}
        host = endpoint.get("host")
        if host:
            headers["Host"] = host
        try:
            response = TELEGRAM_SESSION.post(
                url,
                json=json,
                data=data,
                files=files,
                headers=headers or None,
                timeout=(endpoint.get("connect_timeout", TELEGRAM_CONNECT_TIMEOUT), timeout),
                verify=endpoint.get("verify", True),
            )
            _mark_endpoint_healthy(endpoint["base"])
            if is_send:
                try:
                    success = bool(response.json().get("ok"))
                except ValueError:
                    success = False
                labels = {"method": method, "status": "success" if success else "error"}
                observe_metric(
                    TELEGRAM_SEND_DURATION,
                    (time.monotonic() - started_at) * 1000,
                    labels,
                )
                if success:
                    observe_metric(TELEGRAM_MESSAGES_SENT, 1, {"method": method})
                else:
                    observe_metric(TELEGRAM_MESSAGES_FAILED, 1, {"method": method, "reason": "api"})
                observe_metric(TELEGRAM_SUCCESS_RATE, 100 if success else 0, {"method": method})
            return response
        except requests.RequestException as exc:
            last_error = exc
            _mark_endpoint_failed(endpoint["base"])
            logger.warning(
                "Telegram API %s via %s failed: %s",
                method,
                endpoint["base"],
                _safe_telegram_error(exc),
            )
    if last_error:
        if is_send:
            observe_metric(
                TELEGRAM_SEND_DURATION,
                (time.monotonic() - started_at) * 1000,
                {"method": method, "status": "error"},
            )
            observe_metric(TELEGRAM_MESSAGES_FAILED, 1, {"method": method, "reason": "network"})
            observe_metric(TELEGRAM_SUCCESS_RATE, 0, {"method": method})
        raise requests.ConnectionError(_safe_telegram_error(last_error))
    if not attempted:
        if is_send:
            observe_metric(
                TELEGRAM_SEND_DURATION,
                (time.monotonic() - started_at) * 1000,
                {"method": method, "status": "error"},
            )
            observe_metric(TELEGRAM_MESSAGES_FAILED, 1, {"method": method, "reason": "circuit_breaker"})
            observe_metric(TELEGRAM_SUCCESS_RATE, 0, {"method": method})
        raise requests.ConnectionError("Telegram API endpoints temporarily disabled by circuit breaker")
    raise RuntimeError("Telegram API недоступен")


def dispatch_telegram_request(method: str, chat_id, payload: dict, timeout: int = 5) -> tuple[bool, str]:
    if not chat_id:
        return False, "empty chat_id"

    body = {"chat_id": chat_id, **payload}
    try:
        response = _telegram_post(method, json=body, timeout=timeout)
    except requests.RequestException as exc:
        logger.error("%s failed chat_id=%s: %s", method, chat_id, _safe_telegram_error(exc))
        return False, _safe_telegram_error(exc)

    try:
        result = response.json()
    except ValueError:
        error = f"invalid JSON status={response.status_code} body={response.text[:200]}"
        logger.error("%s %s chat_id=%s", method, error, chat_id)
        return False, error

    if not result.get("ok"):
        error = str(result.get("description") or result)
        logger.error("%s API error chat_id=%s status=%s response=%s", method, chat_id, response.status_code, result)
        return False, error

    log_bot_outgoing(chat_id=chat_id, method=method)
    return True, ""


def tg_send_message(chat_id: str, text: str, timeout: int = 5, reply_markup: dict | None = None) -> bool:
    if not chat_id:
        return False
    if text is None:
        return False

    payload = {
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    ok, _ = dispatch_telegram_request(
        "sendMessage",
        chat_id,
        payload,
        timeout=timeout,
    )
    return ok


def tg_send_business_message(
    business_connection_id: str,
    chat_id,
    text: str,
    timeout: int = 8,
) -> tuple[bool, str]:
    """Отправка сообщения в business-чат от имени владельца аккаунта (нужен can_reply)."""
    if not business_connection_id or not chat_id or text is None:
        return False, "empty args"
    body = {
        "business_connection_id": business_connection_id,
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        response = _telegram_post("sendMessage", json=body, timeout=timeout)
    except requests.RequestException as exc:
        logger.error("business sendMessage failed: %s", _safe_telegram_error(exc))
        return False, _safe_telegram_error(exc)
    try:
        result = response.json()
    except ValueError:
        return False, f"invalid JSON status={response.status_code}"
    if not result.get("ok"):
        error = str(result.get("description") or result)
        logger.error("business sendMessage API error: %s", error)
        return False, error
    log_bot_outgoing(chat_id=chat_id, method="sendMessage")
    return True, ""


def answer_callback_query(callback_query_id: str, text: str = "", timeout: int = 5) -> bool:
    if not callback_query_id:
        return False
    body = {"callback_query_id": callback_query_id}
    if text:
        body["text"] = text
    try:
        response = _telegram_post("answerCallbackQuery", json=body, timeout=timeout)
        result = response.json()
        return bool(result.get("ok"))
    except (requests.RequestException, ValueError) as exc:
        logger.error("answerCallbackQuery failed: %s", _safe_telegram_error(exc))
        return False


def edit_message_text(
    chat_id,
    message_id: int,
    text: str,
    reply_markup: dict | None = None,
    timeout: int = 8,
) -> bool:
    if not chat_id or message_id is None or text is None:
        return False
    body = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        body["reply_markup"] = reply_markup
    try:
        response = _telegram_post("editMessageText", json=body, timeout=timeout)
        result = response.json()
        if not result.get("ok"):
            logger.error("editMessageText API error: %s", result)
            return False
        return True
    except (requests.RequestException, ValueError) as exc:
        logger.error("editMessageText failed: %s", _safe_telegram_error(exc))
        return False


def get_business_connection(msg) -> BusinessConnection:
    body = {"business_connection_id": msg.get("business_connection_id")}
    response = _telegram_post("getBusinessConnection", json=body, timeout=TELEGRAM_API_TIMEOUT)
    ans = response.json()
    print("business_connection", ans)
    result = ans.get("result") or {}
    user = result.get("user") or {}
    return BusinessConnection(
        user_chat_id=result.get("user_chat_id"),
        user_id=user.get("id"),
        username=user.get("username"),
    )


# Типы апдейтов WhoUpdate (бот + Telegram Business).
WHO_UPDATE_ALLOWED_UPDATES = (
    "message",
    "edited_message",
    "callback_query",
    "pre_checkout_query",
    "business_connection",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
)


def delete_telegram_webhook(*, drop_pending_updates: bool = True, timeout: int = 15) -> dict:
    """Отключает webhook — без этого getUpdates не работает."""
    payload = {}
    if drop_pending_updates:
        payload["drop_pending_updates"] = True
    response = _telegram_post("deleteWebhook", json=payload, timeout=timeout)
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"deleteWebhook failed: {result}")
    return result


def set_telegram_webhook(
    url: str,
    *,
    drop_pending_updates: bool = False,
    max_connections: int = 10,
    timeout: int = 15,
) -> dict:
    payload = {
        "url": url,
        "secret_token": telegram_webhook_secret(),
        "allowed_updates": list(WHO_UPDATE_ALLOWED_UPDATES),
        "drop_pending_updates": drop_pending_updates,
        "max_connections": max_connections,
    }
    response = _telegram_post("setWebhook", json=payload, timeout=timeout)
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"setWebhook failed: {result}")
    return result


def get_telegram_webhook_info(timeout: int = 10) -> dict:
    response = _telegram_post("getWebhookInfo", json={}, timeout=timeout)
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"getWebhookInfo failed: {result}")
    return result


def set_bot_commands(commands: list[dict], timeout: int = 10) -> dict:
    response = _telegram_post("setMyCommands", json={"commands": commands}, timeout=timeout)
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"setMyCommands failed: {result}")
    return result


def get_telegram_updates(
    *,
    offset: int = 0,
    poll_timeout: int = 25,
    timeout: int | None = None,
    allowed_updates=None,
) -> dict:
    """Long poll getUpdates. timeout HTTP > poll_timeout."""
    if timeout is None:
        timeout = poll_timeout + 10
    if allowed_updates is None:
        allowed_updates = list(WHO_UPDATE_ALLOWED_UPDATES)
    response = _telegram_post(
        "getUpdates",
        json={
            "offset": offset,
            "timeout": poll_timeout,
            "allowed_updates": allowed_updates,
        },
        timeout=timeout,
    )
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"getUpdates failed: {result}")
    return result


def _media_payload(caption: str) -> dict:
    payload = {}
    if caption:
        payload["caption"] = caption
        payload["parse_mode"] = "HTML"
        payload["disable_web_page_preview"] = True
    return payload


def send_photo(
    chat_id,
    photo_id: str,
    caption: str = "",
    timeout: int = 5,
    reply_markup: dict | None = None,
) -> bool:
    """Отправка фото по file_id. caption — подпись к фото (HTML)."""
    if not chat_id or not photo_id:
        return False
    payload = {"photo": photo_id, **_media_payload(caption)}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    ok, _ = dispatch_telegram_request("sendPhoto", chat_id, payload, timeout=timeout)
    return ok


def send_audio(chat_id, audio_file_id: str, caption: str = "", timeout: int = 5) -> bool:
    """Отправка аудио/голоса по file_id."""
    if not chat_id or not audio_file_id:
        return False
    payload = {"audio": audio_file_id, **_media_payload(caption)}
    ok, _ = dispatch_telegram_request("sendAudio", chat_id, payload, timeout=timeout)
    return ok


def send_video(chat_id, video_file_id: str, caption: str = "", timeout: int = 5) -> bool:
    """Отправка видео по file_id."""
    if not chat_id or not video_file_id:
        return False
    payload = {"video": video_file_id, **_media_payload(caption)}
    ok, _ = dispatch_telegram_request("sendVideo", chat_id, payload, timeout=timeout)
    return ok


def send_video_group(chat_id, videos: list[dict], timeout: int = 30) -> bool:
    """Отправка 2–10 видео одним Telegram-альбомом по существующим file_id."""
    if not chat_id or not 2 <= len(videos) <= 10:
        return False
    media = []
    for video in videos:
        file_id = video.get("file_id")
        if not file_id:
            return False
        item = {"type": "video", "media": file_id}
        caption = video.get("caption") or ""
        if caption:
            item.update({"caption": caption, "parse_mode": "HTML"})
        media.append(item)
    ok, _ = dispatch_telegram_request(
        "sendMediaGroup",
        chat_id,
        {"media": media},
        timeout=timeout,
    )
    return ok


def send_document(chat_id, document_file_id: str, caption: str = "", timeout: int = 5) -> bool:
    """Отправка документа по file_id."""
    if not chat_id or not document_file_id:
        return False
    payload = {"document": document_file_id, **_media_payload(caption)}
    ok, _ = dispatch_telegram_request("sendDocument", chat_id, payload, timeout=timeout)
    return ok


def send_document_bytes(
    chat_id,
    document_bytes: bytes,
    *,
    filename: str = "messages.txt",
    caption: str = "",
    content_type: str = "text/plain; charset=utf-8",
    timeout: int = 60,
) -> bool:
    """Загружает и отправляет документ из памяти."""
    if not chat_id or not document_bytes:
        return False

    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
        data["parse_mode"] = "HTML"
    files = {
        "document": (
            filename or "messages.txt",
            document_bytes,
            content_type or "application/octet-stream",
        )
    }

    try:
        response = _telegram_post("sendDocument", data=data, files=files, timeout=timeout)
        result = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.error("sendDocument bytes failed chat_id=%s: %s", chat_id, _safe_telegram_error(exc))
        return False

    if not result.get("ok"):
        logger.error("sendDocument bytes API error chat_id=%s response=%s", chat_id, result)
        return False
    log_bot_outgoing(chat_id=chat_id, method="sendDocument")
    return True


def delete_business_messages(
    business_connection_id: str,
    message_ids: list[int],
    timeout: int = 8,
) -> tuple[bool, str]:
    """Удаляет сообщения от имени business-аккаунта (нужно can_delete_all_messages)."""
    if not business_connection_id or not message_ids:
        return False, "empty args"
    body = {
        "business_connection_id": business_connection_id,
        "message_ids": list(message_ids),
    }
    try:
        response = _telegram_post("deleteBusinessMessages", json=body, timeout=timeout)
    except requests.RequestException as exc:
        logger.error("deleteBusinessMessages failed: %s", _safe_telegram_error(exc))
        return False, _safe_telegram_error(exc)
    try:
        result = response.json()
    except ValueError:
        return False, f"invalid JSON status={response.status_code}"
    if not result.get("ok"):
        error = str(result.get("description") or result)
        logger.error("deleteBusinessMessages API error: %s", error)
        return False, error
    return True, ""


def send_photo_bytes(
    chat_id,
    image_bytes: bytes,
    caption: str = "",
    timeout: int = 60,
    filename: str = "photo.jpg",
    content_type: str = "image/jpeg",
) -> bool:
    if not chat_id or not image_bytes:
        return False
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
        data["parse_mode"] = "HTML"
    files = {"photo": (filename or "photo.jpg", image_bytes, content_type or "image/jpeg")}

    last_exc = None
    for attempt in range(3):
        try:
            response = _telegram_post("sendPhoto", data=data, files=files, timeout=timeout)
            result = response.json()
            if result.get("ok"):
                log_bot_outgoing(chat_id=chat_id, method="sendPhoto")
                return True
            logger.error("sendPhoto bytes API error chat_id=%s response=%s", chat_id, result)
            return False
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            logger.warning(
                "sendPhoto bytes attempt %s failed chat_id=%s: %s",
                attempt + 1,
                chat_id,
                exc,
            )
    logger.error("sendPhoto bytes failed chat_id=%s: %s", chat_id, _safe_telegram_error(last_exc))
    return False


def send_video_bytes(
    chat_id,
    video_bytes: bytes,
    caption: str = "",
    timeout: int = 120,
    filename: str = "video.mp4",
    content_type: str = "video/mp4",
) -> bool:
    if not chat_id or not video_bytes:
        return False
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
        data["parse_mode"] = "HTML"
    files = {"video": (filename or "video.mp4", video_bytes, content_type or "video/mp4")}

    last_exc = None
    for attempt in range(3):
        try:
            response = _telegram_post("sendVideo", data=data, files=files, timeout=timeout)
            result = response.json()
            if result.get("ok"):
                log_bot_outgoing(chat_id=chat_id, method="sendVideo")
                return True
            logger.error("sendVideo bytes API error chat_id=%s response=%s", chat_id, result)
            return False
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            logger.warning(
                "sendVideo bytes attempt %s failed chat_id=%s: %s",
                attempt + 1,
                chat_id,
                exc,
            )
    logger.error("sendVideo bytes failed chat_id=%s: %s", chat_id, _safe_telegram_error(last_exc))
    return False


def download_telegram_file_bytes(file_id: str, timeout: int = 60) -> tuple[bytes, str]:
    """
    Скачивает файл Bot API по file_id.
    Нужно для SelfDestructingPhoto/Video: их file_id нельзя переиспользовать в sendPhoto/sendVideo.
    Возвращает (bytes, file_path).
    """
    file_path = get_telegram_file_path(file_id, timeout=min(timeout, 30))
    response = open_telegram_file_stream(file_path, timeout=timeout)
    try:
        content = response.content
    finally:
        response.close()
    if not content:
        raise RuntimeError("empty file download")
    return content, file_path


def _guess_media_filename(file_path: str, file_type: str) -> tuple[str, str]:
    name = (file_path or "").rsplit("/", 1)[-1] or ""
    lower = name.lower()
    if file_type == "PHOTO":
        if not lower.endswith((".jpg", ".jpeg", ".png", ".webp")):
            name = "photo.jpg"
        return name, "image/jpeg" if not lower.endswith(".png") else "image/png"
    if file_type == "VIDEO":
        if not lower.endswith((".mp4", ".mov", ".mkv", ".webm")):
            name = "video.mp4"
        return name, "video/mp4"
    return name or "file.bin", "application/octet-stream"


_MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".pdf": "application/pdf",
    ".tgs": "application/x-tgsticker",
}


def guess_telegram_file_mime(file_path: str, file_type: str | None = None) -> str:
    path = (file_path or "").lower()
    for ext, mime in _MIME_BY_EXT.items():
        if path.endswith(ext):
            return mime
    ft = (file_type or "").upper()
    if ft == "PHOTO":
        return "image/jpeg"
    if ft == "VIDEO":
        return "video/mp4"
    if ft == "AUDIO":
        return "audio/ogg"
    if ft == "ANIMATION":
        return "video/mp4"
    if ft == "STICKER":
        return "image/webp"
    return "application/octet-stream"


def get_telegram_file_path(file_id: str, timeout: int = 20) -> str:
    """getFile → file_path. Файл на диск не пишется."""
    if not file_id:
        raise ValueError("empty file_id")
    response = _telegram_post("getFile", json={"file_id": file_id}, timeout=timeout)
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(str(result.get("description") or result))
    file_path = (result.get("result") or {}).get("file_path")
    if not file_path:
        raise RuntimeError("getFile: empty file_path")
    return file_path


def open_telegram_file_stream(file_path: str, timeout: int = 60):
    """
    Открывает streaming GET к файлу Bot API.
    Возвращает requests.Response (stream=True) — вызывающий должен закрыть.
    """
    last_error = None
    now = time.monotonic()
    for endpoint in TELEGRAM_API_ENDPOINTS:
        if not _endpoint_is_available(endpoint["base"], now):
            continue
        url = f"{endpoint['base'].rstrip('/')}/file/bot{TOKEN_BOT}/{file_path.lstrip('/')}"
        headers = {}
        host = endpoint.get("host")
        if host:
            headers["Host"] = host
        try:
            response = TELEGRAM_SESSION.get(
                url,
                headers=headers or None,
                timeout=(endpoint.get("connect_timeout", TELEGRAM_CONNECT_TIMEOUT), timeout),
                verify=endpoint.get("verify", True),
                stream=True,
            )
            if response.status_code >= 400:
                response.close()
                last_error = RuntimeError(f"download status={response.status_code}")
                continue
            _mark_endpoint_healthy(endpoint["base"])
            return response
        except requests.RequestException as exc:
            last_error = exc
            _mark_endpoint_failed(endpoint["base"])
            logger.warning(
                "Telegram file download via %s failed: %s",
                endpoint["base"],
                _safe_telegram_error(exc),
            )
    if last_error:
        raise requests.ConnectionError(_safe_telegram_error(last_error))
    raise RuntimeError("Telegram file download failed")
