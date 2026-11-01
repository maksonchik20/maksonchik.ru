"""Populate additive attribution metadata without rewriting or exporting goals."""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from webhook_tg.acquisition import acquisition_changes, acquisition_history
from webhook_tg.models import UserTg, WhoUpdateUserAcquisition


class Command(BaseCommand):
    help = "Восстанавливает источники/первое известное подключение. По умолчанию dry-run; конверсии не меняет."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--user-pk", type=int)
        parser.add_argument("--limit", type=int)

    def handle(self, *args, **options):
        if options["limit"] is not None and options["limit"] < 1:
            raise CommandError("--limit must be positive")
        users = UserTg.objects.order_by("pk")
        if options["user_pk"] is not None:
            users = users.filter(pk=options["user_pk"])
        if options["limit"] is not None:
            users = users[:options["limit"]]
        counterToValue = {"examined": 0, "created": 0, "updated": 0, "unchanged": 0, "legacy_unknown_date": 0}
        for user_pk in users.values_list("pk", flat=True).iterator(chunk_size=200):
            with transaction.atomic():
                user_query = UserTg.objects
                if options["apply"]:
                    user_query = user_query.select_for_update()
                user = user_query.get(pk=user_pk)
                acquisition = WhoUpdateUserAcquisition.objects.filter(user=user).first()
                created = acquisition is None
                acquisition = acquisition or WhoUpdateUserAcquisition(user=user)
                fieldToValue = acquisition_changes(acquisition, acquisition_history(user))
                for field, value in fieldToValue.items():
                    setattr(acquisition, field, value)
                counterToValue["examined"] += 1
                counterToValue["created" if created else "updated" if fieldToValue else "unchanged"] += 1
                counterToValue["legacy_unknown_date"] += int(
                    acquisition.has_connection_history and acquisition.first_connected_at is None,
                )
                if options["apply"] and (created or fieldToValue):
                    if created:
                        acquisition.save()
                    else:
                        acquisition.save(update_fields=list(fieldToValue))
        mode = "Applied" if options["apply"] else "Dry run (no writes)"
        self.stdout.write(f"{mode}: " + " ".join(f"{key}={value}" for key, value in counterToValue.items()))
