from __future__ import annotations

import fcntl
from datetime import timedelta
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db.models import BigIntegerField, Exists, OuterRef
from django.db.models.functions import Cast
from django.utils import timezone

from webhook_tg.background_tasks import (
    ACCESS_EXPIRY_REMINDER_HOURS,
    ACCESS_EXPIRY_REMINDER_TASK,
    schedule_access_expiry_reminders,
)
from webhook_tg.config import (
    CONNECTION_REMINDER_REPLY_MARKUP,
    CONNECTION_REMINDER_TEXT,
    START_PHOTO_ID,
)
from webhook_tg.models import BackgroundTask, UserTg
from webhook_tg.subscriptions import notify_access_expired
from webhook_tg.telegram import dispatch_telegram_request
from webhook_tg.metrics import record_heartbeat


LOCK_PATH = Path("/tmp/who-update-connection-reminders.lock")
FAILED_RETRY_DELAY = timedelta(hours=6)
PERMANENT_ERRORS = (
    "bot was blocked by the user",
    "chat not found",
    "user is deactivated",
    "bot can't initiate conversation",
)


class Command(BaseCommand):
    help = "Отправляет запланированные напоминания о подключении WhoUpdate."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=50)

    def handle(self, *args, **options):
        limit = max(1, options["limit"])
        with LOCK_PATH.open("w") as lock_file:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.stdout.write("Another reminder job is already running")
                return

            now = timezone.now()
            expired_users = list(
                UserTg.objects.filter(
                    access_unlimited=False,
                    access_expires_at__isnull=False,
                    access_expires_at__lte=now,
                    access_expired_notified_at__isnull=True,
                )
                .order_by("access_expires_at", "id")[:limit]
            )
            expired_sent = sum(notify_access_expired(user, at=now) for user in expired_users)

            # Подхватываем пользователей, которым первое уведомление уже было
            # отправлено до появления цепочки. Планируются только будущие сроки.
            scheduled_reminders = BackgroundTask.objects.alias(
                reminder_user_pk=Cast("payload__user_pk", BigIntegerField()),
            ).filter(
                task_type=ACCESS_EXPIRY_REMINDER_TASK,
                reminder_user_pk=OuterRef("pk"),
                created_at__gte=OuterRef("access_expires_at"),
            )
            recently_expired_users = (
                UserTg.objects.filter(
                    access_unlimited=False,
                    access_expires_at__gt=now - timedelta(hours=max(ACCESS_EXPIRY_REMINDER_HOURS)),
                    access_expires_at__lte=now,
                    access_expired_notified_at__isnull=False,
                )
                .filter(~Exists(scheduled_reminders))
                .order_by("access_expires_at", "id")[:limit]
            )
            for user in recently_expired_users:
                schedule_access_expiry_reminders(user, at=now)

            users = list(
                UserTg.objects.filter(
                    business_is_connected=False,
                    connection_reminder_at__isnull=False,
                    connection_reminder_at__lte=now,
                )
                .order_by("connection_reminder_at", "id")[:limit]
            )

            sent = 0
            failed = 0
            permanent = 0
            for user in users:
                # Повторно читаем состояние прямо перед внешним вызовом: пользователь
                # мог подключить бота после формирования первоначальной выборки.
                user.refresh_from_db()
                if user.business_is_connected or not user.connection_reminder_at:
                    continue
                if user.connection_reminder_at > timezone.now():
                    continue

                ok, error = dispatch_telegram_request(
                    "sendPhoto",
                    user.chat_id,
                    {
                        "photo": START_PHOTO_ID,
                        "caption": CONNECTION_REMINDER_TEXT,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                        "reply_markup": CONNECTION_REMINDER_REPLY_MARKUP,
                    },
                )
                if ok:
                    user.connection_reminder_sent_at = timezone.now()
                    user.connection_reminder_at = None
                    user.save(
                        update_fields=[
                            "connection_reminder_at",
                            "connection_reminder_sent_at",
                        ]
                    )
                    sent += 1
                elif any(marker in error.lower() for marker in PERMANENT_ERRORS):
                    # Пользователь заблокировал бота или чат больше недоступен:
                    # повторять такой запрос каждые несколько часов бессмысленно.
                    user.connection_reminder_at = None
                    user.save(update_fields=["connection_reminder_at"])
                    permanent += 1
                else:
                    user.connection_reminder_at = timezone.now() + FAILED_RETRY_DELAY
                    user.save(update_fields=["connection_reminder_at"])
                    failed += 1

            self.stdout.write(
                f"due={len(users)} sent={sent} permanent={permanent} failed={failed} "
                f"expired_due={len(expired_users)} expired_sent={expired_sent}"
            )
            record_heartbeat("connection_reminders", min_interval_seconds=0)
