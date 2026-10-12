"""Durable, owner-scoped history exports outside the incoming command worker."""

import fcntl
import html
import os
import tempfile
from pathlib import Path

from django.db import transaction
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .history_files import HISTORY_FILE_LIMIT, history_directory, history_document_path, open_history_document
from .models import BackgroundTask, Message, TelegramOutbox, UserTg


HISTORY_EXPORT_TASK = "export_message_history"
HISTORY_BATCH_SIZE = 500
HISTORY_FIELDS = (
    "id", "created_at", "message_id", "chat_id", "first_name", "username_from",
    "text", "caption", "file_id", "file_type",
)


def schedule_history_export(bot_user, *, chat_id, target, update_id, contacts=False):
    from .background_tasks import enqueue_background_task

    if chat_id != bot_user.chat_id or chat_id != bot_user.user_id or not bot_user.business_connection_id:
        raise ValueError("History recipient must own the connection")
    suffix = "history:contacts" if contacts else "history"
    task, _ = enqueue_background_task(
        task_type=HISTORY_EXPORT_TASK,
        payload={
            "user_pk": bot_user.pk, "chat_id": chat_id,
            "business_connection_id": bot_user.business_connection_id,
            "target": target, "contacts": contacts, "update_id": update_id,
        },
        run_at=timezone.now(), idempotency_key=f"command:{update_id}:{suffix}:export",
        priority=80,
    )
    return task


def _history_queryset(fieldToPayload):
    queryset = Message.objects.filter(
        business_connection_id=fieldToPayload["business_connection_id"],
        pk__lte=fieldToPayload["upper_pk"],
    )
    target = fieldToPayload["target"]
    if isinstance(target, int):
        return queryset.filter(chat_id=target)
    return queryset.alias(username_normalized=Lower("username_from")).filter(username_normalized=target)


def _row_key(message):
    return [message.created_at.isoformat() if message.created_at else None, message.message_id, message.pk]


def _after_key(queryset, key):
    date, message_id, pk = key
    tail = Q(message_id__gt=message_id) | Q(message_id=message_id, pk__gt=pk)
    if date is None:
        return queryset.filter(created_at__isnull=True).filter(tail)
    date = parse_datetime(date)
    return queryset.filter(Q(created_at__gt=date) | (Q(created_at=date) & tail))


def _iter_history(queryset, *, after_key=None, last_key=None, heartbeat=None):
    # Two explicit keyset scans keep memory bounded even with server cursors
    # disabled. Legacy NULL dates come last, consistently on PostgreSQL/SQLite.
    for null_dates in (False, True):
        if after_key is not None and after_key[0] is None and not null_dates:
            continue
        section = queryset.filter(created_at__isnull=null_dates)
        key = after_key if after_key is not None and (after_key[0] is None) == null_dates else None
        while True:
            page = section if key is None else _after_key(section, key)
            rows = list(page.only(*HISTORY_FIELDS).order_by("created_at", "message_id", "pk")[:HISTORY_BATCH_SIZE])
            if not rows:
                break
            if heartbeat:
                heartbeat()
            for message in rows:
                yield message
                key = _row_key(message)
                if last_key is not None and key == last_key:
                    return


def _message_entry(message):
    from .history import _format_created_at

    author = message.first_name or "Без имени"
    if message.username_from:
        author += f" (@{message.username_from})"
    content = message.text or message.caption or "(без текста)"
    entry = (
        f"[{_format_created_at(message)}] {author}\n"
        f"Chat ID: {message.chat_id}; Message ID: {message.message_id}\n{content}\n"
    )
    if message.file_id:
        entry += f"[вложение: {message.file_type or 'UNKNOWN'}]\n"
    return (entry + "\n").encode("utf-8")


def _description(target):
    if isinstance(target, int):
        return (
            f"История переписки с Telegram ID {target}", f"history_id_{target}.txt",
            f"Сохранённой переписки с Telegram ID <b>{target}</b> пока не найдено.",
        )
    return (
        f"История сообщений от @{target}", f"history_{target}.txt",
        f"Сообщений от <b>@{html.escape(target)}</b> пока не найдено.",
    )


