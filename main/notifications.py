from html import escape

from django.db import transaction
from django.utils import timezone

from webhook_tg.config import OWNER_CHAT_ID
from webhook_tg.models import TelegramOutbox
from webhook_tg.outbox import enqueue_outbox

from .models import Lead


@transaction.atomic
def enqueue_lead_notification(lead: Lead) -> TelegramOutbox | None:
    """Persist one notification per lead; the outbox worker performs delivery."""
    lead = Lead.objects.select_for_update().get(pk=lead.pk)
    if lead.notification_sent or lead.notification_outbox_id:
        return lead.notification_outbox

    created_at = timezone.localtime(lead.created_at).strftime("%d.%m.%Y %H:%M %Z")
    text = (
        "<b>Новая заявка с maksonchik.ru</b>\n\n"
        f"<b>Заявка:</b> №{lead.pk}, {created_at}\n"
        f"<b>Имя:</b> {escape(lead.name or 'Не указано')}\n"
        f"<b>Контакт:</b> {escape(lead.contact)}\n"
        f"<b>Задача:</b> {escape(lead.message or 'Не указана')}\n"
        f"<b>Страница:</b> {escape(lead.page_url or 'Не определена')}\n"
        f"<b>UTM campaign:</b> {escape(lead.utm_campaign or '—')}\n\n"
        f'<a href="https://maksonchik.ru/admin/main/lead/{lead.pk}/change/">Открыть заявку</a>'
    )
    item = enqueue_outbox(
        chat_id=OWNER_CHAT_ID,
        method=TelegramOutbox.Method.SEND_MESSAGE,
        payload={"text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
        idempotency_key=f"lead:{lead.pk}",
    )
    if item is None:
        raise RuntimeError(f"Failed to persist notification for lead {lead.pk}")
    lead.notification_outbox = item
    lead.notification_sent = item.status == TelegramOutbox.Status.SENT
    lead.notification_error = item.last_error
    lead.save(update_fields=("notification_outbox", "notification_sent", "notification_error"))
    return item
