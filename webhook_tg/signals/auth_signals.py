from django.conf import settings
from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver

from webhook_tg.telegram import tg_send_message
from env import OWNER_CHAT_ID


@receiver(user_logged_in)
def notify_admin_login(sender, request, user, **kwargs):
    # чтобы не слать на входы на сайте, а только /admin/
    if not request or not request.path.startswith("/admin/"):
        return

    ip = request.META.get("REMOTE_ADDR")
    ua = request.META.get("HTTP_USER_AGENT", "")
    text = (
        f"✅ Admin login OK\n"
        f"user: {user.get_username()} (id={user.pk})\n"
        f"ip: {ip}\n"
        f"ua: {ua[:200]}"
    )

    tg_send_message(OWNER_CHAT_ID, text)
