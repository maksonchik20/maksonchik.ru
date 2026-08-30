from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [("webhook_tg", "0026_whoupdateonboardingfunnel")]

    operations = [
        migrations.CreateModel(
            name="WhoUpdateMetrikaConversion",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "event_type",
                    models.CharField(
                        choices=[("start", "/start"), ("connected", "Полное подключение")],
                        max_length=16,
                    ),
                ),
                ("target", models.CharField(max_length=64)),
                ("occurred_at", models.DateTimeField(db_index=True)),
                (
                    "identifier_type",
                    models.CharField(
                        choices=[("yclid", "YCLID"), ("client_id", "ClientID")],
                        max_length=16,
                    ),
                ),
                ("identifier", models.CharField(max_length=255)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Ожидает отправки"),
                            ("submitted", "Принято Метрикой"),
                            ("processed", "Обработано Метрикой"),
                            ("failed", "Ошибка сопоставления"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                (
                    "next_attempt_at",
                    models.DateTimeField(db_index=True, default=django.utils.timezone.now),
                ),
                ("api_upload_id", models.BigIntegerField(blank=True, db_index=True, null=True)),
                ("api_status", models.CharField(blank=True, default="", max_length=32)),
                ("last_error", models.TextField(blank=True, default="")),
                ("submitted_at", models.DateTimeField(blank=True, null=True)),
                ("processed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "funnel",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="metrika_conversions",
                        to="webhook_tg.whoupdateonboardingfunnel",
                        verbose_name="Воронка",
                    ),
                ),
            ],
            options={
                "verbose_name": "Офлайн-конверсия WhoUpdate",
                "verbose_name_plural": "Офлайн-конверсии WhoUpdate",
                "ordering": ("-created_at",),
            },
        ),
        migrations.AddConstraint(
            model_name="whoupdatemetrikaconversion",
            constraint=models.UniqueConstraint(
                fields=("funnel", "event_type"),
                name="wu_metrika_funnel_event_unique",
            ),
        ),
        migrations.AddIndex(
            model_name="whoupdatemetrikaconversion",
            index=models.Index(
                fields=["status", "next_attempt_at"],
                name="wu_metrika_status_due",
            ),
        ),
    ]
