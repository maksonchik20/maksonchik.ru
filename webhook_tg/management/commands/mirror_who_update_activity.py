from django.core.management.base import BaseCommand, CommandError

from webhook_tg.activity_mirror import initialize_activity_mirror, publish_activity_mirror


class Command(BaseCommand):
    help = "Initialize or retry the bounded WhoUpdate activity mirror."

    def add_arguments(self, parser):
        parser.add_argument("--initialize", action="store_true")
        parser.add_argument("--state-path")

    def handle(self, *args, **optionToValue):
        state_path = optionToValue.get("state_path")
        if optionToValue["initialize"]:
            fieldToState = initialize_activity_mirror(state_path=state_path)
            self.stdout.write("Activity cursor initialized: %s" % fieldToState["last_message_pk"])
        elif not publish_activity_mirror(state_path=state_path):
            raise CommandError("Activity batch not delivered; cursor preserved")
