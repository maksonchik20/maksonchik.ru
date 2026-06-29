import json
import logging
import threading

import requests

logger = logging.getLogger(__name__)


def report_who_update_event(
    *,
    chat_id,
    message_id,
    business_connection_id=None,
    username_from=None,
    first_name=None,
):
    try:
        from env import WHO_UPDATE_EVENT_URL, WHO_UPDATE_EVENT_TOKEN
    except ImportError:
        return
    if not WHO_UPDATE_EVENT_URL or not WHO_UPDATE_EVENT_TOKEN:
        return

    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "business_connection_id": business_connection_id,
        "username_from": username_from,
        "first_name": first_name,
    }

    def _post():
        try:
            requests.post(
                WHO_UPDATE_EVENT_URL,
                json=payload,
                headers={"X-Who-Update-Token": WHO_UPDATE_EVENT_TOKEN},
                timeout=3,
            )
        except Exception as exc:
            logger.warning("who_update event report failed: %s", exc)

    threading.Thread(target=_post, daemon=True).start()
