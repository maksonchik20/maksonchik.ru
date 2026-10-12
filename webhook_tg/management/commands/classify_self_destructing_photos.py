from django.core.management.base import BaseCommand

from webhook_tg.models import FileType, Message
from webhook_tg.telegram_file_ids import is_self_destructing_photo


class Command(BaseCommand):
    help = "Классифицирует старые исчезающие фото, сохраняя исходные file_id."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        after_id = 0
        found = updated = 0
        while True:
            rows = list(
                Message.objects.filter(file_type=FileType.PHOTO, id__gt=after_id)
                .order_by("id").values_list("id", "file_id")[:500]
            )
            if not rows:
                break
            after_id = rows[-1][0]
            matched = [(pk, file_id) for pk, file_id in rows if is_self_destructing_photo(file_id)]
            found += len(matched)
            if matched and not options["dry_run"]:
                updated += Message.objects.filter(
                    id__in=[pk for pk, _ in matched],
                    file_id__in=[file_id for _, file_id in matched],
                    file_type=FileType.PHOTO,
                ).update(file_type=FileType.SELF_DESTRUCT_PHOTO)
        self.stdout.write(f"Найдено: {found}; обновлено: {updated}")
