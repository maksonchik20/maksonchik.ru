from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("webhook_tg", "0013_telegram_outbox"),
    ]

    operations = [
        migrations.CreateModel(
            name="BotOutgoingMessage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("chat_id", models.BigIntegerField(verbose_name="Chat id получателя")),
                ("method", models.CharField(max_length=32, verbose_name="Метод Telegram API")),
                ("sent_at", models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="Отправлено")),
            ],
            options={
                "verbose_name": "Исходящее сообщение бота",
                "verbose_name_plural": "Исходящие сообщения бота",
            },
        ),
    ]
