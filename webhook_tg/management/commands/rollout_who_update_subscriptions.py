from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from webhook_tg.models import UserTg
from webhook_tg.subscriptions import TRIAL_DAYS


class Command(BaseCommand):
    help = "Включает ограниченный доступ WhoUpdate для всех существующих пользователей."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Применить изменения. Без флага команда показывает только расчёт.",
        )
        parser.add_argument(
            "--notify-expired",
            action="store_true",
            help="Не отмечать уже истёкший доступ уведомлённым (разрешить фоновой рассылке).",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        cutoff = now - timedelta(days=TRIAL_DAYS)
        users = list(UserTg.objects.order_by("id"))
        no_start = sum(user.last_start_at is None for user in users)
        old_start = sum(
            user.last_start_at is not None and user.last_start_at <= cutoff
            for user in users
        )
        recent_start = len(users) - no_start - old_start

        self.stdout.write(
            f"users={len(users)} no_start={no_start} old_start={old_start} "
            f"recent_start={recent_start} reference_time={now.isoformat()}"
        )
        if not options["apply"]:
            self.stdout.write("Dry run: add --apply to update users")
            return

        with transaction.atomic():
            locked_users = list(UserTg.objects.select_for_update().order_by("id"))
            for user in locked_users:
                start_at = user.last_start_at
                expired_now = start_at is None or start_at <= cutoff
                user.access_unlimited = False
                user.trial_started_at = start_at or now
                user.access_expires_at = (
                    now if expired_now else start_at + timedelta(days=TRIAL_DAYS)
                )
                user.access_expired_notified_at = (
                    None if not expired_now or options["notify_expired"] else now
                )

            UserTg.objects.bulk_update(
                locked_users,
                [
                    "access_unlimited",
                    "trial_started_at",
                    "access_expires_at",
                    "access_expired_notified_at",
                ],
                batch_size=200,
            )

        self.stdout.write(self.style.SUCCESS(f"Updated {len(users)} users"))
