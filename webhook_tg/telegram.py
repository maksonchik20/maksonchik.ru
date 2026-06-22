import logging

import requests

from env import TOKEN_BOT
import html
from .inner_models.BusinessConnection import BusinessConnection

logger = logging.getLogger(__name__)

api_tg_url = f"https://api.telegram.org/bot{TOKEN_BOT}"

def tg_send_message(chat_id: str, text: str, timeout: int = 5) -> bool:
    if not chat_id:
        return False
    if text is None:
        return False

    url = f"{api_tg_url}/sendMessage"
    body = {}
    body['chat_id'] = chat_id
    body['text'] = text
    body['parse_mode'] = "HTML"
    body['disable_web_page_preview'] = True
    try:
        response = requests.post(
            url,
            json=body,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        logger.error("sendMessage failed chat_id=%s: %s", chat_id, exc)
        return False

    try:
        payload = response.json()
    except ValueError:
        logger.error(
            "sendMessage invalid JSON chat_id=%s status=%s body=%s",
            chat_id,
            response.status_code,
            response.text[:500],
        )
        return False

    if not payload.get("ok"):
        logger.error(
            "sendMessage API error chat_id=%s status=%s response=%s",
            chat_id,
            response.status_code,
            payload,
        )
        return False

    return True

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

def send_photo(chat_id, photo_id: str, caption: str = "", timeout: int = 1) -> None:
    """Отправка фото по file_id. caption — подпись к фото (HTML)."""
    if not chat_id or not photo_id:
        return
    url = f"{api_tg_url}/sendPhoto"
    body = {"chat_id": chat_id, "photo": photo_id}
    if caption:
        body["caption"] = caption
        body["parse_mode"] = "HTML"
        body["disable_web_page_preview"] = True
    requests.post(url, json=body, timeout=timeout)


def send_audio(chat_id, audio_file_id: str, caption: str = "", timeout: int = 1) -> None:
    """Отправка аудио/голоса по file_id."""
    if not chat_id or not audio_file_id:
        return
    url = f"{api_tg_url}/sendAudio"
    body = {"chat_id": chat_id, "audio": audio_file_id}
    if caption:
        body["caption"] = caption
        body["parse_mode"] = "HTML"
    requests.post(url, json=body, timeout=timeout)


def send_video(chat_id, video_file_id: str, caption: str = "", timeout: int = 1) -> None:
    """Отправка видео по file_id."""
    if not chat_id or not video_file_id:
        return
    url = f"{api_tg_url}/sendVideo"
    body = {"chat_id": chat_id, "video": video_file_id}
    if caption:
        body["caption"] = caption
        body["parse_mode"] = "HTML"
    requests.post(url, json=body, timeout=timeout)


def send_document(chat_id, document_file_id: str, caption: str = "", timeout: int = 1) -> None:
    """Отправка документа по file_id."""
    if not chat_id or not document_file_id:
        return
    url = f"{api_tg_url}/sendDocument"
    body = {"chat_id": chat_id, "document": document_file_id}
    if caption:
        body["caption"] = caption
        body["parse_mode"] = "HTML"
    requests.post(url, json=body, timeout=timeout)
