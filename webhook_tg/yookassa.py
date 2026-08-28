import base64
import ipaddress
import uuid
from decimal import Decimal

import requests
from django.conf import settings


API_URL = "https://api.yookassa.ru/v3/payments"
WEBHOOK_NETWORKS = [
    ipaddress.ip_network("185.71.76.0/27"),
    ipaddress.ip_network("185.71.77.0/27"),
    ipaddress.ip_network("77.75.153.0/25"),
    ipaddress.ip_network("77.75.154.128/25"),
    ipaddress.ip_network("2a02:5180::/32"),
]
WEBHOOK_ADDRESSES = {
    ipaddress.ip_address("77.75.156.11"),
    ipaddress.ip_address("77.75.156.35"),
}


class YooKassaError(Exception):
    pass


def _credentials():
    shop_id = str(getattr(settings, "YOOKASSA_SHOP_ID", "") or "").strip()
    secret_key = str(getattr(settings, "YOOKASSA_SECRET_KEY", "") or "").strip()
    if not shop_id or not secret_key:
        raise YooKassaError("ЮKassa не настроена")
    return shop_id, secret_key


def _headers(idempotence_key=None):
    shop_id, secret_key = _credentials()
    encoded = base64.b64encode(f"{shop_id}:{secret_key}".encode()).decode("ascii")
    headers = {"Authorization": f"Basic {encoded}", "Content-Type": "application/json"}
    if idempotence_key:
        headers["Idempotence-Key"] = idempotence_key
    return headers


def _raise_for_response(response):
    if response.status_code in (200, 201):
        return
    raise YooKassaError(f"ЮKassa HTTP {response.status_code}: {response.text[:500]}")


def create_payment(*, amount, description, return_url, metadata):
    response = requests.post(
        API_URL,
        json={
            "amount": {"value": f"{Decimal(amount):.2f}", "currency": "RUB"},
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": return_url},
            "description": description[:128],
            "metadata": metadata,
        },
        headers=_headers(str(uuid.uuid4())),
        timeout=30,
    )
    _raise_for_response(response)
    data = response.json()
    return {
        "id": data.get("id", ""),
        "confirmation_url": (data.get("confirmation") or {}).get("confirmation_url", ""),
    }


def get_payment(payment_id):
    response = requests.get(
        f"{API_URL}/{payment_id}",
        headers=_headers(),
        timeout=30,
    )
    _raise_for_response(response)
    return response.json()


def is_webhook_ip(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address in WEBHOOK_ADDRESSES or any(address in network for network in WEBHOOK_NETWORKS)
