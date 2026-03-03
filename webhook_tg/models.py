from django.conf import settings
from django.db import models


class AdminChatFilter(models.Model):
    """Привязка пользователя админки к chat_id: пользователь видит только Message с этими chat_id (можно несколько)."""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="admin_chat_filters",
        verbose_name="Пользователь",
    )
    chat_id = models.BigIntegerField(verbose_name="Chat id", help_text="Пользователь видит сообщения с этим chat_id")

    class Meta:
        verbose_name = "Доступ к чату (админка)"
        verbose_name_plural = "Доступ к чатам (админка)"
        unique_together = [("user", "chat_id")]

    def __str__(self):
        return f"{self.user.username} → chat_id={self.chat_id}"


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