def _finish_file(output, temporary_path, final_path, count, count_offset):
    output.seek(count_offset)
    output.write(str(count).ljust(20).encode())
    output.flush()
    os.fsync(output.fileno())
    output.close()
    os.replace(temporary_path, final_path)
    descriptor = os.open(final_path.parent, os.O_DIRECTORY | os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_history(task, fieldToPayload, *, partFieldToValue=None):
    title, filename, _ = _description(fieldToPayload["target"])
    parts = []
    output = None
    temporary_path = None
    count = 0
    previous_key = partFieldToValue.get("after_key") if partFieldToValue else None
    part_number = partFieldToValue["part"] if partFieldToValue else 1
    after_key = previous_key
    last_key = partFieldToValue.get("last_key") if partFieldToValue else None

    def heartbeat():
        if task.pk:
            BackgroundTask.objects.filter(pk=task.pk, status=BackgroundTask.Status.PROCESSING).update(locked_at=timezone.now())

    def finish():
        nonlocal output, temporary_path
        path = history_document_path(task.idempotency_key, part_number)
        _finish_file(output, temporary_path, path, count, count_offset)
        output, temporary_path = None, None
        parts.append({
            "document_path": str(path), "filename": filename, "count": count,
            "part": part_number, "after_key": after_key, "last_key": previous_key,
        })

    try:
        for message in _iter_history(_history_queryset(fieldToPayload), after_key=after_key, last_key=last_key, heartbeat=heartbeat):
            entry = _message_entry(message)
            if len(entry) > HISTORY_FILE_LIMIT - 4096:
                raise ValueError("One archived message exceeds the history document size limit")
            if output is not None and output.tell() + len(entry) > HISTORY_FILE_LIMIT:
                if partFieldToValue:
                    raise ValueError("Restored history part exceeds its original size limit")
                finish()
                part_number += 1
                count = 0
                after_key = previous_key
            if output is None:
                output = tempfile.NamedTemporaryFile(mode="w+b", prefix=".history-", dir=history_directory(), delete=False)
                temporary_path = Path(output.name)
                output.write(("\ufeff" + title + "\nСообщений: ").encode("utf-8"))
                count_offset = output.tell()
                output.write(b" " * 20 + b"\n\n")
            output.write(entry)
            count += 1
            previous_key = _row_key(message)
        if partFieldToValue and previous_key != last_key:
            raise RuntimeError("Cannot recover the original history range")
        if output is not None:
            finish()
    finally:
        if output is not None:
            output.close()
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return parts


def _enqueue_text(chat_id, text, key):
    from .outbox import enqueue_outbox

    if enqueue_outbox(chat_id=chat_id, method=TelegramOutbox.Method.SEND_MESSAGE,
                      payload={"text": text, "parse_mode": "HTML"}, idempotency_key=key) is None:
        raise RuntimeError("Could not persist history response")


def export_history_task(task):
    from .background_tasks import PermanentTaskError
    from .outbox import enqueue_outbox

    fieldToPayload = task.payload
    user = UserTg.objects.filter(pk=fieldToPayload.get("user_pk")).first()
    if (
        user is None or user.chat_id != fieldToPayload.get("chat_id") or user.user_id != user.chat_id
        or not fieldToPayload.get("business_connection_id")
        or user.business_connection_id != fieldToPayload["business_connection_id"]
    ):
        raise PermanentTaskError("History owner connection changed; request a new export")
    # File generation does not run in a DB transaction. The lock prevents a
    # stale task lease or duplicate worker from replacing a file being built.
    lock_path = history_document_path(task.idempotency_key, 0).with_suffix(".lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a+"):
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        task.refresh_from_db(fields=["payload"])
        fieldToPayload = task.payload
        if "upper_pk" not in fieldToPayload:
            upper_pk = Message.objects.order_by("-pk").values_list("pk", flat=True).first() or 0
            fieldToPayload = {**fieldToPayload, "upper_pk": upper_pk}
            BackgroundTask.objects.filter(pk=task.pk).update(payload=fieldToPayload)
            task.payload = fieldToPayload
        if fieldToPayload.get("contacts"):
            return _export_contacts(task, user, fieldToPayload)
        title, filename, empty_text = _description(fieldToPayload["target"])
        parts = fieldToPayload.get("parts")
        if parts is None:
            try:
                parts = _write_history(task, fieldToPayload)
            except ValueError:
                _enqueue_text(user.chat_id, "Не удалось подготовить историю: размер одного сообщения слишком велик. Обратитесь в поддержку.", f"command:{fieldToPayload['update_id']}:history:limit")
                return
            fieldToPayload = {**fieldToPayload, "parts": parts}
            BackgroundTask.objects.filter(pk=task.pk).update(payload=fieldToPayload)
            task.payload = fieldToPayload
        prefix = f"command:{fieldToPayload['update_id']}:history"
        if not parts:
            _enqueue_text(user.chat_id, empty_text, f"{prefix}:empty")
            return
        # All files are durable before any outbox row becomes visible.
        with transaction.atomic():
            for fieldToPart in parts:
                part = fieldToPart["part"]
                suffix = "" if len(parts) == 1 else f":part:{part}"
                caption = f"<b>{html.escape(title)}</b>: {fieldToPart['count']} шт."
                if len(parts) > 1:
                    caption += f"\nЧасть {part} из {len(parts)}"
                fieldToDocument = {
                    **fieldToPart, "caption": caption, "content_type": "text/plain; charset=utf-8",
                    "history_task_id": task.pk,
                }
                if len(parts) > 1:
                    fieldToDocument["filename"] = filename.removesuffix(".txt") + f"_part{part}.txt"
                if enqueue_outbox(chat_id=user.chat_id, method=TelegramOutbox.Method.SEND_DOCUMENT_FILE,
                                  payload=fieldToDocument, idempotency_key=f"{prefix}:document{suffix}") is None:
                    raise RuntimeError("Could not persist history document")


def _export_contacts(task, user, fieldToPayload):
    # Contacts only load the latest row of each peer, never every message.
    # Move the relatively expensive window query off the command worker too.
    from .history import _build_history_contacts_export
    from .outbox import enqueue_outbox

    prefix = f"command:{fieldToPayload['update_id']}:history:contacts"
    if TelegramOutbox.objects.filter(idempotency_key__in=[f"{prefix}:document", f"{prefix}:empty", f"{prefix}:limit"]).exists():
        return
    path = history_document_path(task.idempotency_key, 1)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=history_directory(), prefix=".history-", delete=False) as output:
            temporary_path = Path(output.name)
            _, count = _build_history_contacts_export(user, upper_pk=fieldToPayload["upper_pk"], output=output)
            if not count:
                _enqueue_text(user.chat_id, "Сохранённых переписок пока нет.", f"{prefix}:empty")
                return
            if output.tell() > HISTORY_FILE_LIMIT:
                _enqueue_text(user.chat_id, "Список переписок слишком большой. Запросите историю конкретного собеседника командой /history ID.", f"{prefix}:limit")
                return
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        descriptor = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    if enqueue_outbox(chat_id=user.chat_id, method=TelegramOutbox.Method.SEND_DOCUMENT_FILE,
                      payload={"document_path": str(path), "filename": "history_contacts.txt",
                               "caption": f"Сохранённых переписок: <b>{count}</b>.\nНайдите собеседника в файле и скопируйте команду <code>/history ID</code> рядом с ним.",
                               "history_task_id": task.pk},
                      idempotency_key=f"{prefix}:document") is None:
        raise RuntimeError("Could not persist contacts document")


