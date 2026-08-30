from django.db import migrations, models
import django.db.models.deletion
import webhook_tg.models


class Migration(migrations.Migration):
    dependencies = [("webhook_tg", "0025_botchatevent")]

    operations = [
        migrations.CreateModel(
            name="WhoUpdateOnboardingFunnel",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("tracking_code", models.CharField(default=webhook_tg.models.generate_who_update_tracking_code, editable=False, max_length=24, unique=True, verbose_name="Код сквозной аналитики")),
                ("landing_path", models.CharField(blank=True, default="", max_length=255)),
                ("utm_source", models.CharField(blank=True, db_index=True, default="", max_length=255)),
                ("utm_medium", models.CharField(blank=True, default="", max_length=255)),
                ("utm_campaign", models.CharField(blank=True, db_index=True, default="", max_length=255)),
                ("utm_content", models.CharField(blank=True, default="", max_length=255)),
                ("utm_term", models.CharField(blank=True, default="", max_length=255)),
                ("utm_device", models.CharField(blank=True, db_index=True, default="", max_length=64)),
                ("utm_region", models.CharField(blank=True, db_index=True, default="", max_length=64)),
                ("yclid", models.CharField(blank=True, db_index=True, default="", max_length=255)),
                ("metrika_client_id", models.CharField(blank=True, db_index=True, default="", max_length=255)),
                ("landing_viewed_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("telegram_started_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("start_update_id", models.BigIntegerField(blank=True, null=True, unique=True)),
                ("demo_opened_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("first_reminder_sent_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("second_reminder_sent_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("connected_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("connection_stage", models.CharField(blank=True, choices=[("immediate", "Сразу"), ("after_first", "После первого напоминания"), ("after_second", "После второго напоминания"), ("unknown", "Не удалось определить")], db_index=True, default="", max_length=24, verbose_name="Когда подключился")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="onboarding_funnels", to="webhook_tg.usertg", verbose_name="Пользователь")),
            ],
            options={
                "verbose_name": "Воронка подключения WhoUpdate",
                "verbose_name_plural": "Воронки подключения WhoUpdate",
                "ordering": ("-created_at",),
            },
        ),
        migrations.AddIndex(
            model_name="whoupdateonboardingfunnel",
            index=models.Index(fields=["user", "-telegram_started_at"], name="wu_funnel_user_started"),
        ),
    ]
