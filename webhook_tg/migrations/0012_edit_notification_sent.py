from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("webhook_tg", "0011_webhook_update_and_message_chat_uniq"),
    ]

    operations = [
        migrations.CreateModel(
            name="EditNotificationSent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("editor_id", models.BigIntegerField(verbose_name="ID редактора")),
                ("edit_date", models.BigIntegerField(verbose_name="edit_date из Telegram")),
                ("text_hash", models.CharField(max_length=16, verbose_name="Хеш нового текста")),
                ("sent_at", models.DateTimeField(auto_now_add=True, verbose_name="Отправлено")),
            ],
            options={
                "verbose_name": "Отправленное уведомление об редактировании",
                "verbose_name_plural": "Отправленные уведомления об редактировании",
            },
        ),
        migrations.AddConstraint(
            model_name="editnotificationsent",
            constraint=models.UniqueConstraint(
                fields=("editor_id", "edit_date", "text_hash"),
                name="webhook_tg_edit_notification_uniq",
            ),
        ),
    ]
