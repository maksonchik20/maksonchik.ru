"""Bounded, restart-safe activity summaries for the legacy silence monitor."""

import fcntl
import json
import logging
import os
import tempfile
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import requests
from django.utils import timezone

from .metrics import INCOMING_UPDATES_PROCESSED
from .models import Message, OperationalMetricBucket

logger = logging.getLogger(__name__)
DEFAULT_STATE_PATH = "/var/lib/who-update-event-mirror/state.json"
BATCH_LIMIT = 5000


def _state_path(state_path=None):
    return Path(state_path or os.environ.get("WHO_UPDATE_EVENT_STATE_PATH", DEFAULT_STATE_PATH))


def _save_state(path, fieldToState):
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".state-", delete=False) as output:
            temporary_path = output.name
            json.dump(fieldToState, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path is not None:
            os.unlink(temporary_path)


def _load_state(path):
    with path.open() as source:
        fieldToState = json.load(source)
    if (
        not isinstance(fieldToState, dict)
        or fieldToState.get("version") != 1
        or type(fieldToState.get("last_message_pk")) is not int
        or fieldToState["last_message_pk"] < 0
    ):
        raise ValueError("Invalid activity mirror cursor; refusing to reset it")
    return fieldToState


def _locked_state_file(path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    lock_file = os.fdopen(descriptor, "a+")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        lock_file.close()
        raise
    return lock_file


def initialize_activity_mirror(*, state_path=None):
    """Seed before replacing the per-event reporter; never reset an existing cursor."""
    path = _state_path(state_path)
    with _locked_state_file(path):
        if path.exists():
            return _load_state(path)
        latest = Message.objects.order_by("-pk").values_list("pk", "created_at").first()
        fieldToState = {
            "version": 1,
            "initialized_at": timezone.now().isoformat(),
            "last_message_pk": latest[0] if latest else 0,
            "last_event_at": latest[1].isoformat() if latest and latest[1] else None,
            "pending": None,
        }
        _save_state(path, fieldToState)
        return fieldToState


def _prepare_batch(fieldToState):
    rows = list(
        Message.objects.filter(pk__gt=fieldToState["last_message_pk"])
        .order_by("pk").values_list("pk", "created_at")[:BATCH_LIMIT]
    )
    # This existing durable signal covers edits/deletions/commands even when
    # there are no newly archived messages. The indexed time range is bounded.
    activity_minute = (
        OperationalMetricBucket.objects.filter(
            metric_name=INCOMING_UPDATES_PROCESSED,
            minute__gte=timezone.now() - timedelta(hours=1),
            labels__status="success",
        ).order_by("-minute").values_list("minute", flat=True).first()
    )
    event_times = [created_at.isoformat() for _, created_at in rows if created_at]
    if activity_minute is not None:
        event_times.append(activity_minute.isoformat())
    if fieldToState.get("last_event_at"):
        event_times.append(fieldToState["last_event_at"])
    last_event_at = max(event_times) if event_times else None
    return {
        "kind": "activity_batch_v1",
        "source": "who-update-main",
        "batch_key": str(uuid4()),
        "new_archived_messages": len(rows),
        "first_message_pk": rows[0][0] if rows else None,
        "last_message_pk": rows[-1][0] if rows else fieldToState["last_message_pk"],
        "last_event_at": last_event_at,
    }


def publish_activity_mirror(*, state_path=None):
    """Publish at most one small batch; failures never interrupt metrics or bots."""
    try:
        from env import WHO_UPDATE_EVENT_URL, WHO_UPDATE_EVENT_TOKEN
        if not WHO_UPDATE_EVENT_URL or not WHO_UPDATE_EVENT_TOKEN:
            return False
        path = _state_path(state_path)
        with _locked_state_file(path):
            # Fail closed if deployment forgot initialization: silently seeding
            # here could skip messages received after the old reporter stopped.
            fieldToState = _load_state(path)
            fieldToPayload = fieldToState.get("pending")
            if fieldToPayload is None:
                fieldToPayload = _prepare_batch(fieldToState)
                fieldToState["pending"] = fieldToPayload
                _save_state(path, fieldToState)
            response = requests.post(
                WHO_UPDATE_EVENT_URL,
                json=fieldToPayload,
                headers={"X-Who-Update-Token": WHO_UPDATE_EVENT_TOKEN},
                timeout=(2, 3),
                allow_redirects=False,
            )
            if not 200 <= response.status_code < 300:
                raise RuntimeError("Activity receiver returned HTTP %s" % response.status_code)
            fieldToState["last_message_pk"] = fieldToPayload["last_message_pk"]
            fieldToState["last_event_at"] = fieldToPayload["last_event_at"]
            fieldToState["pending"] = None
            _save_state(path, fieldToState)
            return True
    except BlockingIOError:
        return False
    except Exception:
        logger.exception("Activity mirror batch failed; pending batch will be retried")
        return False
