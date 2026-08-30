from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("webhook_tg", "0029_message_history_username_index")]

    operations = [
        migrations.AddField(
            model_name="whoupdatemetrikaconversion",
            name="counter_id",
            field=models.PositiveBigIntegerField(
                db_index=True,
                default=111680333,
                verbose_name="Счётчик Метрики",
            ),
        ),
    ]
