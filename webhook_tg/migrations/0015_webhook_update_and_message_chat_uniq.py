import ast

from django.db import migrations, models
from django.db.models import Count


def backfill_chat_id_from_payload(apps, schema_editor):
    Message = apps.get_model("webhook_tg", "Message")
    for message in Message.objects.filter(chat_id__isnull=True).iterator():
        if not message.payload:
            continue
        try:
            payload = ast.literal_eval(message.payload)
        except (SyntaxError, ValueError):
            continue
        chat_id = (payload.get("chat") or {}).get("id")
        if chat_id is not None:
            message.chat_id = chat_id
            message.save(update_fields=["chat_id"])


def remove_duplicate_chat_messages(apps, schema_editor):
    Message = apps.get_model("webhook_tg", "Message")
    duplicate_groups = (
        Message.objects.filter(chat_id__isnull=False)
        .values("chat_id", "message_id")
        .annotate(total=Count("id"))
        .filter(total__gt=1)
    )
    for group in duplicate_groups:
        rows = list(
            Message.objects.filter(
                chat_id=group["chat_id"],
                message_id=group["message_id"],
            ).order_by("-created_at", "-id")
        )
        for duplicate in rows[1:]:
            duplicate.delete()


def delete_messages_without_chat_id(apps, schema_editor):
    Message = apps.get_model("webhook_tg", "Message")
    Message.objects.filter(chat_id__isnull=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("webhook_tg", "0014_alter_message_text"),
    ]

    operations = [
        migrations.CreateModel(
            name="WebhookUpdate",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("update_id", models.BigIntegerField(db_index=True, unique=True, verbose_name="Telegram update_id")),
                ("processed_at", models.DateTimeField(auto_now_add=True, verbose_name="Обработано")),
            ],
            options={
                "verbose_name": "Обработанный webhook",
                "verbose_name_plural": "Обработанные webhook",
            },
        ),
        migrations.RunPython(backfill_chat_id_from_payload, migrations.RunPython.noop),
        migrations.RunPython(remove_duplicate_chat_messages, migrations.RunPython.noop),
        migrations.RunPython(delete_messages_without_chat_id, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="message",
            name="chat_id",
            field=models.BigIntegerField(verbose_name="Chat id"),
        ),
        migrations.AlterField(
            model_name="message",
            name="message_id",
            field=models.IntegerField(verbose_name="Message Id"),
        ),
        migrations.AddConstraint(
            model_name="message",
            constraint=models.UniqueConstraint(
                fields=("chat_id", "message_id"),
                name="webhook_tg_message_chat_message_id_uniq",
            ),
        ),
    ]
