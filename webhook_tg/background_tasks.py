from __future__ import annotations

import logging
import os
import socket
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .config import CONNECTION_REMINDER_TEXT, START_PHOTO_ID
from .models import BackgroundTask, UserTg
from .telegram import dispatch_telegram_request

logger = logging.getLogger(__name__)

CONNECTION_REMINDER_TASK = "send_connection_reminder"
STALE_AFTER = timedelta(minutes=5)
MAX_BACKOFF_SECONDS = 3600
PERMANENT_SEND_ERRORS = (
    "bot was blocked by the user",
    "chat not found",
    "user is deactivated",
    "bot can't initiate conversation",
)


class PermanentTaskError(Exception):
    pass


def worker_name() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def enqueue_background_task(
    *,
    task_type: str,
    payload: dict,
    run_at,
    idempotency_key: str,
    priority: int = 100,
    max_attempts: int = 10,
) -> tuple[BackgroundTask, bool]:
    return BackgroundTask.objects.get_or_create(
        idempotency_key=idempotency_key,
        defaults={
            "task_type": task_type,
            "payload": payload,
            "run_at": run_at,
            "priority": priority,
            "max_attempts": max_attempts,
        },
    )


def cancel_connection_reminders(bot_user: UserTg) -> int:
    return BackgroundTask.objects.filter(
        task_type=CONNECTION_REMINDER_TASK,
        status=BackgroundTask.Status.PENDING,
        payload__user_pk=bot_user.pk,
    ).update(
        status=BackgroundTask.Status.CANCELLED,
        completed_at=timezone.now(),
        last_error="",
    )


def schedule_connection_reminders(
    bot_user: UserTg,
    *,
    started_at,
    start_update_id: int,
    onboarding_funnel_id: int | None = None,
) -> list[BackgroundTask]:
    """Создаёт ровно две задачи для последнего /start пользователя."""
    with transaction.atomic():
        if bot_user.business_is_connected:
            cancel_connection_reminders(bot_user)
            return []

        task_specs = (
            ("30m", timedelta(minutes=30), 1),
            ("24h", timedelta(days=1), 2),
        )
        current_keys = [
            f"connection-reminder:{bot_user.user_id}:start:{start_update_id}:{suffix}"
            for suffix, _, _ in task_specs
        ]
        BackgroundTask.objects.filter(
            task_type=CONNECTION_REMINDER_TASK,
            status=BackgroundTask.Status.PENDING,
            payload__user_pk=bot_user.pk,
        ).exclude(idempotency_key__in=current_keys).update(
            status=BackgroundTask.Status.CANCELLED,
            completed_at=timezone.now(),
            last_error="",
        )

        common_payload = {
            "user_pk": bot_user.pk,
            "started_at": started_at.isoformat(),
            "start_update_id": start_update_id,
            "onboarding_funnel_id": onboarding_funnel_id,
        }
        tasks = []
        for suffix, delay, reminder_number in task_specs:
            task, _ = enqueue_background_task(
                task_type=CONNECTION_REMINDER_TASK,
                payload={**common_payload, "reminder_number": reminder_number},
                run_at=started_at + delay,
                idempotency_key=(
                    f"connection-reminder:{bot_user.user_id}:"
                    f"start:{start_update_id}:{suffix}"
                ),
                priority=50,
            )
            tasks.append(task)
        return tasks


def recover_stale_tasks() -> int:
    stale_before = timezone.now() - STALE_AFTER
    return BackgroundTask.objects.filter(
        status=BackgroundTask.Status.PROCESSING,
        locked_at__lt=stale_before,
    ).update(
        status=BackgroundTask.Status.PENDING,
        locked_at=None,
        locked_by="",
        run_at=timezone.now(),
        last_error="Recovered after stale processing lease",
    )


