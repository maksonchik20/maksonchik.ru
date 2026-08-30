from django.db import migrations, models
from django.db.models.functions import Lower


class Migration(migrations.Migration):
    dependencies = [("webhook_tg", "0028_message_chat_pagination_indexes")]

    operations = [
        migrations.AddIndex(
            model_name="message",
            index=models.Index(
                models.F("business_connection_id"),
                Lower("username_from"),
                models.F("created_at"),
                models.F("message_id"),
                name="wu_msg_history_user",
            ),
        ),
    ]
