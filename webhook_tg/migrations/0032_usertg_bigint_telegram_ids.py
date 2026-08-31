from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("webhook_tg", "0031_telegramoutbox_delivery_status"),
    ]

    operations = [
        migrations.AlterField(
            model_name="usertg",
            name="user_id",
            field=models.BigIntegerField(verbose_name="User Id пользователя"),
        ),
        migrations.AlterField(
            model_name="usertg",
            name="chat_id",
            field=models.BigIntegerField(verbose_name="Chat Id пользователя с ботом"),
        ),
    ]
