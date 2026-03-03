from django.conf import settings
from django.db import models


class AdminChatFilter(models.Model):
    """Доступ пользователя админки к сообщениям по паре (chat_id, business_connection_id). Можно несколько записей."""
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

class Message(models.Model):
    business_connection_id = models.CharField(verbose_name="Business connection id", default="", blank=True, null=True, max_length=255)
    message_id = models.IntegerField(verbose_name="Message Id", unique=True)
    username_from = models.CharField(verbose_name="Username sender", default="", blank=True, null=True, max_length=255)
    first_name = models.CharField(verbose_name="First name sender", default="", blank=True, null=True, max_length=255)
    text = models.TextField(verbose_name="Text", default="Не текстовое сообщение", blank=True, null=True)
    chat_id = models.IntegerField(verbose_name="Chat id", blank=True, null=True)
    created_at = models.DateTimeField(verbose_name="Создано", auto_now_add=True, null=True, blank=True)

    def __str__(self):
        return f'"{self.first_name}" ({self.created_at}): {self.text}'
    
    class Meta:
        verbose_name = "Сообщение"
        verbose_name_plural = "Сообщения"
