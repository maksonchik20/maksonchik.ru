import shutil
import psutil
from django.conf import settings
from django.core.management.base import BaseCommand
from webhook_tg.telegram import tg_send_message
from env import OWNER_CHAT_ID

class Command(BaseCommand):
    def handle(self, *args, **kwargs):
        host = psutil.users()

        total, used, free = shutil.disk_usage("/")
        disk_used_pct = int(used * 100 / total)

        vm = psutil.virtual_memory()
        ram_used_pct = int(vm.percent)

        msgs = [f"host: {host}"]

        if disk_used_pct >= 90:
            msgs.append(f"⚠️ Disk /: {disk_used_pct}% used, free={free/1024/1024/1024:.1f}GB")
        else:
            msgs.append(f"✅  Disk /: {disk_used_pct}% used, free={free/1024/1024/1024:.1f}GB")
        if ram_used_pct >= 90:
            msgs.append(f"⚠️ RAM: {ram_used_pct}% used")
        else:
            msgs.append(f"✅ RAM: {ram_used_pct}% used") 

        if msgs:
            tg_send_message(OWNER_CHAT_ID, "\n".join(msgs))
