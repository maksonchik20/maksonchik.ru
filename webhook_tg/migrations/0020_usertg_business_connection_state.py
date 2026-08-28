from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("webhook_tg", "0019_telegramincomingupdate"),
    ]

    operations = [
        migrations.AddField(
            model_name="usertg",
            name="business_connection_id",
            field=models.CharField(
                blank=True,
                max_length=255,
                null=True,
                verbose_name="Business connection id",
            ),
        ),
        migrations.AddField(
            model_name="usertg",
            name="business_is_connected",
            field=models.BooleanField(db_index=True, default=False, verbose_name="Бот подключён"),
        ),
        migrations.AddField(
            model_name="usertg",
            name="last_start_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Последний /start"),
        ),
        migrations.AddField(
            model_name="usertg",
            name="business_connected_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Последнее подключение"),
        ),
        migrations.AddField(
            model_name="usertg",
            name="business_disconnected_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Последнее отключение"),
        ),
        migrations.AddField(
            model_name="usertg",
            name="connection_reminder_at",
            field=models.DateTimeField(
                blank=True,
                db_index=True,
                null=True,
                verbose_name="Напомнить о подключении",
            ),
        ),
    ]
