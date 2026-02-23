from django.db import models

class UserTg(models.Model):
    user_id = models.IntegerField(verbose_name="User Id пользователя")
    chat_id = models.IntegerField(verbose_name="Chat Id пользователя с ботом")
    username = models.CharField(verbose_name="Username", default="", max_length=255)

    def __str__(self):
        return f"{self.username} : {self.user_id}"
    
    class Meta:
        verbose_name = "Пользователь бота"
        verbose_name_plural = "Пользователи бота"

class Message(models.Model):
    message_id = models.IntegerField(verbose_name="Message Id", unique=True)
    username_from = models.TextField(verbose_name="Username sender", default="")
    text = models.TextField(verbose_name="Text", default="Не текстовое сообщение", blank=True, null=True)

    def __str__(self):
        return f"{self.username_from}: {self.text}"
    
    class Meta:
        verbose_name = "Сообщение"
        verbose_name_plural = "Сообщения"
    