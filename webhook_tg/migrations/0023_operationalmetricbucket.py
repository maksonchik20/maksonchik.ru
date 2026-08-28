from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("webhook_tg", "0022_who_update_access_referrals_payments")]

    operations = [
        migrations.CreateModel(
            name="OperationalMetricBucket",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("metric_name", models.CharField(db_index=True, max_length=128)),
                ("minute", models.DateTimeField(db_index=True)),
                ("labels_hash", models.CharField(max_length=64)),
                ("labels", models.JSONField(default=dict)),
                ("count", models.PositiveBigIntegerField(default=0)),
                ("total", models.FloatField(default=0)),
                ("maximum", models.FloatField(default=0)),
                ("exported_at", models.DateTimeField(blank=True, db_index=True, null=True)),
            ],
            options={"ordering": ("minute", "metric_name")},
        ),
        migrations.AddConstraint(
            model_name="operationalmetricbucket",
            constraint=models.UniqueConstraint(
                fields=("metric_name", "minute", "labels_hash"),
                name="unique_operational_metric_bucket",
            ),
        ),
    ]
