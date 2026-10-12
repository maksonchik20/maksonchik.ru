from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("webhook_tg", "0038_wheel_admin_names")]
    operations = [
        migrations.AddField(model_name="whoupdatewheelreward", name="spin_reset_at", field=models.DateTimeField(
            "Повторное вращение разрешено", null=True, blank=True,
        )),
        migrations.RemoveConstraint(model_name="whoupdatewheelreward", name="wheel_one_spin_per_day"),
        migrations.AddConstraint(model_name="whoupdatewheelreward", constraint=models.UniqueConstraint(
            fields=("user", "spin_day"), condition=models.Q(spin_reset_at__isnull=True),
            name="wheel_one_active_spin_per_day",
        )),
    ]
