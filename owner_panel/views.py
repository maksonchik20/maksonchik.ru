import base64
import hashlib
import json
import uuid
from datetime import timedelta
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse
import requests
from django.contrib.auth import authenticate, login, logout
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from . import stats
from .config import config
from .models import (
    PushSubscription,
    PushEvent,
    PushDelivery,
    NotificationState,
    PanelAudit,
    LoginThrottle,
)


def owner_only(fn):
    @wraps(fn)
    @never_cache
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Требуется вход."}, status=401)
        if not request.user.is_active or not request.user.is_superuser:
            return JsonResponse({"error": "Доступ только владельцу."}, status=403)
        return fn(request, *args, **kwargs)

    return wrapped


@never_cache
@ensure_csrf_cookie
@require_GET
def index(request):
    if not request.user.is_authenticated:
        return redirect("owner_panel:login")
    if not request.user.is_active or not request.user.is_superuser:
        return HttpResponse("Доступ только владельцу.", status=403)
    return render(request, "owner_panel/index.html")


@never_cache
@require_http_methods(["GET", "POST"])
def sign_in(request):
    error = ""
    if request.method == "POST":
        username = (request.POST.get("username") or "")[:150]
        key = hashlib.sha256(username.casefold().encode()).hexdigest()
        now = timezone.now()
        with transaction.atomic():
            row, _ = LoginThrottle.objects.get_or_create(key=key)
            row = LoginThrottle.objects.select_for_update().get(pk=key)
            if now - row.window_started_at > timedelta(minutes=10):
                row.failures = 0
                row.window_started_at = now
            if row.failures >= 8:
                error = "Слишком много попыток. Повторите через 10 минут."
            else:
                user = authenticate(
                    request,
                    username=username,
                    password=request.POST.get("password", ""),
                )
                if user and user.is_active and user.is_superuser:
                    row.failures = 0
                    row.save()
                    login(request, user)
                    request.session.set_expiry(30 * 24 * 3600)
                    return redirect("owner_panel:index")
                row.failures += 1
                error = "Не удалось войти. Проверьте логин и пароль администратора."
            row.save()
    return render(request, "owner_panel/login.html", {"error": error})


@owner_only
@require_POST
def sign_out(request):
    try:
        endpoint = json.loads(request.body or "{}").get("endpoint", "")
        if endpoint:
            PushSubscription.objects.filter(
                user=request.user,
                endpoint_hash=hashlib.sha256(endpoint.encode()).hexdigest(),
            ).update(enabled=False)
    except (ValueError, TypeError, AttributeError):
        pass
    logout(request)
    return JsonResponse({"ok": True})


def ghost_request(action, params=None, data=None):
    conf = config()
    if not conf.get("bridge_token"):
        return {"error": "Соединение с GhostNote не настроено."}, 503
    try:
        response = requests.request(
            "POST" if data is not None else "GET",
            conf.get("ghost_url", "https://yc.maksonchik.ru")
            + "/ghost/owner-api/"
            + action
            + "/",
            params=params,
            json=data,
            headers={"X-Owner-Panel-Key": conf["bridge_token"]},
            timeout=(5, 25),
            allow_redirects=False,
        )
        if response.status_code >= 500:
            return {"error": "GhostNote временно недоступен. Попробуйте обновить."}, 502
        payload = response.json()
        return payload, response.status_code
    except (requests.RequestException, ValueError):
        return {
            "error": "Не удалось получить данные GhostNote. Попробуйте обновить."
        }, 502


@owner_only
@require_GET
def data(request, project, section):
    if project == "ghost":
        if section not in ("dashboard", "referrals", "tokens", "token", "users"):
            return JsonResponse({"error": "Not found"}, status=404)
        result, status = ghost_request(section, request.GET)
        return JsonResponse(result, status=status)
    if project != "who":
        return JsonResponse({"error": "Not found"}, status=404)
    try:
        if section == "dashboard":
            return JsonResponse(stats.dashboard(request.GET))
        if section == "referrals":
            return JsonResponse(stats.referrals())
        if section == "tokens":
            return JsonResponse(stats.access_list(request.GET))
    except (ValueError, TypeError):
        return JsonResponse(
            {"error": "Проверьте период и параметры фильтра."}, status=400
        )
    return JsonResponse({"error": "Not found"}, status=404)


