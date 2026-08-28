from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("webhook_tg", "0018_mute_wizard_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="TelegramIncomingUpdate",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("update_id", models.BigIntegerField(unique=True, verbose_name="Telegram update_id")),
                ("payload", models.JSONField(verbose_name="Telegram update")),
                (
                    "queue",
                    models.CharField(
                        choices=[("priority", "Команды и подключения"), ("business", "Business-события")],
                        max_length=16,
                        verbose_name="Очередь",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Ожидает"),
                            ("processing", "Обрабатывается"),
                            ("done", "Обработано"),
                            ("failed", "Ошибка"),
                        ],
                        default="pending",
                        max_length=16,
                        verbose_name="Статус",
                    ),
                ),
                ("attempts", models.PositiveIntegerField(default=0, verbose_name="Попыток")),
                ("next_attempt_at", models.DateTimeField(db_index=True, verbose_name="Следующая попытка")),
                ("started_at", models.DateTimeField(blank=True, null=True, verbose_name="Начало обработки")),
                ("processed_at", models.DateTimeField(blank=True, null=True, verbose_name="Обработано")),
                ("last_error", models.TextField(blank=True, default="", verbose_name="Последняя ошибка")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Получено")),
            ],
            options={
                "verbose_name": "Входящее обновление Telegram",
                "verbose_name_plural": "Входящие обновления Telegram",
            },
        ),
        migrations.AddIndex(
            model_name="telegramincomingupdate",
            index=models.Index(
                fields=["queue", "status", "next_attempt_at", "created_at"],
                name="tg_incoming_worker_idx",
            ),
        ),
    ]
