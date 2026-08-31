from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.utils import timezone

from .models import UserTg, WhoUpdateOnboardingFunnel


@dataclass(frozen=True)
class DailyUserMetrics:
    day: date
    unique_landing_visitors: int
    landing_views: int
    started_users: int
    connected_users: int
    disconnected_users: int
    expired_users: int


def _day_interval(day: date, *, at: datetime) -> tuple[datetime, datetime]:
    current_timezone = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(day, time.min), current_timezone)
    end = start + timedelta(days=1)
    local_at = timezone.localtime(at, current_timezone)
    if day == local_at.date():
        end = min(end, local_at)
    elif day > local_at.date():
        end = start
    return start, end


def collect_daily_user_metrics(*, day: date | None = None, at: datetime | None = None) -> DailyUserMetrics:
    at = at or timezone.now()
    day = day or timezone.localdate(at)
    start, end = _day_interval(day, at=at)

    landing = WhoUpdateOnboardingFunnel.objects.filter(
        landing_viewed_at__gte=start,
        landing_viewed_at__lt=end,
    )
    unique_landing_visitors = (
        landing.exclude(metrika_client_id="")
        .values("metrika_client_id")
        .distinct()
        .count()
    )
    started_users = (
        WhoUpdateOnboardingFunnel.objects.filter(
            telegram_started_at__gte=start,
            telegram_started_at__lt=end,
            user__isnull=False,
        )
        .values("user_id")
        .distinct()
        .count()
    )
    connected_users = (
        WhoUpdateOnboardingFunnel.objects.filter(
            connected_at__gte=start,
            connected_at__lt=end,
            user__isnull=False,
        )
        .values("user_id")
        .distinct()
        .count()
    )
    disconnected_users = UserTg.objects.filter(
        business_disconnected_at__gte=start,
        business_disconnected_at__lt=end,
    ).count()
    expired_users = UserTg.objects.filter(
        access_unlimited=False,
        access_expires_at__gte=start,
        access_expires_at__lt=end,
        access_expires_at__lte=at,
    ).count()

    return DailyUserMetrics(
        day=day,
        unique_landing_visitors=unique_landing_visitors,
        landing_views=landing.count(),
        started_users=started_users,
        connected_users=connected_users,
        disconnected_users=disconnected_users,
        expired_users=expired_users,
    )


def daily_user_metrics_text(metrics: DailyUserMetrics) -> str:
    return (
        "📈 <b>Пользовательские метрики</b>\n"
        f"За <b>{metrics.day:%d.%m.%Y}</b>\n\n"
        "<b>Лендинг WhoUpdate</b>\n"
        f"👥 Уникальные посетители: <b>{metrics.unique_landing_visitors}</b>\n"
        f"👀 Открытия лендинга: <b>{metrics.landing_views}</b>\n\n"
        "<b>Пользователи бота</b>\n"
        f"▶️ Написали /start: <b>{metrics.started_users}</b>\n"
        f"✅ Подключились: <b>{metrics.connected_users}</b>\n"
        f"❌ Отключились: <b>{metrics.disconnected_users}</b>\n"
        f"⌛ Доступ закончился: <b>{metrics.expired_users}</b>"
    )
