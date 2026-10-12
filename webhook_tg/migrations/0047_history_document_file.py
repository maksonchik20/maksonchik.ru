from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("webhook_tg", "0046_admin_keyset_indexes")]

    operations = [
        migrations.AlterField(
            model_name="telegramoutbox",
            name="method",
            field=models.CharField(
                choices=[
                    ("sendMessage", "sendMessage"), ("sendPhoto", "sendPhoto"),
                    ("sendAudio", "sendAudio"), ("sendVideo", "sendVideo"),
                    ("sendVideoNoteWithText", "Кружок, затем текст"),
                    ("sendMediaWithText", "Медиа, затем текст"),
                    ("sendDocument", "sendDocument"), ("sendDocumentBytes", "sendDocumentBytes"),
                    ("sendDocumentFile", "sendDocumentFile"),
                ],
                max_length=32, verbose_name="Метод Telegram API",
            ),
        ),
    ]
