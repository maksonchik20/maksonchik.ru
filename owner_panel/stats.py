from collections import defaultdict
from decimal import Decimal
from django.db.models import Q
from django.utils import timezone
from webhook_tg.models import (
    UserTg,
    WhoUpdateOnboardingFunnel,
    WhoUpdatePaymentOrder,
    WhoUpdatePartnerCommission,
    WhoUpdatePartnerWithdrawal,
)
from .common import period, money, day, iso, series_for, paginate, payment_totals


def label(user):
    if not user:
        return ""
    return (
        ("@" + user.username.lstrip("@"))
        if user.username
        else (user.first_name or str(user.user_id))
    )


def dashboard(params):
    first, last, begin, end = period(params)
    now = timezone.now()
    series = series_for(first, last)
    all_orders = list(
        WhoUpdatePaymentOrder.objects.filter(is_test=False)
        .select_related("user__referred_by")
        .order_by("-created_at")
    )
    commissions = {
        c.order_id: c
        for c in WhoUpdatePartnerCommission.objects.select_related("partner")
    }
    rows = []
    for order in all_orders:
        at = (
            order.paid_at
            if order.status == "paid" and order.paid_at
            else order.created_at
        )
        commission = commissions.get(order.pk)
        referrer = commission.partner if commission else order.user.referred_by
        row = dict(
            id=str(order.public_id),
            amount=money(order.amount),
            status=order.status,
            at=iso(at),
            user=label(order.user),
            user_id=order.user_id,
            referrer=label(referrer),
            commission=money(
                commission.amount if commission and commission.status == "earned" else 0
            ),
            plan=order.get_plan_display(),
            source="ЮKassa",
            admin_url="https://maksonchik.ru/admin/webhook_tg/whoupdatepaymentorder/{}/change/".format(
                order.pk
            ),
        )
        if begin <= at < end:
            rows.append(row)
            if order.status == "paid":
                item = series[day(at)]
                item["revenue"] += float(order.amount)
                item["payments"] += 1
                if referrer:
                    item["referral_revenue"] += float(order.amount)
        # Historical orders predate explicit checkout tracking; only confirmed payment links count.
        checkout = order.checkout_at or (
            order.created_at
            if order.yookassa_payment_id and order.status != "failed"
            else None
        )
        if checkout and day(checkout) in series:
            series[day(checkout)]["checkouts"] += 1
    # Same user-per-day definitions as webhook_tg.user_metrics.collect_daily_user_metrics (/stat).
    started, connected, visitors = defaultdict(set), defaultdict(set), defaultdict(set)
    funnels = WhoUpdateOnboardingFunnel.objects.filter(
        Q(landing_viewed_at__gte=begin, landing_viewed_at__lt=end)
        | Q(telegram_started_at__gte=begin, telegram_started_at__lt=end)
        | Q(connected_at__gte=begin, connected_at__lt=end)
    ).exclude(landing_path="/bot/checkout/").values(
        "user_id",
        "landing_viewed_at",
        "telegram_started_at",
        "connected_at",
        "metrika_client_id",
    )
    for f in funnels:
        for field, target in [
            ("telegram_started_at", started),
            ("connected_at", connected),
        ]:
            key = day(f[field])
            if key in series and f["user_id"] and f[field] < now:
                target[key].add(f["user_id"])
        key = day(f["landing_viewed_at"])
        if key in series and f["landing_viewed_at"] < now:
            series[key]["views"] += 1
            if f["metrika_client_id"]:
                visitors[key].add(f["metrika_client_id"])
    for key in series:
        series[key].update(
            started=len(started[key]),
            connected=len(connected[key]),
            visitors=len(visitors[key]),
        )
    users = list(UserTg.objects.select_related("referred_by"))
    for u in users:
        key = day(u.business_disconnected_at)
        if key in series and u.business_disconnected_at < now:
            series[key]["disconnected"] += 1
        key = day(u.access_expires_at)
        if key in series and not u.access_unlimited and u.access_expires_at <= now:
            series[key]["expired"] += 1
    totals = payment_totals(rows)
    totals.update(
        users_total=len(users),
        connected_total=sum(u.business_is_connected for u in users),
        checkout_count=sum(s["checkouts"] for s in series.values()),
        active_tokens=sum(u.has_active_access(at=now) for u in users),
        upcoming_tokens=0,
    )
    return dict(
        project="who",
        generated_at=iso(now),
        summary=totals,
        series=list(series.values()),
        payments=paginate(sorted(rows, key=lambda r: r["at"], reverse=True), params),
        definitions="Подключения и /start считаются как в /stat: уникальные пользователи за день. Отключения — по последней сохранённой дате. Время — Москва.",
        checkout_note="Для старых заказов переходы восстановлены по созданным ссылкам на оплату.",
    )


