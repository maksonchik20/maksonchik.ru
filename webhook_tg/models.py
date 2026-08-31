import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone


def generate_who_update_referral_code():
    return secrets.token_urlsafe(9)


def generate_who_update_tracking_code():
    return secrets.token_urlsafe(9)


class AdminChatFilter(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="admin_chat_filters",
        verbose_name="Пользователь",
    )
    chat_id = models.BigIntegerField(verbose_name="Chat id", help_text="Chat id из Telegram")
    business_connection_id = models.CharField(
        verbose_name="Business connection id",
        max_length=255,
        blank=True,
        null=True,
        help_text="Если указан — доступ только к сообщениям с этим business_connection_id и chat_id. Пусто — все сообщения этого chat_id.",
    )

    class Meta:
        verbose_name = "Доступ к чату (админка)"
        verbose_name_plural = "Доступ к чатам (админка)"
        unique_together = [("user", "chat_id", "business_connection_id")]

    def __str__(self):
        conn = f", {self.business_connection_id}" if self.business_connection_id else ""
        return f"{self.user.username} → chat_id={self.chat_id}{conn}"


class UserTg(models.Model):
    user_id = models.IntegerField(verbose_name="User Id пользователя")
    chat_id = models.IntegerField(verbose_name="Chat Id пользователя с ботом")
    username = models.CharField(verbose_name="Username", default="", max_length=255, blank=True, null=True)
    first_name = models.CharField(verbose_name="First name sender", default="", blank=True, null=True, max_length=255)
    business_connection_id = models.CharField(
        verbose_name="Business connection id",
        max_length=255,
        blank=True,
        null=True,
    )
    business_is_connected = models.BooleanField(
        verbose_name="Бот подключён",
        default=False,
        db_index=True,
    )
    last_start_at = models.DateTimeField(
        verbose_name="Последний /start",
        blank=True,
        null=True,
    )
    business_connected_at = models.DateTimeField(
        verbose_name="Последнее подключение",
        blank=True,
        null=True,
    )
    business_disconnected_at = models.DateTimeField(
        verbose_name="Последнее отключение",
        blank=True,
        null=True,
    )
    connection_reminder_at = models.DateTimeField(
        verbose_name="Напомнить о подключении",
        blank=True,
        null=True,
        db_index=True,
    )
    connection_reminder_sent_at = models.DateTimeField(
        verbose_name="Напоминание о подключении отправлено",
        blank=True,
        null=True,
    )
    referral_code = models.CharField(
        verbose_name="Реферальный код",
        max_length=24,
        unique=True,
        default=generate_who_update_referral_code,
        editable=False,
    )
    referred_by = models.ForeignKey(
        "self",
        verbose_name="Пригласил",
        related_name="referrals",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
    )
    referral_rewarded_at = models.DateTimeField(
        verbose_name="Реферальный бонус начислен",
        blank=True,
        null=True,
    )
    referral_bonus_days = models.PositiveIntegerField(
        verbose_name="Дней за рефералов",
        default=0,
    )
    access_unlimited = models.BooleanField(
        verbose_name="Бессрочный доступ",
        default=True,
        db_index=True,
    )
    trial_started_at = models.DateTimeField(
        verbose_name="Начало пробного периода",
        blank=True,
        null=True,
    )
    access_expires_at = models.DateTimeField(
        verbose_name="Доступ до",
        blank=True,
        null=True,
        db_index=True,
    )
    access_expired_notified_at = models.DateTimeField(
        verbose_name="Уведомление об окончании доступа",
        blank=True,
        null=True,
    )

    def has_active_access(self, at=None):
        if self.access_unlimited:
            return True
        at = at or timezone.now()
        return bool(self.access_expires_at and self.access_expires_at > at)

    def ensure_trial_started(self, at=None):
        if self.access_unlimited or self.trial_started_at:
            return False
        at = at or timezone.now()
        self.trial_started_at = at
        trial_days = int(getattr(settings, "WHO_UPDATE_TRIAL_DAYS", 7))
        self.access_expires_at = at + timedelta(
            days=trial_days + self.referral_bonus_days
        )
        self.access_expired_notified_at = None
        self.save(
            update_fields=[
                "trial_started_at",
                "access_expires_at",
                "access_expired_notified_at",
            ]
        )
        return True

    def extend_access(self, days, at=None):
        at = at or timezone.now()
        base = max(filter(None, (at, self.access_expires_at)))
        self.access_unlimited = False
        self.access_expires_at = base + timedelta(days=days)
        self.access_expired_notified_at = None
        self.save(
            update_fields=[
                "access_unlimited",
                "access_expires_at",
                "access_expired_notified_at",
            ]
        )

    def __str__(self):
        return f"{self.username} : {self.user_id}"
    
    class Meta:
        verbose_name = "Пользователь бота"
        verbose_name_plural = "Пользователи бота"


