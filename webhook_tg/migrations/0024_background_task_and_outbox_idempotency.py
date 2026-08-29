from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("webhook_tg", "0023_operationalmetricbucket"),
    ]

    operations = [
        migrations.RenameField(
            model_name="telegramoutbox",
            old_name="dedup_key",
            new_name="idempotency_key",
        ),
        migrations.AlterField(
            model_name="telegramoutbox",
            name="idempotency_key",
            field=models.CharField(
                blank=True,
                max_length=128,
                null=True,
                unique=True,
                verbose_name="Ключ идемпотентности",
            ),
        ),
        migrations.CreateModel(
            name="BackgroundTask",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("task_type", models.CharField(db_index=True, max_length=100, verbose_name="Тип задачи")),
                ("payload", models.JSONField(default=dict, verbose_name="Параметры")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Ожидает"),
                            ("processing", "Выполняется"),
                            ("completed", "Выполнена"),
                            ("failed", "Ошибка"),
                            ("cancelled", "Отменена"),
                        ],
                        default="pending",
                        max_length=16,
                        verbose_name="Статус",
                    ),
                ),
                ("priority", models.SmallIntegerField(default=100, verbose_name="Приоритет")),
                ("run_at", models.DateTimeField(db_index=True, verbose_name="Выполнить не раньше")),
                ("attempts", models.PositiveIntegerField(default=0, verbose_name="Попыток")),
                ("max_attempts", models.PositiveIntegerField(default=10, verbose_name="Максимум попыток")),
                ("locked_at", models.DateTimeField(blank=True, null=True, verbose_name="Взята в работу")),
                ("locked_by", models.CharField(blank=True, default="", max_length=100, verbose_name="Воркер")),
                ("last_error", models.TextField(blank=True, default="", verbose_name="Последняя ошибка")),
                (
                    "idempotency_key",
                    models.CharField(max_length=255, unique=True, verbose_name="Ключ идемпотентности"),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Создана")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Обновлена")),
                ("completed_at", models.DateTimeField(blank=True, null=True, verbose_name="Завершена")),
            ],
            options={
                "verbose_name": "Фоновая задача",
                "verbose_name_plural": "Фоновые задачи",
                "indexes": [
                    models.Index(
                        fields=["status", "run_at", "priority", "created_at"],
                        name="background_task_queue_idx",
                    ),
                    models.Index(
                        fields=["status", "locked_at"],
                        name="background_task_lock_idx",
                    ),
                ],
            },
        ),
    ]
