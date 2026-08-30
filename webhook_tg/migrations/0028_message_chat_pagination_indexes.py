from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("webhook_tg", "0027_whoupdatemetrikaconversion")]

    operations = [
        migrations.AddIndex(
            model_name="message",
            index=models.Index(
                fields=["chat_id", "-id"],
                name="wu_msg_chat_id_desc",
            ),
        ),
        migrations.AddIndex(
            model_name="message",
            index=models.Index(
                fields=["chat_id", "business_connection_id", "-id"],
                name="wu_msg_chat_conn_id",
            ),
        ),
    ]
