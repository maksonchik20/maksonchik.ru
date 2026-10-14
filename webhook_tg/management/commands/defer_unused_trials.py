"""Move unused legacy trials to first connection, without changing earned access."""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

from webhook_tg.models import (
    TelegramIncomingUpdate,
    UserTg,
    WhoUpdateMetrikaConversion,
    WhoUpdateOnboardingFunnel,
    WhoUpdatePaymentOrder,
    WhoUpdateWheelReward,
)


class Command(BaseCommand):
    help = "Откладывает неиспользованные старые trial до первого подключения. По умолчанию dry run."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        changed = preserved = skipped = 0
        # Run --apply while priority/background workers are paused. The lock also
        # serializes updates with payment fulfilment and wheel/referral rewards.
        with transaction.atomic():
            candidates = UserTg.objects.filter(
                trial_started_at__isnull=False,
                access_unlimited=False,
                business_is_connected=False,
                business_connected_at__isnull=True,
                business_disconnected_at__isnull=True,
                referral_rewarded_at__isnull=True,
            ).filter(Q(business_connection_id__isnull=True) | Q(business_connection_id=""))
            users = list(candidates.select_for_update().order_by("pk"))
            connected_users = set(WhoUpdateOnboardingFunnel.objects.filter(
                connected_at__isnull=False, user_id__in=[user.pk for user in users],
            ).values_list("user_id", flat=True))
            connected_users.update(WhoUpdateMetrikaConversion.objects.filter(
                event_type="connected", funnel__user_id__in=[user.pk for user in users],
            ).values_list("funnel__user_id", flat=True))
            historical_telegram_ids = set(TelegramIncomingUpdate.objects.filter(
                queue="priority", payload__has_key="business_connection",
            ).values_list("payload__business_connection__user__id", flat=True))
            paid_users = set(WhoUpdatePaymentOrder.objects.filter(
                Q(status=WhoUpdatePaymentOrder.Status.PAID) | Q(paid_at__isnull=False),
                user_id__in=[user.pk for user in users],
            ).values_list("user_id", flat=True))
            bonus_users = set(WhoUpdateWheelReward.objects.filter(
                user_id__in=[user.pk for user in users], granted_days__gt=0,
            ).values_list("user_id", flat=True))

            for user in users:
                if user.pk in connected_users or user.user_id in historical_telegram_ids:
                    skipped += 1
                    continue
                keep_deadline = (
                    user.pk in paid_users or user.pk in bonus_users or user.referral_bonus_days > 0
                )
                if not keep_deadline and user.access_expires_at is not None:
                    duration = user.access_expires_at - user.trial_started_at
                    if duration not in (timedelta(0), timedelta(days=7), timedelta(days=14)):
                        # An unexplained/manual extension needs separate review.
                        skipped += 1
                        continue
                changed += 1
                preserved += int(keep_deadline)
                if options["apply"]:
                    user.trial_started_at = None
                    if not keep_deadline:
                        user.access_expires_at = None
                    user.access_expired_notified_at = None
                    user.save(update_fields=[
                        "trial_started_at", "access_expires_at", "access_expired_notified_at",
                    ])

        mode = "Applied" if options["apply"] else "Dry run"
        self.stdout.write(
            f"{mode}: deferred={changed} preserved_paid_or_bonus_deadlines={preserved} "
            f"skipped_history_or_custom_access={skipped}"
        )