@owner_only
@require_POST
def create_token(request):
    if len(request.body) > 10000:
        return JsonResponse({"error": "Слишком большой запрос."}, status=413)
    try:
        payload = json.loads(request.body)
        if not isinstance(payload, dict):
            raise ValueError()
        request_id = uuid.UUID(payload.get("request_id", ""))
        if type(payload.get("allow_ai", False)) is not bool:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        return JsonResponse({"error": "Некорректный запрос."}, status=400)
    # Stable across retries; actor is supplied by the authenticated server, not the client.
    payload["actor"] = request.user.username
    payload.setdefault("allow_ai", False)
    result, status = ghost_request("create-token", data=payload)
    if status in (200, 201):
        PanelAudit.objects.get_or_create(
            request_id=request_id,
            defaults={
                "user": request.user,
                "action": "ghost.create_token",
                "object_id": str(result["id"]),
            },
        )
    return JsonResponse(result, status=status)


@owner_only
@require_POST
def ghost_payout(request):
    if len(request.body) > 2000:
        return JsonResponse({"error": "Слишком большой запрос."}, status=413)
    try:
        payload = json.loads(request.body)
        if (
            not isinstance(payload, dict)
            or type(payload.get("id")) is not int
            or not 0 < payload["id"] <= 9223372036854775807
            or type(payload.get("is_paid")) is not bool
            or not isinstance(payload.get("revision"), str)
            or len(payload["revision"]) != 64
        ):
            raise ValueError()
    except (ValueError, TypeError):
        return JsonResponse({"error": "Некорректная отметка о выплате."}, status=400)
    result, status = ghost_request(
        "set-payout",
        data={
            "id": payload["id"],
            "is_paid": payload["is_paid"],
            "revision": payload["revision"],
        },
    )
    if status == 200:
        PanelAudit.objects.create(
            user=request.user,
            action="ghost.payout_paid" if payload["is_paid"] else "ghost.payout_unpaid",
            object_id=str(payload["id"]),
        )
    return JsonResponse(result, status=status)


@owner_only
@require_POST
def update_token(request):
    if len(request.body) > 4000:
        return JsonResponse({"error": "Слишком большой запрос."}, status=413)
    try:
        payload = json.loads(request.body)
        if (
            not isinstance(payload, dict)
            or set(payload) - {"allow_ai"}
            != {
                "id",
                "revision",
                "label",
                "starts_at",
                "expires_at",
                "is_active",
                "allow_local",
                "allow_remote",
            }
            or type(payload.get("id")) is not int
            or not 0 < payload["id"] <= 9223372036854775807
            or not isinstance(payload.get("revision"), str)
            or len(payload["revision"]) != 64
            or any(
                type(payload.get(flag)) is not bool
                for flag in ("is_active", "allow_local", "allow_remote")
            )
            or ("allow_ai" in payload and type(payload["allow_ai"]) is not bool)
        ):
            raise ValueError()
    except (ValueError, TypeError):
        return JsonResponse({"error": "Некорректные параметры токена."}, status=400)
    result, status = ghost_request("update-token", data=payload)
    if status == 200:
        PanelAudit.objects.create(
            user=request.user, action="ghost.update_token", object_id=str(payload["id"])
        )
    return JsonResponse(result, status=status)


def valid_subscription(info):
    if not isinstance(info, dict):
        raise ValueError("Некорректная подписка.")
    endpoint = info.get("endpoint", "")
    parsed = urlparse(endpoint)
    host = parsed.hostname or ""
    allowed = (
        host.endswith(".push.apple.com")
        or host in ("fcm.googleapis.com", "updates.push.services.mozilla.com")
        or host.endswith(".notify.windows.com")
    )
    if (
        parsed.scheme != "https"
        or not allowed
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or len(endpoint) > 2048
    ):
        raise ValueError("Сервис уведомлений не поддерживается.")
    keys = info.get("keys", {})

    def decode(value):
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    if (
        len(decode(keys.get("p256dh", ""))) != 65
        or len(decode(keys.get("auth", ""))) != 16
    ):
        raise ValueError("Некорректные ключи уведомлений.")
    return {
        "endpoint": endpoint,
        "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]},
    }


