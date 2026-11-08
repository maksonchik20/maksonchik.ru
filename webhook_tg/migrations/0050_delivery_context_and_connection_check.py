import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("webhook_tg", "0049_first_observed_value")]

    operations = [
        migrations.AddField(
            model_name="telegramoutbox", name="delivery_context",
            field=models.JSONField(blank=True, db_default={}, default=dict),
        ),
        migrations.AlterField(
            model_name="telegramoutbox", name="method",
            field=models.CharField(choices=[
                ("sendMessage", "sendMessage"), ("sendPhoto", "sendPhoto"),
                ("sendAudio", "sendAudio"), ("sendVideo", "sendVideo"),
                ("sendVideoNoteWithText", "Кружок, затем текст"),
                ("sendMediaWithText", "Медиа, затем текст"),
                ("sendDocument", "sendDocument"), ("sendDocumentBytes", "sendDocumentBytes"),
                ("sendDocumentFile", "sendDocumentFile"), ("sendProtectedMedia", "Копия защищённого медиа"),
            ], max_length=32, verbose_name="Метод Telegram API"),
        ),
        migrations.CreateModel(
            name="WhoUpdateConnectionCheck",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("run_id", models.UUIDField(default=uuid.uuid4, unique=True)),
                ("start_update_id", models.BigIntegerField()),
                ("started_at", models.DateTimeField()),
                ("expires_at", models.DateTimeField()),
                ("peer_chat_id", models.BigIntegerField(blank=True, null=True)),
                ("peer_message_id", models.BigIntegerField(blank=True, null=True)),
                ("received_at", models.DateTimeField(blank=True, null=True)),
                ("edit_delivered_at", models.DateTimeField(blank=True, null=True)),
                ("delete_delivered_at", models.DateTimeField(blank=True, null=True)),
                ("user", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="connection_check", to="webhook_tg.usertg")),
            ],
        ),
    ]
