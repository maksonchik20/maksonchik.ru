from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("webhook_tg", "0017_mutedpeer"),
    ]

    operations = [
        migrations.AddField(
            model_name="mutedpeer",
            name="expires_at",
            field=models.DateTimeField(
                blank=True,
                db_index=True,
                help_text="Пусто = навсегда",
                null=True,
                verbose_name="Снять mute после",
            ),
        ),
        migrations.AddField(
            model_name="mutedpeer",
            name="notify_in_bot",
            field=models.BooleanField(default=False, verbose_name="Присылать сообщения в бота"),
        ),
        migrations.AddField(
            model_name="mutedpeer",
            name="warning_sent",
            field=models.BooleanField(
                default=False,
                verbose_name="Предупреждение собеседнику отправлено",
            ),
        ),
        migrations.CreateModel(
            name="MuteSetup",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("owner_user_id", models.BigIntegerField(db_index=True, verbose_name="Telegram user id владельца")),
                ("owner_chat_id", models.BigIntegerField(verbose_name="Chat id владельца с ботом")),
                ("muted_username", models.CharField(max_length=255, verbose_name="Username без @")),
                (
                    "duration_seconds",
                    models.PositiveIntegerField(
                        blank=True,
                        null=True,
                        verbose_name="Длительность, сек (0=навсегда)",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Создано")),
            ],
            options={
                "verbose_name": "Настройка mute (черновик)",
                "verbose_name_plural": "Настройки mute (черновики)",
            },
        ),
    ]
