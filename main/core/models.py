from django.db import models


class Messages(models.Model):
    text = models.TextField(verbose_name="Текст сообщения")

    def __str__(self):
        return self.text
