from django.conf import settings
from django.db import models


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

    def __str__(self):
        return f"{self.username} : {self.user_id}"
    
    class Meta:
        verbose_name = "Пользователь бота"
        verbose_name_plural = "Пользователи бота"

class FileType(models.TextChoices):
    """Тип медиафайла в сообщении."""
    UNKNOWN = "UNKNOWN", "Неизвестно"
    PHOTO = "PHOTO", "Фото"
    AUDIO = "AUDIO", "Аудио"
    VIDEO = "VIDEO", "Видео"
    DOCUMENT = "DOCUMENT", "Документ"


class WebhookUpdate(models.Model):
    update_id = models.BigIntegerField(verbose_name="Telegram update_id", unique=True, db_index=True)
    processed_at = models.DateTimeField(verbose_name="Обработано", auto_now_add=True)

    class Meta:
        verbose_name = "Обработанный webhook"
        verbose_name_plural = "Обработанные webhook"

    def __str__(self):
        return str(self.update_id)


class EditNotificationSent(models.Model):
    """Deprecated: заменено на TelegramOutbox с dedup_key."""

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
    class Method(models.TextChoices):
        SEND_MESSAGE = "sendMessage", "sendMessage"
        SEND_PHOTO = "sendPhoto", "sendPhoto"
        SEND_AUDIO = "sendAudio", "sendAudio"
        SEND_VIDEO = "sendVideo", "sendVideo"
        SEND_DOCUMENT = "sendDocument", "sendDocument"

    chat_id = models.BigIntegerField(verbose_name="Chat id")
    method = models.CharField(
        verbose_name="Метод Telegram API",
        max_length=32,
        choices=Method.choices,
    )
    payload = models.JSONField(verbose_name="Тело запроса (без chat_id)")
    dedup_key = models.CharField(
        verbose_name="Ключ дедупликации",
        max_length=128,
        blank=True,
        null=True,
        unique=True,
    )
    attempts = models.PositiveIntegerField(verbose_name="Попыток отправки", default=0)
    next_attempt_at = models.DateTimeField(verbose_name="Следующая попытка", db_index=True)
    last_error = models.TextField(verbose_name="Последняя ошибка", blank=True, default="")
    created_at = models.DateTimeField(verbose_name="Создано", auto_now_add=True)

    class Meta:
        verbose_name = "Исходящее сообщение (очередь)"
        verbose_name_plural = "Исходящие сообщения (очередь)"

    def __str__(self):
        return f"{self.method} → {self.chat_id} (attempts={self.attempts})"


class BotOutgoingMessage(models.Model):
    chat_id = models.BigIntegerField(verbose_name="Chat id получателя")
    method = models.CharField(verbose_name="Метод Telegram API", max_length=32)
    sent_at = models.DateTimeField(verbose_name="Отправлено", auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Исходящее сообщение бота"
        verbose_name_plural = "Исходящие сообщения бота"

    def __str__(self):
        return f"{self.method} → {self.chat_id} @ {self.sent_at}"


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
