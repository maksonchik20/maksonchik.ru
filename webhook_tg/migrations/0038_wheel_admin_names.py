from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("webhook_tg", "0037_daily_wheel")]
    operations = [migrations.AlterModelOptions(
        name="whoupdatewheelreward",
        options={"verbose_name": "Выигрыш колеса", "verbose_name_plural": "Выигрыши колеса"},
    )]