class WhoUpdateOnboardingFunnel(models.Model):
    """Одна попытка пройти путь от лендинга или /start до подключения."""

    class ConnectionStage(models.TextChoices):
        IMMEDIATE = "immediate", "Сразу"
        AFTER_FIRST_REMINDER = "after_first", "После первого напоминания"
        AFTER_SECOND_REMINDER = "after_second", "После второго напоминания"
        UNKNOWN = "unknown", "Не удалось определить"

    tracking_code = models.CharField(
        verbose_name="Код сквозной аналитики",
        max_length=24,
        unique=True,
        default=generate_who_update_tracking_code,
        editable=False,
    )
    user = models.ForeignKey(
        UserTg,
        verbose_name="Пользователь",
        related_name="onboarding_funnels",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
    )
    landing_path = models.CharField(max_length=255, blank=True, default="")
    utm_source = models.CharField(max_length=255, blank=True, default="", db_index=True)
    utm_medium = models.CharField(max_length=255, blank=True, default="")
    utm_campaign = models.CharField(max_length=255, blank=True, default="", db_index=True)
    utm_content = models.CharField(max_length=255, blank=True, default="")
    utm_term = models.CharField(max_length=255, blank=True, default="")
    utm_device = models.CharField(max_length=64, blank=True, default="", db_index=True)
    utm_region = models.CharField(max_length=64, blank=True, default="", db_index=True)
    yclid = models.CharField(max_length=255, blank=True, default="", db_index=True)
    metrika_client_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        db_index=True,
    )
    landing_viewed_at = models.DateTimeField(blank=True, null=True, db_index=True)
    telegram_started_at = models.DateTimeField(blank=True, null=True, db_index=True)
    start_update_id = models.BigIntegerField(blank=True, null=True, unique=True)
    demo_opened_at = models.DateTimeField(blank=True, null=True, db_index=True)
    first_reminder_sent_at = models.DateTimeField(blank=True, null=True, db_index=True)
    second_reminder_sent_at = models.DateTimeField(blank=True, null=True, db_index=True)
    connected_at = models.DateTimeField(blank=True, null=True, db_index=True)
    connection_stage = models.CharField(
        verbose_name="Когда подключился",
        max_length=24,
        choices=ConnectionStage.choices,
        blank=True,
        default="",
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Воронка подключения WhoUpdate"
        verbose_name_plural = "Воронки подключения WhoUpdate"
        ordering = ("-created_at",)
        indexes = [
            models.Index(
                fields=("user", "-telegram_started_at"),
                name="wu_funnel_user_started",
            ),
        ]

    def __str__(self):
        user = self.user.username if self.user_id and self.user.username else self.user_id or "—"
        return f"{self.tracking_code} → {user}"


class WhoUpdateMetrikaConversion(models.Model):
    """Надёжная очередь офлайн-конверсий для Яндекс Метрики."""

    class EventType(models.TextChoices):
        START = "start", "/start"
        CONNECTED = "connected", "Полное подключение"

    class IdentifierType(models.TextChoices):
        YCLID = "yclid", "YCLID"
        CLIENT_ID = "client_id", "ClientID"

    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает отправки"
        SUBMITTED = "submitted", "Принято Метрикой"
        PROCESSED = "processed", "Обработано Метрикой"
        FAILED = "failed", "Ошибка сопоставления"

    funnel = models.ForeignKey(
        WhoUpdateOnboardingFunnel,
        verbose_name="Воронка",
        related_name="metrika_conversions",
        on_delete=models.CASCADE,
    )
    event_type = models.CharField(max_length=16, choices=EventType.choices)
    target = models.CharField(max_length=64)
    counter_id = models.PositiveBigIntegerField(
        verbose_name="Счётчик Метрики",
        default=111680333,
        db_index=True,
    )
    occurred_at = models.DateTimeField(db_index=True)
    identifier_type = models.CharField(max_length=16, choices=IdentifierType.choices)
    identifier = models.CharField(max_length=255)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    api_upload_id = models.BigIntegerField(blank=True, null=True, db_index=True)
    api_status = models.CharField(max_length=32, blank=True, default="")
    last_error = models.TextField(blank=True, default="")
    submitted_at = models.DateTimeField(blank=True, null=True)
    processed_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Офлайн-конверсия WhoUpdate"
        verbose_name_plural = "Офлайн-конверсии WhoUpdate"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("funnel", "event_type"),
                name="wu_metrika_funnel_event_unique",
            ),
        ]
        indexes = [
            models.Index(
                fields=("status", "next_attempt_at"),
                name="wu_metrika_status_due",
            ),
        ]

    def __str__(self):
        return f"{self.funnel.tracking_code}: {self.event_type} ({self.status})"


