from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("webhook_tg", "0012_edit_notification_sent"),
    ]

    operations = [
        migrations.CreateModel(
            name="TelegramOutbox",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("chat_id", models.BigIntegerField(verbose_name="Chat id")),
                (
                    "method",
                    models.CharField(
                        choices=[
                            ("sendMessage", "sendMessage"),
                            ("sendPhoto", "sendPhoto"),
                            ("sendAudio", "sendAudio"),
                            ("sendVideo", "sendVideo"),
                            ("sendDocument", "sendDocument"),
                        ],
                        max_length=32,
                        verbose_name="Метод Telegram API",
                    ),
                ),
                ("payload", models.JSONField(verbose_name="Тело запроса (без chat_id)")),
                (
                    "dedup_key",
                    models.CharField(
                        blank=True,
                        max_length=128,
                        null=True,
                        unique=True,
                        verbose_name="Ключ дедупликации",
                    ),
                ),
                ("attempts", models.PositiveIntegerField(default=0, verbose_name="Попыток отправки")),
                ("next_attempt_at", models.DateTimeField(db_index=True, verbose_name="Следующая попытка")),
                ("last_error", models.TextField(blank=True, default="", verbose_name="Последняя ошибка")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Создано")),
            ],
            options={
                "verbose_name": "Исходящее сообщение (очередь)",
                "verbose_name_plural": "Исходящие сообщения (очередь)",
            },
        ),
    ]