def referrals():
    users = list(UserTg.objects.select_related("referred_by"))
    by_id = {u.pk: u for u in users}
    payments = defaultdict(lambda: [0, Decimal(0)])
    for order in WhoUpdatePaymentOrder.objects.filter(status="paid", is_test=False):
        payments[order.user_id][0] += 1
        payments[order.user_id][1] += order.amount
    earned, by_buyer, paid, reserved = (
        defaultdict(Decimal),
        defaultdict(Decimal),
        defaultdict(Decimal),
        defaultdict(Decimal),
    )
    for c in WhoUpdatePartnerCommission.objects.filter(status="earned"):
        earned[c.partner_id] += c.amount
        by_buyer[c.buyer_id] += c.amount
    requests = []
    for w in WhoUpdatePartnerWithdrawal.objects.select_related("partner").order_by(
        "-created_at"
    ):
        if w.status == "paid":
            paid[w.partner_id] += w.amount
        if w.status == "new":
            reserved[w.partner_id] += w.amount
            requests.append(
                dict(
                    id=w.pk,
                    user=label(w.partner),
                    amount=money(w.amount),
                    at=iso(w.created_at),
                    admin_url="https://maksonchik.ru/admin/webhook_tg/whoupdatepartnerwithdrawal/{}/change/".format(
                        w.pk
                    ),
                )
            )
    partners = []
    for pk in set(earned) | set(paid) | set(reserved):
        balance = earned[pk] - paid[pk]
        partners.append(
            dict(
                id=pk,
                user=label(by_id.get(pk)),
                earned=money(earned[pk]),
                paid=money(paid[pk]),
                due=money(balance),
                reserved=money(reserved[pk]),
                available=money(balance - reserved[pk]),
                admin_url="https://maksonchik.ru/admin/webhook_tg/usertg/{}/change/".format(
                    pk
                ),
            )
        )
    invitees = [
        dict(
            id=u.pk,
            user=label(u),
            referrer=label(u.referred_by),
            purchases=payments[u.pk][0],
            revenue=money(payments[u.pk][1]),
            commission=money(by_buyer[u.pk]),
            connected=u.business_is_connected,
            admin_url="https://maksonchik.ru/admin/webhook_tg/usertg/{}/change/".format(
                u.pk
            ),
        )
        for u in users
        if u.referred_by_id
    ]
    return dict(
        partners=sorted(partners, key=lambda x: Decimal(x["due"]), reverse=True),
        requests=requests,
        referrals=sorted(invitees, key=lambda x: Decimal(x["revenue"]), reverse=True),
        totals=dict(
            earned=money(sum(earned.values())),
            paid=money(sum(paid.values())),
            due=money(sum(earned.values()) - sum(paid.values())),
            reserved=money(sum(reserved.values())),
        ),
    )


def access_list(params):
    now = timezone.now()
    rows = []
    for u in UserTg.objects.order_by("access_expires_at", "pk"):
        state = "active" if u.has_active_access(at=now) else "expired"
        rows.append(
            dict(
                id=u.pk,
                user=label(u),
                status=state,
                starts_at=iso(u.trial_started_at),
                expires_at=iso(u.access_expires_at),
                token_type="unlimited" if u.access_unlimited else "subscription",
                amount="",
                admin_url="https://maksonchik.ru/admin/webhook_tg/usertg/{}/change/".format(
                    u.pk
                ),
            )
        )
    return paginate(rows, params)
