from datetime import date, datetime, time, timedelta
from decimal import Decimal
from django.utils import timezone


def period(params):
    today = timezone.localdate()
    start = date.fromisoformat(
        params.get("from") or (today - timedelta(days=29)).isoformat()
    )
    end = date.fromisoformat(params.get("to") or today.isoformat())
    if end < start or (end - start).days > 3650:
        raise ValueError(
            "Выберите период до 10 лет; начало должно быть раньше окончания."
        )
    tz = timezone.get_current_timezone()
    return (
        start,
        end,
        timezone.make_aware(datetime.combine(start, time.min), tz),
        timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min), tz),
    )


def money(value):
    return str(Decimal(value or 0).quantize(Decimal("0.01")))


def day(value):
    return timezone.localtime(value).date().isoformat() if value else None


def iso(value):
    return value.isoformat() if value else None


def series_for(start, end):
    result = {}
    while start <= end:
        result[start.isoformat()] = dict(
            date=start.isoformat(),
            revenue=0,
            payments=0,
            checkouts=0,
            referral_revenue=0,
            started=0,
            connected=0,
            disconnected=0,
            expired=0,
            visitors=0,
            views=0,
            new_users=0,
        )
        start += timedelta(days=1)
    return result


def paginate(rows, params):
    page = max(1, int(params.get("page") or 1))
    query = (params.get("q") or "").strip().casefold()
    status = params.get("status") or "all"
    filtered = [
        r
        for r in rows
        if (status == "all" or r.get("status") == status)
        and (
            not query
            or query in str(r.get("user", "")).casefold()
            or query in str(r.get("referrer", "")).casefold()
            or query in str(r.get("id", "")).casefold()
        )
    ]
    return dict(
        rows=filtered[(page - 1) * 50 : page * 50],
        total=len(filtered),
        page=page,
        has_more=len(filtered) > page * 50,
    )


def payment_totals(rows):
    paid = [r for r in rows if r["status"] == "paid"]
    revenue = sum((Decimal(r["amount"]) for r in paid), Decimal(0))
    return dict(
        revenue=money(revenue),
        payment_count=len(paid),
        average=money(revenue / len(paid) if paid else 0),
        referral_revenue=money(
            sum((Decimal(r["amount"]) for r in paid if r.get("referrer")), Decimal(0))
        ),
        commission=money(
            sum((Decimal(r.get("commission") or 0) for r in paid), Decimal(0))
        ),
    )