def restore_history_document(item):
    """Rebuild a lost pending file from its durable task and original PK bound."""
    task = BackgroundTask.objects.filter(pk=item.payload.get("history_task_id"), task_type=HISTORY_EXPORT_TASK).first()
    if task is None or task.payload.get("chat_id") != item.chat_id or "upper_pk" not in task.payload:
        raise RuntimeError("History spool missing and no matching recovery task")
    expected_path = history_document_path(task.idempotency_key, item.payload.get("part", 1))
    if str(expected_path) != item.payload.get("document_path"):
        raise ValueError("History document does not belong to its task")
    if task.payload.get("contacts"):
        # Never delete a pending outbox row to force regeneration.
        from .history import _build_history_contacts_export
        user = UserTg.objects.get(pk=task.payload["user_pk"], chat_id=item.chat_id,
                                  business_connection_id=task.payload["business_connection_id"])
        with tempfile.NamedTemporaryFile(dir=history_directory(), prefix=".history-", delete=False) as output:
            temporary_path = Path(output.name)
            try:
                _build_history_contacts_export(user, upper_pk=task.payload["upper_pk"], output=output)
                if not 0 < output.tell() <= HISTORY_FILE_LIMIT:
                    raise RuntimeError("Cannot recover history contacts document")
                output.flush()
                os.fsync(output.fileno())
                os.replace(temporary_path, expected_path)
                descriptor = os.open(expected_path.parent, os.O_DIRECTORY | os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            finally:
                temporary_path.unlink(missing_ok=True)
    else:
        try:
            parts = _write_history(task, task.payload, partFieldToValue=item.payload)
        except ValueError as exc:
            raise RuntimeError("Cannot recover the original history range") from exc
        if len(parts) != 1 or parts[0]["last_key"] != item.payload["last_key"]:
            raise RuntimeError("Cannot recover the original history range")
    return open_history_document(expected_path)
