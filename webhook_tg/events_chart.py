import io
import re
from datetime import timedelta

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from django.utils import timezone

from .models import BotOutgoingMessage

_PERIOD_RE = re.compile(r"^(\d+)(h|d|m)$", re.IGNORECASE)

PERIOD_HELP = (
    "Использование: /events <период>\n\n"
    "Примеры: 1h, 3h, 12h, 1d, 3d, 7d\n"
    "h — часы, d — дни, m — минуты"
)


def parse_events_period(raw: str) -> timedelta | None:
    token = (raw or "").strip().lower()
    if not token:
        return timedelta(hours=1)
    match = _PERIOD_RE.fullmatch(token)
    if not match:
        return None
    amount = int(match.group(1))
    if amount <= 0:
        return None
    unit = match.group(2).lower()
    if unit == "h":
        return timedelta(hours=amount)
    if unit == "d":
        return timedelta(days=amount)
    return timedelta(minutes=amount)


def _bucket_size(period: timedelta) -> timedelta:
    minutes = period.total_seconds() / 60
    if minutes <= 180:
        return timedelta(minutes=5)
    if minutes <= 720:
        return timedelta(minutes=15)
    if minutes <= 1440:
        return timedelta(hours=1)
    if minutes <= 4320:
        return timedelta(hours=3)
    return timedelta(hours=6)


def _period_label(period: timedelta) -> str:
    total_minutes = int(period.total_seconds() // 60)
    if total_minutes % (24 * 60) == 0 and total_minutes >= 24 * 60:
        return f"{total_minutes // (24 * 60)}d"
    if total_minutes % 60 == 0 and total_minutes >= 60:
        return f"{total_minutes // 60}h"
    return f"{total_minutes}m"


def build_outgoing_events_chart(period: timedelta) -> tuple[bytes, str]:
    now = timezone.localtime()
    start = now - period
    bucket = _bucket_size(period)

    qs = BotOutgoingMessage.objects.filter(sent_at__gte=start, sent_at__lte=now).order_by("sent_at")
    total = qs.count()

    bucket_start = start
    labels = []
    counts = []
    while bucket_start < now:
        bucket_end = min(bucket_start + bucket, now)
        count = qs.filter(sent_at__gte=bucket_start, sent_at__lt=bucket_end).count()
        labels.append(bucket_start)
        counts.append(count)
        bucket_start = bucket_end

    fig, ax = plt.subplots(figsize=(10, 4.5), dpi=120)
    fig.patch.set_facecolor("#1e1e2e")
    ax.set_facecolor("#1e1e2e")

    ax.bar(labels, counts, width=bucket.total_seconds() / 86400 * 0.85, color="#89b4fa", edgecolor="#45475a")
    ax.set_title(
        f"Исходящие сообщения бота · {_period_label(period)} · всего {total}",
        color="#cdd6f4",
        fontsize=12,
        pad=12,
    )
    ax.set_ylabel("Сообщений", color="#a6adc8")
    ax.tick_params(colors="#a6adc8")
    ax.grid(axis="y", color="#45475a", alpha=0.5, linestyle="--", linewidth=0.6)
    for spine in ax.spines.values():
        spine.set_color("#45475a")

    if period <= timedelta(hours=6):
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    elif period <= timedelta(days=2):
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m %H:%M"))
    else:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    fig.autofmt_xdate(rotation=30, ha="right")

    plt.tight_layout()
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    buffer.seek(0)

    caption = (
        f"📊 Исходящие сообщения бота за {_period_label(period)}\n"
        f"Всего: {total} · интервал: {int(bucket.total_seconds() // 60)} мин"
    )
    return buffer.read(), caption
