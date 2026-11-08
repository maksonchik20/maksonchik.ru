"""Conservative delivery-side measurement; message contents are never copied."""
import logging

from django.db import transaction
from django.utils import timezone

from .models import TelegramOutbox, UserTg, WhoUpdateFirstObservedValue


logger = logging.getLogger(__name__)


def _record_value(chat_id, *, event_type, sent_at, outbox=None):
    user_pks = list(UserTg.objects.filter(chat_id=chat_id).values_list("pk", flat=True)[:2])
    if len(user_pks) != 1:
        return
    first, created = WhoUpdateFirstObservedValue.objects.get_or_create(
        user_id=user_pks[0],
        defaults={"event_type": event_type, "observed_at": sent_at, "outbox": outbox},
    )
    if not created:
        # Concurrent workers can finish in a different order from their DB
        # commits. Preserve the earliest confirmed completion timestamp.
        WhoUpdateFirstObservedValue.objects.filter(pk=first.pk, observed_at__gt=sent_at).update(
            event_type=event_type, observed_at=sent_at, outbox=outbox,
        )


def record_first_observed_value(item, *, sent_at):
    # This prefix is assigned by the existing edit-notification sender. Do not
    # guess intent from message text, commands or a successful generic send.
    contextFieldToValue = item.delivery_context or {}
    event_type = "edit" if item.method == TelegramOutbox.Method.SEND_MESSAGE and (item.idempotency_key or "").startswith("edit:") else None
    if contextFieldToValue.get("event_type") == "delete_text" and contextFieldToValue.get("has_original_text"):
        event_type = "delete_text"
    if event_type is None or sent_at is None:
        return
    confirmed_at = (
        TelegramOutbox.objects.filter(pk=item.pk, status=TelegramOutbox.Status.SENT)
        .values_list("sent_at", flat=True).first()
    )
    if confirmed_at is None:
        return
    _record_value(item.chat_id, event_type=event_type, sent_at=confirmed_at, outbox=item)


def observe_deleted_text_delivery(chat_id):
    """Call only after Telegram accepted a copy containing known original text."""
    try:
        with transaction.atomic():
            _record_value(chat_id, event_type="delete_text", sent_at=timezone.now())
    except Exception:
        # Measurement must never turn a completed direct send into a failed
        # incoming update that will resend the same deletion notification.
        logger.exception("Could not record deleted-text delivery")
