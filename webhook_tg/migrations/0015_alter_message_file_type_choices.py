from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("webhook_tg", "0014_bot_outgoing_message"),
    ]

    operations = [
        migrations.AlterField(
            model_name="message",
            name="file_type",
            field=models.CharField(
                blank=True,
                choices=[
                    ("UNKNOWN", "Неизвестно"),
                    ("PHOTO", "Фото"),
                    ("AUDIO", "Аудио"),
                    ("VIDEO", "Видео"),
                    ("DOCUMENT", "Документ"),
                    ("STICKER", "Стикер"),
                    ("ANIMATION", "GIF"),
                ],
                default="UNKNOWN",
                max_length=20,
                null=True,
                verbose_name="Тип файла",
            ),
        ),
    ]
