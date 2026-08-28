from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("webhook_tg", "0015_alter_message_file_type_choices"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="message",
            index=models.Index(fields=["-created_at"], name="wu_msg_created_desc"),
        ),
        migrations.AddIndex(
            model_name="message",
            index=models.Index(fields=["chat_id", "-created_at"], name="wu_msg_chat_created"),
        ),
        migrations.AddIndex(
            model_name="message",
            index=models.Index(fields=["file_id"], name="wu_msg_file_id"),
        ),
    ]
