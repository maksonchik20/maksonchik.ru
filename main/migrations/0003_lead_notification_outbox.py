from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("main", "0002_lead_name_optional"),
        ("webhook_tg", "0031_telegramoutbox_delivery_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="lead",
            name="notification_outbox",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="lead",
                to="webhook_tg.telegramoutbox",
                verbose_name="Доставка уведомления",
            ),
        ),
    ]
