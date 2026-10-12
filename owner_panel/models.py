import uuid
from django.conf import settings
from django.db import models
from django.utils import timezone


class PushSubscription(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    endpoint_hash = models.CharField(max_length=64, unique=True)
    subscription = models.JSONField()
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)


class NotificationState(models.Model):
    name = models.CharField(max_length=32, primary_key=True)
    activated_at = models.DateTimeField(default=timezone.now)
    last_poll_at = models.DateTimeField(null=True)
    last_error = models.CharField(max_length=240, blank=True)


class PushEvent(models.Model):
    key = models.CharField(max_length=180, unique=True)
    category = models.CharField(max_length=32)
    occurred_at = models.DateTimeField(db_index=True)
    payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)


class PushDelivery(models.Model):
    event = models.ForeignKey(PushEvent, on_delete=models.CASCADE)
    subscription = models.ForeignKey(PushSubscription, on_delete=models.CASCADE)
    status = models.CharField(max_length=12, default="pending", db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    sent_at = models.DateTimeField(null=True)
    last_error = models.CharField(max_length=240, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["event", "subscription"], name="panel_push_event_subscription"
            )
        ]


class PanelAudit(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    request_id = models.UUIDField(default=uuid.uuid4, unique=True)
    action = models.CharField(max_length=32)
    object_id = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)


class LoginThrottle(models.Model):
    key = models.CharField(max_length=64, primary_key=True)
    failures = models.PositiveSmallIntegerField(default=0)
    window_started_at = models.DateTimeField(default=timezone.now)
