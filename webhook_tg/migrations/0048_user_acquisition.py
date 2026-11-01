import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("webhook_tg", "0047_history_document_file")]

    operations = [
        migrations.CreateModel(
            name="WhoUpdateUserAcquisition",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("first_start_at", models.DateTimeField(blank=True, null=True)),
                ("first_connected_at", models.DateTimeField(blank=True, null=True, help_text="Первое подключение, подтверждённое сохранённой историей; не время последнего переподключения.")),
                ("has_connection_history", models.BooleanField(db_default=False, default=False)),
                ("user", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="acquisition", to="webhook_tg.usertg")),
                ("first_start_funnel", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="first_start_acquisitions", to="webhook_tg.whoupdateonboardingfunnel")),
                ("acquisition_funnel", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="user_acquisitions", to="webhook_tg.whoupdateonboardingfunnel")),
                ("first_connection_funnel", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="first_connection_acquisitions", to="webhook_tg.whoupdateonboardingfunnel")),
            ],
        ),
        migrations.CreateModel(
            name="WhoUpdateTelegramStart",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("update_id", models.BigIntegerField(unique=True)),
                ("started_at", models.DateTimeField()),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="webhook_tg.usertg")),
                ("funnel", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="webhook_tg.whoupdateonboardingfunnel")),
            ],
        ),
    ]
