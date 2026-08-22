from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Lead",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120, verbose_name="Имя")),
                ("contact", models.CharField(max_length=255, verbose_name="Телефон, email или Telegram")),
                ("message", models.TextField(blank=True, verbose_name="Задача")),
                ("page_url", models.URLField(blank=True, max_length=1000, verbose_name="Страница отправки")),
                ("page_title", models.CharField(blank=True, max_length=300, verbose_name="Название страницы")),
                ("utm_source", models.CharField(blank=True, max_length=255)),
                ("utm_medium", models.CharField(blank=True, max_length=255)),
                ("utm_campaign", models.CharField(blank=True, max_length=255)),
                ("utm_content", models.CharField(blank=True, max_length=255)),
                ("utm_term", models.CharField(blank=True, max_length=255)),
                ("notification_sent", models.BooleanField(default=False, verbose_name="Уведомление отправлено")),
                ("notification_error", models.TextField(blank=True, verbose_name="Ошибка уведомления")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="Создана")),
            ],
            options={
                "verbose_name": "Заявка с сайта",
                "verbose_name_plural": "Заявки с сайта",
                "ordering": ("-created_at",),
            },
        ),
    ]
