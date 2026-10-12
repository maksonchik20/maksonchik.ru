from django.db import migrations, models


class PortableConcurrentIndex(migrations.AddIndex):
    """Concurrent on production PostgreSQL; ordinary DDL for SQLite tests."""

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor != "postgresql":
            return super().database_forwards(app_label, schema_editor, from_state, to_state)
        if schema_editor.connection.in_atomic_block:
            raise RuntimeError("Concurrent indexes must run outside a transaction")
        model = to_state.apps.get_model(app_label, self.model_name)
        if self.allow_migrate_model(schema_editor.connection.alias, model):
            schema_editor.add_index(model, self.index, concurrently=True)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor != "postgresql":
            return super().database_backwards(app_label, schema_editor, from_state, to_state)
        if schema_editor.connection.in_atomic_block:
            raise RuntimeError("Concurrent indexes must run outside a transaction")
        model = from_state.apps.get_model(app_label, self.model_name)
        if self.allow_migrate_model(schema_editor.connection.alias, model):
            schema_editor.remove_index(model, self.index, concurrently=True)


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("webhook_tg", "0045_purchase_attribution")]

    operations = [
        PortableConcurrentIndex(
            model_name="usertg",
            index=models.Index(fields=["business_connection_id"], name="wu_user_business_conn"),
        ),
        PortableConcurrentIndex(
            model_name="telegramincomingupdate",
            index=models.Index(
                fields=["status", "-id"],
                condition=models.Q(status__in=["pending", "processing", "failed"]),
                name="wu_incoming_active_id",
            ),
        ),
        PortableConcurrentIndex(
            model_name="telegramincomingupdate",
            index=models.Index(
                fields=["queue", "-id"],
                condition=models.Q(queue="priority"),
                name="wu_incoming_priority_id",
            ),
        ),
    ]