class WhoUpdatePaymentOrder(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает оплаты"
        PAID = "paid", "Оплачен"
        CANCELED = "canceled", "Отменён"
        FAILED = "failed", "Ошибка"

    class Plan(models.TextChoices):
        MONTH = "month", "1 месяц"
        THREE_MONTHS = "three_months", "3 месяца"
        YEAR = "year", "1 год"

    id = models.AutoField(primary_key=True)
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(
        UserTg,
        on_delete=models.CASCADE,
        related_name="payment_orders",
        verbose_name="Пользователь",
    )
    plan = models.CharField(max_length=24, choices=Plan.choices, verbose_name="Тариф")
    duration_days = models.PositiveSmallIntegerField(verbose_name="Дней доступа")
    amount = models.DecimalField(max_digits=8, decimal_places=2, verbose_name="Сумма")
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
        verbose_name="Статус",
    )
    yookassa_payment_id = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        verbose_name="ID платежа ЮKassa",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(blank=True, null=True)
    access_expires_at_after = models.DateTimeField(blank=True, null=True)

    class Meta:
        verbose_name = "Оплата WhoUpdate"
        verbose_name_plural = "Оплаты WhoUpdate"
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.user} — {self.get_plan_display()} ({self.get_status_display()})"


class OperationalMetricBucket(models.Model):
    """Минутный буфер метрик перед отправкой в Yandex Monitoring."""

    metric_name = models.CharField(max_length=128, db_index=True)
    minute = models.DateTimeField(db_index=True)
    labels_hash = models.CharField(max_length=64)
    labels = models.JSONField(default=dict)
    count = models.PositiveBigIntegerField(default=0)
    total = models.FloatField(default=0)
    maximum = models.FloatField(default=0)
    exported_at = models.DateTimeField(blank=True, null=True, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("metric_name", "minute", "labels_hash"),
                name="unique_operational_metric_bucket",
            )
        ]
        ordering = ("minute", "metric_name")

    def __str__(self):
        return f"{self.metric_name} @ {self.minute:%Y-%m-%d %H:%M}"


class MutedPeer(models.Model):
    """Пользователи, чьи входящие business-сообщения автоудаляются у обоих."""

    owner_user_id = models.BigIntegerField(verbose_name="Telegram user id владельца", db_index=True)
    owner_chat_id = models.BigIntegerField(verbose_name="Chat id владельца с ботом")
    muted_username = models.CharField(
        verbose_name="Username без @",
        max_length=255,
        db_index=True,
    )
    muted_user_id = models.BigIntegerField(
        verbose_name="Telegram user id заглушенного",
        blank=True,
        null=True,
        db_index=True,
    )
    expires_at = models.DateTimeField(
        verbose_name="Снять mute после",
        blank=True,
        null=True,
        db_index=True,
        help_text="Пусто = навсегда",
    )
    notify_in_bot = models.BooleanField(
        verbose_name="Присылать сообщения в бота",
        default=False,
    )
    warning_sent = models.BooleanField(
        verbose_name="Предупреждение собеседнику отправлено",
        default=False,
    )
    created_at = models.DateTimeField(verbose_name="Создано", auto_now_add=True)

    class Meta:
        verbose_name = "Заглушенный собеседник"
        verbose_name_plural = "Заглушенные собеседники"
        constraints = [
            models.UniqueConstraint(
                fields=["owner_user_id", "muted_username"],
                name="wu_muted_owner_username_uniq",
            ),
        ]

    def __str__(self):
        return f"{self.owner_user_id} mutes @{self.muted_username}"


