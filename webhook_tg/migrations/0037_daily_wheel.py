from zoneinfo import ZoneInfo
from django.db import migrations, models
import django.db.models.deletion


def backfill_days(apps, schema_editor):
    Reward = apps.get_model("webhook_tg", "WhoUpdateWheelReward")
    for reward in Reward.objects.using(schema_editor.connection.alias).all().iterator():
        Reward.objects.using(schema_editor.connection.alias).filter(pk=reward.pk).update(
            spin_day=reward.created_at.astimezone(ZoneInfo("Europe/Moscow")).date(),
        )


class Migration(migrations.Migration):
    dependencies = [("webhook_tg", "0036_wheel_pilot")]
    operations = [
        migrations.AddField(model_name="whoupdatewheelreward", name="spin_day", field=models.DateField(null=True)),
        migrations.RunPython(backfill_days, migrations.RunPython.noop),
        migrations.AlterField(model_name="whoupdatewheelreward", name="spin_day", field=models.DateField()),
        migrations.AlterField(model_name="whoupdatewheelreward", name="user", field=models.ForeignKey(
            on_delete=django.db.models.deletion.CASCADE, related_name="wheel_rewards", to="webhook_tg.usertg",
        )),
        migrations.AddConstraint(model_name="whoupdatewheelreward", constraint=models.UniqueConstraint(
            fields=("user", "spin_day"), name="wheel_one_spin_per_day",
        )),
    ]
