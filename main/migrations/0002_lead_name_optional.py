from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("main", "0001_initial")]

    operations = [
        migrations.AlterField(
            model_name="lead",
            name="name",
            field=models.CharField("Имя", max_length=120, blank=True),
        ),
    ]
