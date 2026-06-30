import logging

import requests

from env import TOKEN_BOT
import html
from .inner_models.BusinessConnection import BusinessConnection
from .bot_outgoing_log import log_bot_outgoing

logger = logging.getLogger(__name__)

api_tg_url = f"https://api.telegram.org/bot{TOKEN_BOT}"


def dispatch_telegram_request(method: str, chat_id, payload: dict, timeout: int = 5) -> tuple[bool, str]:
    if not chat_id:
        return False, "empty chat_id"

    url = f"{api_tg_url}/{method}"
    body = {"chat_id": chat_id, **payload}
    try:
        response = requests.post(url, json=body, timeout=timeout)
    except requests.RequestException as exc:
        logger.error("%s failed chat_id=%s: %s", method, chat_id, exc)
        return False, str(exc)

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


def tg_send_message(chat_id: str, text: str, timeout: int = 5) -> bool:
    if not chat_id:
        return False
    if text is None:
        return False

    ok, _ = dispatch_telegram_request(
        "sendMessage",
        chat_id,
        {
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=timeout,
    )
    return ok

def get_business_connection(msg) -> BusinessConnection:
    url = f"{api_tg_url}/getBusinessConnection"
    body = {}
    body['business_connection_id'] = msg.get("business_connection_id")
    ans = requests.post(url, json=body).json()
    print("business_connection", ans)
    user_chat_id = ans.get("result").get("user_chat_id")
    user_id = ans.get("result", {}).get("user", {}).get("id")
    username = ans.get("result", {}).get("user", {}).get("username")
    return BusinessConnection(
        user_chat_id = user_chat_id,
        user_id = user_id,
        username = username
    )

def _media_payload(caption: str) -> dict:
    payload = {}
    if caption:
        payload["caption"] = caption
        payload["parse_mode"] = "HTML"
        payload["disable_web_page_preview"] = True
    return payload


def send_photo(chat_id, photo_id: str, caption: str = "", timeout: int = 5) -> bool:
    """Отправка фото по file_id. caption — подпись к фото (HTML)."""
    if not chat_id or not photo_id:
        return False
    payload = {"photo": photo_id, **_media_payload(caption)}
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


def send_document(chat_id, document_file_id: str, caption: str = "", timeout: int = 5) -> bool:
    """Отправка документа по file_id."""
    if not chat_id or not document_file_id:
        return False
    payload = {"document": document_file_id, **_media_payload(caption)}
    ok, _ = dispatch_telegram_request("sendDocument", chat_id, payload, timeout=timeout)
    return ok


def send_photo_bytes(chat_id, image_bytes: bytes, caption: str = "", timeout: int = 60) -> bool:
    if not chat_id or not image_bytes:
        return False
    url = f"{api_tg_url}/sendPhoto"
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
    files = {"photo": ("chart.png", image_bytes, "image/png")}

    last_exc = None
    for attempt in range(3):
        try:
            response = requests.post(url, data=data, files=files, timeout=timeout)
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
    logger.error("sendPhoto bytes failed chat_id=%s: %s", chat_id, last_exc)
    return False
