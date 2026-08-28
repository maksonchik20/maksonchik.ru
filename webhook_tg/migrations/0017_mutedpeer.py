from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("webhook_tg", "0016_message_indexes"),
    ]

    operations = [
        migrations.CreateModel(
            name="MutedPeer",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("owner_user_id", models.BigIntegerField(db_index=True, verbose_name="Telegram user id владельца")),
                ("owner_chat_id", models.BigIntegerField(verbose_name="Chat id владельца с ботом")),
                ("muted_username", models.CharField(db_index=True, max_length=255, verbose_name="Username без @")),
                (
                    "muted_user_id",
                    models.BigIntegerField(
                        blank=True,
                        db_index=True,
                        null=True,
                        verbose_name="Telegram user id заглушенного",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Создано")),
            ],
            options={
                "verbose_name": "Заглушенный собеседник",
                "verbose_name_plural": "Заглушенные собеседники",
            },
        ),
        migrations.AddConstraint(
            model_name="mutedpeer",
            constraint=models.UniqueConstraint(
                fields=("owner_user_id", "muted_username"),
                name="wu_muted_owner_username_uniq",
            ),
        ),
    ]
