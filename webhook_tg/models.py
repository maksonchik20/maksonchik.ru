from django.db import models

class UserTg(models.Model):
    user_id = models.IntegerField(verbose_name="User Id пользователя")
    chat_id = models.IntegerField(verbose_name="Chat Id пользователя с ботом")

    def __str__(self):
        return self.text

class Chat(models.Model):
    chat_id = models.IntegerField(verbose_name="Chat Id")
    user1 = models.IntegerField(verbose_name="Пользователь чата 1")
    user2 = models.IntegerField(verbose_name="Пользователь чата 2")

class Message(models.Model):
    message_id = models.IntegerField(verbose_name="Message Id")
    from_user_id = models.IntegerField(verbose_name="From User Id")
    to_user_id = models.IntegerField(verbose_name="From User Id")
    text = models.TextField(verbose_name="Text", null=True, blank=True)
