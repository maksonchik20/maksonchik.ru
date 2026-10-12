from importlib import import_module
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .models import AdminChatFilter, FileType, Message, TelegramIncomingUpdate, UserTg


class ConcurrentAdminIndexOperationTests(SimpleTestCase):
    def test_postgres_uses_concurrent_create_and_drop_without_transactions(self):
        migration_module = import_module("webhook_tg.migrations.0046_admin_keyset_indexes")
        self.assertFalse(migration_module.Migration.atomic)
        from_state = MagicMock()
        to_state = MagicMock()
        schema_editor = MagicMock()
        schema_editor.connection.vendor = "postgresql"
        schema_editor.connection.alias = "default"
        schema_editor.connection.in_atomic_block = False
        for operation in migration_module.Migration.operations:
            with self.subTest(index=operation.index.name), patch.object(operation, "allow_migrate_model", return_value=True):
                operation.database_forwards("webhook_tg", schema_editor, from_state, to_state)
                schema_editor.add_index.assert_called_with(to_state.apps.get_model.return_value, operation.index, concurrently=True)
                operation.database_backwards("webhook_tg", schema_editor, from_state, to_state)
                schema_editor.remove_index.assert_called_with(from_state.apps.get_model.return_value, operation.index, concurrently=True)
                schema_editor.connection.in_atomic_block = True
                with self.assertRaises(RuntimeError):
                    operation.database_forwards("webhook_tg", schema_editor, from_state, to_state)
                with self.assertRaises(RuntimeError):
                    operation.database_backwards("webhook_tg", schema_editor, from_state, to_state)
                schema_editor.connection.in_atomic_block = False


class AdminIndexesSQLiteMigrationTests(TransactionTestCase):
    def test_migration_is_reversible_on_sqlite_and_nonunique(self):
        if connection.vendor != "sqlite":
            self.skipTest("Schema round-trip is intentionally limited to the isolated SQLite test database")
        indexToModel = {
            "wu_user_business_conn": UserTg,
            "wu_incoming_active_id": TelegramIncomingUpdate,
            "wu_incoming_priority_id": TelegramIncomingUpdate,
        }
        executor = MigrationExecutor(connection)
        final_targets = executor.loader.graph.leaf_nodes()
        try:
            executor.migrate([("webhook_tg", "0045_purchase_attribution")])
            with connection.cursor() as cursor:
                for name, model in indexToModel.items():
                    self.assertNotIn(name, connection.introspection.get_constraints(cursor, model._meta.db_table))
            MigrationExecutor(connection).migrate([("webhook_tg", "0046_admin_keyset_indexes")])
            with connection.cursor() as cursor:
                for name, model in indexToModel.items():
                    nameToConstraint = connection.introspection.get_constraints(cursor, model._meta.db_table)
                    self.assertIn(name, nameToConstraint)
                    self.assertFalse(nameToConstraint[name]["unique"])
            UserTg.objects.create(user_id=9001, chat_id=9001, business_connection_id="same-not-unique")
            UserTg.objects.create(user_id=9002, chat_id=9002, business_connection_id="same-not-unique")
        finally:
            MigrationExecutor(connection).migrate(final_targets)


class JournalKeysetPaginationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin_user = get_user_model().objects.create_superuser(
            username="keyset-admin", email="keyset@example.com", password="test-password",
        )
        cls.messages = Message.objects.bulk_create([
            Message(
                chat_id=4001, message_id=index + 1, business_connection_id="keyset-connection",
                username_from="keyset_user", text=f"archive-{index:03d}",
                file_type=FileType.PHOTO if index % 2 else FileType.UNKNOWN,
                payload="unused-payload" * 100,
            )
            for index in range(125)
        ])
        cls.incoming = TelegramIncomingUpdate.objects.bulk_create([
            TelegramIncomingUpdate(
                update_id=990000 + index, payload={"message": {"text": "unused-payload" * 100}},
                queue="priority" if index % 2 else "business",
                status="failed" if index % 3 == 0 else "done",
                next_attempt_at=timezone.now(), last_error="needle-error" if index % 3 == 0 else "",
            )
            for index in range(125)
        ])

    def setUp(self):
        self.client.force_login(self.admin_user)
        self.message_url = reverse("admin:webhook_tg_message_changelist")
        self.incoming_url = reverse("admin:webhook_tg_telegramincomingupdate_changelist")

    def assert_count_free(self, url, paramToValue=None):
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(url, paramToValue or {})
        self.assertEqual(response.status_code, 200)
        for fieldToQuery in queries.captured_queries:
            sql = fieldToQuery["sql"].upper()
            if "WEBHOOK_TG_MESSAGE" in sql or "WEBHOOK_TG_TELEGRAMINCOMINGUPDATE" in sql:
                self.assertNotIn("COUNT(", sql)
                self.assertNotIn("OFFSET", sql)
        return response

    def follow_cursor(self, base_url, cursor_url):
        self.assertIsNotNone(cursor_url)
        return self.assert_count_free(base_url + cursor_url)

    def test_message_list_uses_bounded_page_not_exact_count_or_show_all(self):
        response = self.assert_count_free(self.message_url, {"all": "1", "p": "99999", "_facets": "1"})
        changelist = response.context["cl"]
        self.assertEqual(len(changelist.result_list), 50)
        self.assertEqual(changelist.result_list[0].pk, self.messages[-1].pk)
        self.assertFalse(changelist.show_all)
        self.assertFalse(changelist.show_full_result_count)
        self.assertIn("payload", changelist.result_list[0].get_deferred_fields())
        self.assertContains(response, "Общее число записей не вычисляется")
        self.assertNotContains(response, "125 selected")

    def test_forward_and_backward_pages_keep_stable_order(self):
        first = self.assert_count_free(self.message_url).context["cl"]
        second = self.follow_cursor(self.message_url, first.cursor_older_url).context["cl"]
        third = self.follow_cursor(self.message_url, second.cursor_older_url).context["cl"]
        ids = [item.pk for page in (first, second, third) for item in page.result_list]
        self.assertEqual(ids, [item.pk for item in reversed(self.messages)])
        self.assertIsNone(third.cursor_older_url)
        back = self.follow_cursor(self.message_url, second.cursor_newer_url).context["cl"]
        self.assertEqual([item.pk for item in back.result_list], [item.pk for item in first.result_list])

    def test_new_inserts_do_not_duplicate_next_page(self):
        first = self.assert_count_free(self.message_url).context["cl"]
        Message.objects.create(chat_id=4001, message_id=999, username_from="new", text="new")
        second = self.follow_cursor(self.message_url, first.cursor_older_url).context["cl"]
        self.assertTrue({item.pk for item in first.result_list}.isdisjoint(item.pk for item in second.result_list))

    def test_search_filters_and_cursor_reset_work(self):
        response = self.assert_count_free(self.message_url, {
            "q": "archive", "file_type__exact": FileType.PHOTO, "business_connection_id": "keyset",
        })
        changelist = response.context["cl"]
        self.assertTrue(all(item.file_type == FileType.PHOTO for item in changelist.result_list))
        second = self.follow_cursor(self.message_url, changelist.cursor_older_url).context["cl"]
        self.assertEqual(len(second.result_list), 12)
        fieldToParams = parse_qs(urlsplit(second.get_query_string({"file_type__exact": FileType.UNKNOWN})).query)
        self.assertNotIn("before_id", fieldToParams)
        self.assertEqual(fieldToParams["q"], ["archive"])
        self.assertTrue(all(name not in ("before_id", "after_id") for name, _ in second.search_hidden_fields))
        business_filter = next(spec for spec in second.filter_specs if spec.parameter_name == "business_connection_id")
        self.assertNotIn("before_id", dict(next(business_filter.choices(second))["query_parts"]))

    def test_incoming_search_filter_and_payload_defer(self):
        response = self.assert_count_free(self.incoming_url, {"status__exact": "failed", "q": "needle-error"})
        rows = response.context["cl"].result_list
        self.assertEqual(len(rows), 42)
        self.assertTrue(all(row.status == "failed" for row in rows))
        self.assertTrue(all("payload" in row.get_deferred_fields() for row in rows))

    def test_chat_initial_page_has_only_its_ajax_slice_and_no_table_query(self):
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.message_url, {"chat_id": 4001})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["cl"].result_list, [])
        self.assertEqual(response.context["wu_chat_shown"], 100)
        archive_selects = [
            fieldToQuery["sql"] for fieldToQuery in queries.captured_queries
            if 'FROM "webhook_tg_message"' in fieldToQuery["sql"]
        ]
        self.assertEqual(len(archive_selects), 1)
        self.assertIn("LIMIT 101", archive_selects[0])
        self.assertNotIn("COUNT(", archive_selects[0].upper())

    def test_selected_delete_action_is_available_without_select_across(self):
        selected_pk = self.messages[0].pk
        response = self.client.post(self.message_url, {
            "action": "delete_selected", "_selected_action": str(selected_pk), "index": "0", "select_across": "0",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="post"')
        response = self.client.post(self.message_url, {
            "action": "delete_selected", "_selected_action": str(selected_pk), "post": "yes", "select_across": "0",
        })
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Message.objects.filter(pk=selected_pk).exists())
        self.assertTrue(Message.objects.filter(pk=self.messages[1].pk).exists())

    def test_select_across_is_rejected_instead_of_targeting_entire_journal(self):
        for flag in ("1", "true", "on"):
            with self.subTest(flag=flag):
                response = self.client.post(self.message_url, {
                    "action": "delete_selected", "_selected_action": str(self.messages[0].pk),
                    "post": "yes", "select_across": flag,
                })
                self.assertEqual(response.status_code, 302)
                self.assertTrue(Message.objects.filter(pk=self.messages[0].pk).exists())

    def test_malformed_cursor_is_rejected(self):
        for paramToValue in ({"before_id": "bad"}, {"after_id": "-1"}, {"before_id": "1", "after_id": "2"}):
            with self.subTest(params=paramToValue):
                response = self.client.get(self.incoming_url, paramToValue)
                self.assertEqual(response.status_code, 302)
                self.assertIn("?e=1", response.url)

    def test_staff_chat_scope_is_enforced_on_every_page(self):
        restricted = get_user_model().objects.create_user(username="scoped", password="test", is_staff=True)
        restricted.user_permissions.add(Permission.objects.get(codename="view_message", content_type__app_label="webhook_tg"))
        AdminChatFilter.objects.create(user=restricted, chat_id=4001)
        Message.objects.create(chat_id=9999, message_id=1, username_from="outsider", text="private-other-chat")
        self.client.force_login(restricted)
        first = self.assert_count_free(self.message_url).context["cl"]
        second = self.follow_cursor(self.message_url, first.cursor_older_url).context["cl"]
        self.assertTrue(all(item.chat_id == 4001 for page in (first, second) for item in page.result_list))
        response = self.client.get(self.message_url, {"chat_id": 9999})
        self.assertNotContains(response, "private-other-chat")
