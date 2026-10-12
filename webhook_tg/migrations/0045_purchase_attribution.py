from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("webhook_tg", "0044_whoupdatepaymentorder_checkout_at")]

    operations = [
        migrations.AddField(
            model_name="whoupdateonboardingfunnel",
            name="metrika_counter_id",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="whoupdatemetrikaconversion",
            name="client_id",
            field=models.CharField(blank=True, default="", db_default="", max_length=255),
        ),
        migrations.AddField(
            model_name="whoupdatemetrikaconversion",
            name="upload_history",
            field=models.JSONField(default=list, db_default=[], blank=True),
        ),
        migrations.AddField(
            model_name="whoupdatepaymentorder",
            name="metrika_funnel",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="checkout_orders",
                to="webhook_tg.whoupdateonboardingfunnel",
            ),
        ),
    ]
