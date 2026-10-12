from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("webhook_tg", "0039_wheel_spin_reset")]
    operations = [
        migrations.AddField(model_name="whoupdatewheelreward", name="granted_days", field=models.PositiveSmallIntegerField("Дней начислено без оплаты", default=0, db_default=0)),
        migrations.AddField(model_name="whoupdatewheelreward", name="access_expires_at_after", field=models.DateTimeField("Доступ после начисления", null=True, blank=True)),
    ]