def claim_next_task(*, claimed_by: str) -> BackgroundTask | None:
    now = timezone.now()
    candidate = (
        BackgroundTask.objects.filter(
            status=BackgroundTask.Status.PENDING,
            run_at__lte=now,
        )
        .order_by("priority", "run_at", "created_at")
        .first()
    )
    if candidate is None:
        return None

    claimed = BackgroundTask.objects.filter(
        pk=candidate.pk,
        status=BackgroundTask.Status.PENDING,
    ).update(
        status=BackgroundTask.Status.PROCESSING,
        locked_at=now,
        locked_by=claimed_by,
    )
    if not claimed:
        return None
    candidate.status = BackgroundTask.Status.PROCESSING
    candidate.locked_at = now
    candidate.locked_by = claimed_by
    return candidate


def _same_start(user: UserTg, started_at_raw: str) -> bool:
    expected = parse_datetime(started_at_raw or "")
    if expected is None or user.last_start_at is None:
        return False
    return abs((user.last_start_at - expected).total_seconds()) < 0.001


def _send_connection_reminder(task: BackgroundTask) -> None:
    user_pk = task.payload.get("user_pk")
    user = UserTg.objects.filter(pk=user_pk).first()
    if user is None:
        raise PermanentTaskError(f"UserTg {user_pk} no longer exists")

    # Проверка выполняется непосредственно перед запросом к Telegram. Помимо
    # подключения сверяем /start: устаревшая задача от предыдущего запуска
    # не должна отправлять напоминание.
    if user.business_is_connected or not _same_start(user, task.payload.get("started_at", "")):
        return

    ok, error = dispatch_telegram_request(
        "sendPhoto",
        user.chat_id,
        {
            "photo": START_PHOTO_ID,
            "caption": CONNECTION_REMINDER_TEXT,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    )
    if ok:
        sent_at = timezone.now()
        UserTg.objects.filter(pk=user.pk, business_is_connected=False).update(
            connection_reminder_sent_at=sent_at
        )
        from .onboarding_analytics import record_reminder_sent

        record_reminder_sent(
            user,
            int(task.payload.get("reminder_number") or 0),
            sent_at=sent_at,
            funnel_pk=task.payload.get("onboarding_funnel_id"),
        )
        return

    error = error or "Unknown Telegram error"
    if any(marker in error.lower() for marker in PERMANENT_SEND_ERRORS):
        raise PermanentTaskError(error)
    raise RuntimeError(error)


TASK_HANDLERS = {
    CONNECTION_REMINDER_TASK: _send_connection_reminder,
}


def process_claimed_task(task: BackgroundTask) -> bool:
    handler = TASK_HANDLERS.get(task.task_type)
    if handler is None:
        _mark_failed(task, f"Unknown task type: {task.task_type}")
        return False

    try:
        handler(task)
    except PermanentTaskError as exc:
        BackgroundTask.objects.filter(pk=task.pk).update(
            status=BackgroundTask.Status.CANCELLED,
            attempts=task.attempts + 1,
            locked_at=None,
            locked_by="",
            completed_at=timezone.now(),
            last_error=str(exc)[:2000],
        )
        return True
    except Exception as exc:
        attempts = task.attempts + 1
        if attempts >= task.max_attempts:
            _mark_failed(task, str(exc), attempts=attempts)
        else:
            delay = min(30 * (2 ** max(attempts - 1, 0)), MAX_BACKOFF_SECONDS)
            BackgroundTask.objects.filter(pk=task.pk).update(
                status=BackgroundTask.Status.PENDING,
                attempts=attempts,
                run_at=timezone.now() + timedelta(seconds=delay),
                locked_at=None,
                locked_by="",
                last_error=str(exc)[:2000],
            )
        logger.exception("Background task failed id=%s type=%s", task.pk, task.task_type)
        return False

    BackgroundTask.objects.filter(pk=task.pk).update(
        status=BackgroundTask.Status.COMPLETED,
        locked_at=None,
        locked_by="",
        completed_at=timezone.now(),
        last_error="",
    )
    return True


def _mark_failed(task: BackgroundTask, error: str, *, attempts: int | None = None) -> None:
    BackgroundTask.objects.filter(pk=task.pk).update(
        status=BackgroundTask.Status.FAILED,
        attempts=task.attempts + 1 if attempts is None else attempts,
        locked_at=None,
        locked_by="",
        completed_at=timezone.now(),
        last_error=error[:2000],
    )
