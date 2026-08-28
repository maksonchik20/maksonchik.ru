from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("webhook_tg", "0020_usertg_business_connection_state")]

    operations = [
        migrations.AddField(
            model_name="usertg",
            name="connection_reminder_sent_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                verbose_name="Напоминание о подключении отправлено",
            ),
        ),
    ]
