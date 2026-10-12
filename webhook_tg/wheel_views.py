from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.clickjacking import xframe_options_exempt
from django.utils import timezone

from .models import WhoUpdateWheelReward
from .subscriptions import PLAN_CONFIG, checkout_url, plan_config_for_user
from .wheel import PRIZES, authenticate, active_reward, spin, latest_reward, spin_availability, INSTANT_DAY_PRIZES


@never_cache
@xframe_options_exempt
@require_GET
def wheel_app(request):
    response = render(request, "webhook_tg/wheel.html")
    response["X-Robots-Tag"] = "noindex, nofollow"
    response["Referrer-Policy"] = "no-referrer"
    response["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org"
    )
    return response


def state(user):
    reward = latest_reward(user)
    active = active_reward(user)
    can_spin, next_spin = spin_availability(reward)
    return {
        "server_time": timezone.now().isoformat(),
        "can_spin": can_spin,
        "next_spin_at": next_spin.isoformat(),
        "prizes": [{"id": p["id"], "label": p["label"]} for p in PRIZES],
        "reward": None if not reward else {
            "prize": reward.prize,
            "index": next(i for i, p in enumerate(PRIZES) if p["id"] == reward.prize),
            "label": next(p["label"] for p in PRIZES if p["id"] == reward.prize),
            "expires_at": reward.expires_at.isoformat(),
            "status": "granted" if reward.granted_days else "used" if reward.used_at else "active" if active else "expired",
            "instant_days": INSTANT_DAY_PRIZES.get(reward.prize, 0),
            "granted_days": reward.granted_days,
            "access_expires_at_after": reward.access_expires_at_after.isoformat() if reward.access_expires_at_after else None,
            "access_unlimited": user.access_unlimited,
            "reserved_plan": reward.reserved_order.plan if reward.reserved_order_id and reward.reserved_order.status == "pending" else None,
        },
        "plans": [{
            "id": plan, "label": c["label"], "days": c["days"], "amount": str(c["amount"]),
            "original_label": base["label"], "original_amount": str(base["amount"]),
            "checkout_url": checkout_url(user, plan, config=c),
        } for plan, base in PLAN_CONFIG.items() for c in [plan_config_for_user(user, plan)]],
    }


@csrf_exempt
@never_cache
@require_POST
def wheel_api(request, action):
    header = request.headers.get("Authorization", "")
    user = authenticate(header[4:]) if header.startswith("tma ") else None
    if user is None:
        return JsonResponse({"error": "Колесо недоступно. Открой его из меню бота в Telegram."}, status=403)
    if action not in ("state", "spin"):
        return JsonResponse({"error": "Неизвестное действие"}, status=404)
    if action == "spin":
        spin(user)
    return JsonResponse(state(user))