class MuteSetup(models.Model):
    """Черновик настройки /mute (срок → уведомления в боте)."""

    owner_user_id = models.BigIntegerField(verbose_name="Telegram user id владельца", db_index=True)
    owner_chat_id = models.BigIntegerField(verbose_name="Chat id владельца с ботом")
    muted_username = models.CharField(verbose_name="Username без @", max_length=255)
    duration_seconds = models.PositiveIntegerField(
        verbose_name="Длительность, сек (0=навсегда)",
        blank=True,
        null=True,
    )
    created_at = models.DateTimeField(verbose_name="Создано", auto_now_add=True)

    class Meta:
        verbose_name = "Настройка mute (черновик)"
        verbose_name_plural = "Настройки mute (черновики)"

    def __str__(self):
        return f"setup @{self.muted_username} for {self.owner_user_id}"


class FileType(models.TextChoices):
    """Тип медиафайла в сообщении."""
    UNKNOWN = "UNKNOWN", "Неизвестно"
    PHOTO = "PHOTO", "Фото"
    AUDIO = "AUDIO", "Аудио"
    VIDEO = "VIDEO", "Видео"
    DOCUMENT = "DOCUMENT", "Документ"
    STICKER = "STICKER", "Стикер"
    ANIMATION = "ANIMATION", "GIF"


class WebhookUpdate(models.Model):
    update_id = models.BigIntegerField(verbose_name="Telegram update_id", unique=True, db_index=True)
    processed_at = models.DateTimeField(verbose_name="Обработано", auto_now_add=True)

    class Meta:
        verbose_name = "Обработанный webhook"
        verbose_name_plural = "Обработанные webhook"

    def __str__(self):
        return str(self.update_id)