@owner_only
@require_http_methods(["GET", "POST", "DELETE"])
def push_subscription(request):
    if request.method == "GET":
        states = list(
            NotificationState.objects.values("name", "last_poll_at", "last_error")
        )
        return JsonResponse(
            {
                "public_key": config().get("vapid_public_key", ""),
                "enabled_devices": PushSubscription.objects.filter(
                    user=request.user, enabled=True
                ).count(),
                "states": states,
                "failed_deliveries": PushDelivery.objects.filter(
                    subscription__user=request.user, status="failed"
                ).count(),
            }
        )
    if len(request.body) > 8192:
        return JsonResponse({"error": "Слишком большой запрос."}, status=413)
    try:
        info = json.loads(request.body)
        if request.method == "DELETE":
            endpoint = info["endpoint"]
            PushSubscription.objects.filter(
                user=request.user,
                endpoint_hash=hashlib.sha256(endpoint.encode()).hexdigest(),
            ).update(enabled=False)
            return JsonResponse({"ok": True})
        info = valid_subscription(info)
        if not config().get("vapid_public_key"):
            return JsonResponse({"error": "Пуши пока не настроены."}, status=503)
        row, created = PushSubscription.objects.get_or_create(
            endpoint_hash=hashlib.sha256(info["endpoint"].encode()).hexdigest(),
            defaults={"user": request.user, "subscription": info},
        )
        if row.user_id != request.user.pk:
            return JsonResponse(
                {"error": "Устройство связано с другим аккаунтом."}, status=409
            )
        if not row.enabled:
            row.created_at = timezone.now()
        row.subscription = info
        row.enabled = True
        row.save()
        return JsonResponse({"ok": True})
    except (ValueError, TypeError, KeyError, AttributeError):
        return JsonResponse({"error": "Не удалось сохранить подписку."}, status=400)


@owner_only
@require_POST
def push_test(request):
    try:
        endpoint = json.loads(request.body)["endpoint"]
    except (ValueError, TypeError, KeyError):
        return JsonResponse({"error": "Выберите устройство."}, status=400)
    if not isinstance(endpoint, str):
        return JsonResponse({"error": "Выберите устройство."}, status=400)
    sub = PushSubscription.objects.filter(
        user=request.user,
        enabled=True,
        endpoint_hash=hashlib.sha256(endpoint.encode()).hexdigest(),
    ).first()
    if not sub:
        return JsonResponse(
            {"error": "Сначала включите уведомления на этом устройстве."}, status=400
        )
    event = PushEvent.objects.create(
        key="test-" + uuid.uuid4().hex,
        category="test",
        occurred_at=timezone.now(),
        payload={
            "title": "Control · уведомления работают",
            "body": "Пуши для Who-Update и GhostNote включены.",
            "url": "/panel/?tab=push",
        },
    )
    PushDelivery.objects.create(event=event, subscription=sub)
    return JsonResponse({"ok": True})


@require_GET
def manifest(request):
    return JsonResponse(
        {
            "id": "/panel/",
            "name": "Control · Who-Update & GhostNote",
            "short_name": "Control",
            "lang": "ru",
            "start_url": "/panel/",
            "scope": "/panel/",
            "display": "standalone",
            "background_color": "#f5f6fa",
            "theme_color": "#f5f6fa",
            "icons": [
                {
                    "src": "/static/owner_panel/icon-192.png",
                    "sizes": "192x192",
                    "type": "image/png",
                    "purpose": "any maskable",
                },
                {
                    "src": "/static/owner_panel/icon-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "any maskable",
                },
            ],
        },
        content_type="application/manifest+json",
    )


@never_cache
@require_GET
def service_worker(request):
    source = Path(__file__).parent / "static" / "owner_panel" / "sw.js"
    response = HttpResponse(source.read_text(), content_type="application/javascript")
    response["Service-Worker-Allowed"] = "/panel/"
    return response
