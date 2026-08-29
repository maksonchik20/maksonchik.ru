from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("webhook_tg", "0024_background_task_and_outbox_idempotency"),
    ]

    operations = [
        migrations.CreateModel(
            name="BotChatEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("chat_id", models.BigIntegerField(db_index=True, verbose_name="Chat id")),
                ("direction", models.CharField(choices=[("user", "Пользователь"), ("bot", "Бот")], db_index=True, max_length=8, verbose_name="Направление")),
                ("event_type", models.CharField(choices=[("message", "Сообщение"), ("edited_message", "Изменённое сообщение"), ("callback", "Нажатие кнопки"), ("photo", "Фото"), ("video", "Видео"), ("audio", "Аудио"), ("document", "Документ"), ("media_group", "Альбом"), ("other", "Другое")], default="message", max_length=32, verbose_name="Тип события")),
                ("text", models.TextField(blank=True, default="", verbose_name="Текст")),
                ("payload", models.JSONField(blank=True, default=dict, verbose_name="Данные Telegram")),
                ("telegram_message_id", models.BigIntegerField(blank=True, null=True, verbose_name="Telegram message id")),
                ("update_id", models.BigIntegerField(blank=True, db_index=True, null=True, verbose_name="Telegram update id")),
                ("source_key", models.CharField(blank=True, help_text="Защищает входящие события от повторной записи.", max_length=128, null=True, unique=True, verbose_name="Ключ источника")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="Создано")),
            ],
            options={
                "verbose_name": "Событие диалога с ботом",
                "verbose_name_plural": "Диалоги пользователей с ботом",
                "ordering": ("created_at", "id"),
            },
        ),
        migrations.AddIndex(
            model_name="botchatevent",
            index=models.Index(fields=["chat_id", "created_at"], name="wu_bot_chat_timeline"),
        ),
    ]