class TelegramIncomingUpdate(models.Model):
    """Надёжная входящая очередь между Telegram webhook и обработчиками."""

    class Queue(models.TextChoices):
        PRIORITY = "priority", "Команды и подключения"
        BUSINESS = "business", "Business-события"

    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает"
        PROCESSING = "processing", "Обрабатывается"
        DONE = "done", "Обработано"
        FAILED = "failed", "Ошибка"

    update_id = models.BigIntegerField(verbose_name="Telegram update_id", unique=True)
    payload = models.JSONField(verbose_name="Telegram update")
    queue = models.CharField(verbose_name="Очередь", max_length=16, choices=Queue.choices)
    status = models.CharField(
        verbose_name="Статус",
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    attempts = models.PositiveIntegerField(verbose_name="Попыток", default=0)
    next_attempt_at = models.DateTimeField(verbose_name="Следующая попытка", db_index=True)
    started_at = models.DateTimeField(verbose_name="Начало обработки", blank=True, null=True)
    processed_at = models.DateTimeField(verbose_name="Обработано", blank=True, null=True)
    last_error = models.TextField(verbose_name="Последняя ошибка", blank=True, default="")
    created_at = models.DateTimeField(verbose_name="Получено", auto_now_add=True)

    class Meta:
        verbose_name = "Входящее обновление Telegram"
        verbose_name_plural = "Входящие обновления Telegram"
        indexes = [
            models.Index(
                fields=["queue", "status", "next_attempt_at", "created_at"],
                name="tg_incoming_worker_idx",
            ),
        ]

    def __str__(self):
        return f"{self.update_id}: {self.queue}/{self.status}"


class BackgroundTask(models.Model):
    """Надёжная SQLite-очередь отложенных фоновых действий."""

    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает"
        PROCESSING = "processing", "Выполняется"
        COMPLETED = "completed", "Выполнена"
        FAILED = "failed", "Ошибка"
        CANCELLED = "cancelled", "Отменена"

    task_type = models.CharField(verbose_name="Тип задачи", max_length=100, db_index=True)
    payload = models.JSONField(verbose_name="Параметры", default=dict)
    status = models.CharField(
        verbose_name="Статус",
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    priority = models.SmallIntegerField(verbose_name="Приоритет", default=100)
    run_at = models.DateTimeField(verbose_name="Выполнить не раньше", db_index=True)
    attempts = models.PositiveIntegerField(verbose_name="Попыток", default=0)
    max_attempts = models.PositiveIntegerField(verbose_name="Максимум попыток", default=10)
    locked_at = models.DateTimeField(verbose_name="Взята в работу", blank=True, null=True)
    locked_by = models.CharField(verbose_name="Воркер", max_length=100, blank=True, default="")
    last_error = models.TextField(verbose_name="Последняя ошибка", blank=True, default="")
    idempotency_key = models.CharField(
        verbose_name="Ключ идемпотентности",
        max_length=255,
        unique=True,
    )
    created_at = models.DateTimeField(verbose_name="Создана", auto_now_add=True)
    updated_at = models.DateTimeField(verbose_name="Обновлена", auto_now=True)
    completed_at = models.DateTimeField(verbose_name="Завершена", blank=True, null=True)

    class Meta:
        verbose_name = "Фоновая задача"
        verbose_name_plural = "Фоновые задачи"
        indexes = [
            models.Index(
                fields=["status", "run_at", "priority", "created_at"],
                name="background_task_queue_idx",
            ),
            models.Index(
                fields=["status", "locked_at"],
                name="background_task_lock_idx",
            ),
        ]

    def __str__(self):
        return f"{self.task_type}: {self.status} ({self.run_at:%d.%m.%Y %H:%M})"


class EditNotificationSent(models.Model):
    """Deprecated: заменено на TelegramOutbox с idempotency_key."""

    editor_id = models.BigIntegerField(verbose_name="ID редактора")
    edit_date = models.BigIntegerField(verbose_name="edit_date из Telegram")
    text_hash = models.CharField(verbose_name="Хеш нового текста", max_length=16)
    sent_at = models.DateTimeField(verbose_name="Отправлено", auto_now_add=True)

    class Meta:
        verbose_name = "Отправленное уведомление об редактировании"
        verbose_name_plural = "Отправленные уведомления об редактировании"
        constraints = [
            models.UniqueConstraint(
                fields=["editor_id", "edit_date", "text_hash"],
                name="webhook_tg_edit_notification_uniq",
            ),
        ]


class TelegramOutbox(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает"
        SENT = "sent", "Отправлено"
        DROPPED = "dropped", "Не доставляется"

    class Method(models.TextChoices):
        SEND_MESSAGE = "sendMessage", "sendMessage"
        SEND_PHOTO = "sendPhoto", "sendPhoto"
        SEND_AUDIO = "sendAudio", "sendAudio"
        SEND_VIDEO = "sendVideo", "sendVideo"
        SEND_DOCUMENT = "sendDocument", "sendDocument"
        SEND_DOCUMENT_BYTES = "sendDocumentBytes", "sendDocumentBytes"

    chat_id = models.BigIntegerField(verbose_name="Chat id")
    method = models.CharField(
        verbose_name="Метод Telegram API",
        max_length=32,
        choices=Method.choices,
    )
    payload = models.JSONField(verbose_name="Тело запроса (без chat_id)")
    status = models.CharField(
        verbose_name="Статус",
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    idempotency_key = models.CharField(
        verbose_name="Ключ идемпотентности",
        max_length=128,
        blank=True,
        null=True,
        unique=True,
    )
    attempts = models.PositiveIntegerField(verbose_name="Попыток отправки", default=0)
    next_attempt_at = models.DateTimeField(verbose_name="Следующая попытка", db_index=True)
    last_error = models.TextField(verbose_name="Последняя ошибка", blank=True, default="")
    created_at = models.DateTimeField(verbose_name="Создано", auto_now_add=True)
    sent_at = models.DateTimeField(verbose_name="Отправлено", blank=True, null=True)

    class Meta:
        verbose_name = "Исходящее сообщение (очередь)"
        verbose_name_plural = "Исходящие сообщения (очередь)"
        indexes = [
            models.Index(
                fields=["status", "next_attempt_at"],
                name="wu_outbox_status_due",
            ),
        ]

    def __str__(self):
        return f"{self.method} → {self.chat_id}: {self.status} (attempts={self.attempts})"


class BotOutgoingMessage(models.Model):
    chat_id = models.BigIntegerField(verbose_name="Chat id получателя")
    method = models.CharField(verbose_name="Метод Telegram API", max_length=32)
    sent_at = models.DateTimeField(verbose_name="Отправлено", auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Исходящее сообщение бота"
        verbose_name_plural = "Исходящие сообщения бота"

    def __str__(self):
        return f"{self.method} → {self.chat_id} @ {self.sent_at}"


class BotChatEvent(models.Model):
    """Сообщения и действия в личном диалоге пользователя с ботом."""

    class Direction(models.TextChoices):
        USER = "user", "Пользователь"
        BOT = "bot", "Бот"

    class EventType(models.TextChoices):
        MESSAGE = "message", "Сообщение"
        EDITED_MESSAGE = "edited_message", "Изменённое сообщение"
        CALLBACK = "callback", "Нажатие кнопки"
        PHOTO = "photo", "Фото"
        VIDEO = "video", "Видео"
        AUDIO = "audio", "Аудио"
        DOCUMENT = "document", "Документ"
        MEDIA_GROUP = "media_group", "Альбом"
        OTHER = "other", "Другое"

    chat_id = models.BigIntegerField(verbose_name="Chat id", db_index=True)
    direction = models.CharField(
        verbose_name="Направление",
        max_length=8,
        choices=Direction.choices,
        db_index=True,
    )
    event_type = models.CharField(
        verbose_name="Тип события",
        max_length=32,
        choices=EventType.choices,
        default=EventType.MESSAGE,
    )
    text = models.TextField(verbose_name="Текст", blank=True, default="")
    payload = models.JSONField(verbose_name="Данные Telegram", blank=True, default=dict)
    telegram_message_id = models.BigIntegerField(
        verbose_name="Telegram message id",
        blank=True,
        null=True,
    )
    update_id = models.BigIntegerField(
        verbose_name="Telegram update id",
        blank=True,
        null=True,
        db_index=True,
    )
    source_key = models.CharField(
        verbose_name="Ключ источника",
        max_length=128,
        blank=True,
        null=True,
        unique=True,
        help_text="Защищает входящие события от повторной записи.",
    )
    created_at = models.DateTimeField(verbose_name="Создано", auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Событие диалога с ботом"
        verbose_name_plural = "Диалоги пользователей с ботом"
        indexes = [
            models.Index(fields=["chat_id", "created_at"], name="wu_bot_chat_timeline"),
        ]
        ordering = ("created_at", "id")

    def __str__(self):
        return f"{self.get_direction_display()} → {self.chat_id}: {self.text[:60]}"


class Message(models.Model):
    business_connection_id = models.CharField(verbose_name="Business connection id", default="", blank=True, null=True, max_length=255)
    message_id = models.IntegerField(verbose_name="Message Id")
    username_from = models.CharField(verbose_name="Username sender", default="", blank=True, null=True, max_length=255)
    first_name = models.CharField(verbose_name="First name sender", default="", blank=True, null=True, max_length=255)
    text = models.TextField(verbose_name="Text", default="", blank=True, null=True)
    chat_id = models.BigIntegerField(verbose_name="Chat id")
    file_id = models.CharField(verbose_name="File id", max_length=255, blank=True, null=True)
    file_type = models.CharField(
        verbose_name="Тип файла",
        max_length=20,
        choices=FileType.choices,
        default=FileType.UNKNOWN,
        blank=True,
        null=True,
    )
    caption = models.TextField(verbose_name="Текст к файлу", blank=True, null=True)
    payload = models.TextField(verbose_name="payload", default="", blank=True, null=True)
    created_at = models.DateTimeField(verbose_name="Создано", auto_now_add=True, null=True, blank=True)

    def __str__(self):
        return f'"{self.first_name}" ({self.created_at}): {self.text}'

    class Meta:
        verbose_name = "Сообщение"
        verbose_name_plural = "Сообщения"
        constraints = [
            models.UniqueConstraint(
                fields=["chat_id", "message_id"],
                name="webhook_tg_message_chat_message_id_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["-created_at"], name="wu_msg_created_desc"),
            models.Index(fields=["chat_id", "-created_at"], name="wu_msg_chat_created"),
            models.Index(fields=["chat_id", "-id"], name="wu_msg_chat_id_desc"),
            models.Index(
                fields=["chat_id", "business_connection_id", "-id"],
                name="wu_msg_chat_conn_id",
            ),
            models.Index(
                models.F("business_connection_id"),
                Lower("username_from"),
                models.F("created_at"),
                models.F("message_id"),
                name="wu_msg_history_user",
            ),
            models.Index(fields=["file_id"], name="wu_msg_file_id"),
        ]
