import datetime
import uuid

from django.db import migrations, models
from django.utils import timezone
import django.db.models.deletion
import webhook_tg.models


OWNER_TELEGRAM_ID = 1394340082


def initialize_access_and_referral_codes(apps, schema_editor):
    UserTg = apps.get_model("webhook_tg", "UserTg")
    for bot_user in UserTg.objects.all().iterator():
        bot_user.referral_code = webhook_tg.models.generate_who_update_referral_code()
        if bot_user.user_id == OWNER_TELEGRAM_ID:
            now = timezone.now()
            bot_user.access_unlimited = False
            bot_user.trial_started_at = now
            bot_user.access_expires_at = now + datetime.timedelta(days=14)
        bot_user.save()


class Migration(migrations.Migration):
    dependencies = [("webhook_tg", "0021_usertg_connection_reminder_sent_at")]

    operations = [
        migrations.AddField(
            model_name="usertg",
            name="access_expired_notified_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Уведомление об окончании доступа"),
        ),
        migrations.AddField(
            model_name="usertg",
            name="access_expires_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True, verbose_name="Доступ до"),
        ),
        migrations.AddField(
            model_name="usertg",
            name="access_unlimited",
            field=models.BooleanField(db_index=True, default=True, verbose_name="Бессрочный доступ"),
        ),
        migrations.AddField(
            model_name="usertg",
            name="referral_bonus_days",
            field=models.PositiveIntegerField(default=0, verbose_name="Дней за рефералов"),
        ),
        migrations.AddField(
            model_name="usertg",
            name="referral_code",
            field=models.CharField(blank=True, editable=False, max_length=24, null=True, unique=True, verbose_name="Реферальный код"),
        ),
        migrations.AddField(
            model_name="usertg",
            name="referral_rewarded_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Реферальный бонус начислен"),
        ),
        migrations.AddField(
            model_name="usertg",
            name="referred_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="referrals", to="webhook_tg.usertg", verbose_name="Пригласил"),
        ),
        migrations.AddField(
            model_name="usertg",
            name="trial_started_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Начало пробного периода"),
        ),
        migrations.CreateModel(
            name="WhoUpdatePaymentOrder",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("public_id", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("plan", models.CharField(choices=[("month", "1 месяц"), ("three_months", "3 месяца"), ("year", "1 год")], max_length=24, verbose_name="Тариф")),
                ("duration_days", models.PositiveSmallIntegerField(verbose_name="Дней доступа")),
                ("amount", models.DecimalField(decimal_places=2, max_digits=8, verbose_name="Сумма")),
                ("status", models.CharField(choices=[("pending", "Ожидает оплаты"), ("paid", "Оплачен"), ("canceled", "Отменён"), ("failed", "Ошибка")], db_index=True, default="pending", max_length=16, verbose_name="Статус")),
                ("yookassa_payment_id", models.CharField(blank=True, db_index=True, default="", max_length=64, verbose_name="ID платежа ЮKassa")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("paid_at", models.DateTimeField(blank=True, null=True)),
                ("access_expires_at_after", models.DateTimeField(blank=True, null=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="payment_orders", to="webhook_tg.usertg", verbose_name="Пользователь")),
            ],
            options={
                "verbose_name": "Оплата WhoUpdate",
                "verbose_name_plural": "Оплаты WhoUpdate",
                "ordering": ("-created_at",),
            },
        ),
        migrations.RunPython(initialize_access_and_referral_codes, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="usertg",
            name="referral_code",
            field=models.CharField(default=webhook_tg.models.generate_who_update_referral_code, editable=False, max_length=24, unique=True, verbose_name="Реферальный код"),
        ),
    ]
