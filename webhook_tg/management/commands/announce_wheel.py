from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import F, Count
from django.utils import timezone

from webhook_tg.models import UserTg, TelegramOutbox
from webhook_tg.wheel_announcement import announcement_payload

CAMPAIGN = "wheel-public-launch-20260908:"


class Command(BaseCommand):
    help = "Preview or queue the approved wheel launch once per registered private chat."

    def add_arguments(self, parser):
        parser.add_argument("--send", action="store_true", help="Queue the broadcast (default is read-only).")

    def handle(self, *args, **options):
        chats = list(UserTg.objects.filter(user_id__gt=0, chat_id=F("user_id")).order_by("chat_id").values_list("chat_id", flat=True).distinct())
        created = 0
        if options["send"]:
            now = timezone.now()
            # A stable per-chat key makes retrying after interruption safe.
            with transaction.atomic():
                for index, chat_id in enumerate(chats):
                    _, added = TelegramOutbox.objects.get_or_create(
                        idempotency_key=f"{CAMPAIGN}{chat_id}",
                        defaults={"chat_id":chat_id, "method":TelegramOutbox.Method.SEND_MESSAGE,
                                  "payload":announcement_payload(), "next_attempt_at":now+timedelta(seconds=index/5)},
                    )
                    created += int(added)
        counts = dict(TelegramOutbox.objects.filter(idempotency_key__startswith=CAMPAIGN).values("status").annotate(total=Count("pk")).values_list("status","total"))
        self.stdout.write(f"Recipients={len(chats)} newly_queued={created} statuses={counts}")
