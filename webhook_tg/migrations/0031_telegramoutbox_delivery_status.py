from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("webhook_tg", "0030_whoupdatemetrikaconversion_counter_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="telegramoutbox",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Ожидает"),
                    ("sent", "Отправлено"),
                    ("dropped", "Не доставляется"),
                ],
                db_index=True,
                default="pending",
                max_length=16,
                verbose_name="Статус",
            ),
        ),
        migrations.AddField(
            model_name="telegramoutbox",
            name="sent_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                verbose_name="Отправлено",
            ),
        ),
        migrations.AlterField(
            model_name="telegramoutbox",
            name="method",
            field=models.CharField(
                choices=[
                    ("sendMessage", "sendMessage"),
                    ("sendPhoto", "sendPhoto"),
                    ("sendAudio", "sendAudio"),
                    ("sendVideo", "sendVideo"),
                    ("sendDocument", "sendDocument"),
                    ("sendDocumentBytes", "sendDocumentBytes"),
                ],
                max_length=32,
                verbose_name="Метод Telegram API",
            ),
        ),
        migrations.AddIndex(
            model_name="telegramoutbox",
            index=models.Index(
                fields=["status", "next_attempt_at"],
                name="wu_outbox_status_due",
            ),
        ),
    ]
