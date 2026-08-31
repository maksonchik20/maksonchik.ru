from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("webhook_tg", "0032_usertg_bigint_telegram_ids")]

    operations = [
        migrations.RemoveConstraint(
            model_name="whoupdatemetrikaconversion",
            name="wu_metrika_funnel_event_unique",
        ),
        migrations.AddField(
            model_name="whoupdatemetrikaconversion",
            name="currency",
            field=models.CharField(
                blank=True,
                default="",
                max_length=3,
                verbose_name="Валюта",
            ),
        ),
        migrations.AddField(
            model_name="whoupdatemetrikaconversion",
            name="payment_order",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="metrika_conversion",
                to="webhook_tg.whoupdatepaymentorder",
                verbose_name="Оплата",
            ),
        ),
        migrations.AddField(
            model_name="whoupdatemetrikaconversion",
            name="value",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                max_digits=10,
                null=True,
                verbose_name="Ценность конверсии",
            ),
        ),
        migrations.AlterField(
            model_name="whoupdatemetrikaconversion",
            name="event_type",
            field=models.CharField(
                choices=[
                    ("start", "/start"),
                    ("connected", "Полное подключение"),
                    ("purchase", "Покупка подписки"),
                ],
                max_length=16,
            ),
        ),
        migrations.AddConstraint(
            model_name="whoupdatemetrikaconversion",
            constraint=models.UniqueConstraint(
                condition=models.Q(("payment_order__isnull", True)),
                fields=("funnel", "event_type"),
                name="wu_metrika_funnel_event_unique",
            ),
        ),
    ]
