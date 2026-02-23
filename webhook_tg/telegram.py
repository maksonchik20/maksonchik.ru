import requests

from env import TOKEN_BOT
import html
from .inner_models.BusinessConnection import BusinessConnection

api_tg_url = f"https://api.telegram.org/bot{TOKEN_BOT}"

def tg_send_message(chat_id: str, text: str, timeout: int = 3) -> None:
    if not chat_id:
        return

    url = f"{api_tg_url}/sendMessage"
    body = {}
    body['chat_id'] = chat_id
    body['text'] = text
    body['parse_mode'] = "HTML"
    body['disable_web_page_preview'] = True
    requests.post(
        url,
        json=body,
        timeout=timeout,
    )

def get_business_connection(msg) -> BusinessConnection:
    url = f"{api_tg_url}/getBusinessConnection"
    body = {}
    body['business_connection_id'] = msg.get("business_connection_id")
    ans = requests.post(url, json=body).json()
    print("business_connection", ans)
    user_chat_id = ans.get("result").get("user_chat_id")
    user_id = ans.get("result", {}).get("user", {}).get("id")
    return BusinessConnection(
        user_chat_id = user_chat_id,
        user_id = user_id
        )

def send_photo(text: str, chat_id: int, photo_id: str, timeout: int = 3) -> None:
    url = f"{api_tg_url}/sendPhoto"
    body = {}
    body['chat_id'] = chat_id
    body['caption'] = text
    body['parse_mode'] = "HTML"
    body['disable_web_page_preview'] = True
    body['photo'] = photo_id
    requests.post(
        url,
        json=body,
        timeout=timeout,
    )
