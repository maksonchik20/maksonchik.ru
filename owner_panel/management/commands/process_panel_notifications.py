import fcntl
from django.core.management.base import BaseCommand
from owner_panel.push import poll_sources, deliver


class Command(BaseCommand):
    help = "Poll owner events and deliver pending Web Push notifications."

    def handle(self, *args, **options):
        with open("/tmp/maksonchik-owner-panel-worker.lock", "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            poll_sources()
            count = deliver()
            self.stdout.write("Owner panel: {} push notifications sent.".format(count))
