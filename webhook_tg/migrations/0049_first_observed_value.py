import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("webhook_tg", "0048_user_acquisition")]

    operations = [
        migrations.CreateModel(
            name="WhoUpdateFirstObservedValue",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_type", models.CharField(choices=[("edit", "Доставлено уведомление о правке"), ("delete_text", "Доставлена текстовая копия удаления")], max_length=16)),
                ("observed_at", models.DateTimeField(db_index=True)),
                ("outbox", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to="webhook_tg.telegramoutbox")),
                ("user", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="first_observed_value", to="webhook_tg.usertg")),
            ],
        ),
    ]
