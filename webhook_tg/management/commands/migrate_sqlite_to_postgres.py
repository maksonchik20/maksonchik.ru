from __future__ import annotations

from collections import OrderedDict

from django.apps import apps
from django.core.management import BaseCommand, CommandError, call_command
from django.core.management.color import no_style
from django.db import connections, transaction


class Command(BaseCommand):
    help = "Переносит все данные из legacy SQLite в default PostgreSQL пакетами."

    def add_arguments(self, parser):
        parser.add_argument("--source", default="legacy")
        parser.add_argument("--target", default="default")
        parser.add_argument("--batch-size", type=int, default=1000)
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Обязательное подтверждение очистки таблиц целевой базы.",
        )

    def handle(self, *args, **options):
        source = options["source"]
        target = options["target"]
        batch_size = options["batch_size"]

        if not options["confirm"]:
            raise CommandError("Добавьте --confirm: целевые таблицы будут очищены.")
        if batch_size < 1:
            raise CommandError("--batch-size должен быть положительным.")
        if source not in connections:
            raise CommandError(f"Не настроена база-источник {source!r}.")

        source_connection = connections[source]
        target_connection = connections[target]
        if source_connection.vendor != "sqlite":
            raise CommandError("Источником должна быть SQLite.")
        if target_connection.vendor != "postgresql":
            raise CommandError("Целевой базой должен быть PostgreSQL.")

        call_command("migrate", database=target, interactive=False, verbosity=1)
        models_by_table = self._models_by_table()
        target_tables = set(target_connection.introspection.table_names())
        source_tables = set(source_connection.introspection.table_names())
        tables = [
            table
            for table in models_by_table
            if table in source_tables
            and table in target_tables
            and table != "django_migrations"
        ]
        if not tables:
            raise CommandError("Не найдено общих таблиц для переноса.")

        quoted_tables = ", ".join(
            target_connection.ops.quote_name(table) for table in tables
        )
        copied_models = []
        expected_counts = OrderedDict()

        self.stdout.write(f"Перенос {len(tables)} таблиц, batch_size={batch_size}")
        with transaction.atomic(using=target):
            with target_connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL DEFERRED")
                cursor.execute(f"TRUNCATE TABLE {quoted_tables} RESTART IDENTITY CASCADE")

            for table in tables:
                model = models_by_table[table]
                fields = [
                    field
                    for field in model._meta.concrete_fields
                    if field.column is not None
                ]
                field_names = [field.attname for field in fields]
                source_manager = model._base_manager.using(source)
                expected = source_manager.count()
                expected_counts[table] = expected

                copied = 0
                iterator = source_manager.values_list(*field_names).iterator(
                    chunk_size=batch_size
                )
                batch = []
                for values in iterator:
                    batch.append(model(**dict(zip(field_names, values))))
                    if len(batch) >= batch_size:
                        model._base_manager.using(target).bulk_create(
                            batch, batch_size=batch_size
                        )
                        copied += len(batch)
                        batch.clear()
                if batch:
                    model._base_manager.using(target).bulk_create(
                        batch, batch_size=batch_size
                    )
                    copied += len(batch)

                copied_models.append(model)
                self.stdout.write(f"{table}: {copied}/{expected}")

            sequence_sql = target_connection.ops.sequence_reset_sql(
                no_style(), copied_models
            )
            with target_connection.cursor() as cursor:
                for sql in sequence_sql:
                    cursor.execute(sql)

            target_connection.check_constraints(
                table_names=list(expected_counts.keys())
            )
            mismatches = []
            for table, expected in expected_counts.items():
                actual = models_by_table[table]._base_manager.using(target).count()
                if actual != expected:
                    mismatches.append(f"{table}: ожидалось {expected}, получено {actual}")
            if mismatches:
                raise CommandError("Ошибка проверки:\n" + "\n".join(mismatches))

        self.stdout.write(self.style.SUCCESS("Перенос и проверка количества строк завершены."))

    @staticmethod
    def _models_by_table():
        result = OrderedDict()
        for model in apps.get_models(include_auto_created=True):
            opts = model._meta
            if opts.proxy or not opts.managed:
                continue
            result.setdefault(opts.db_table, model)
        return result
