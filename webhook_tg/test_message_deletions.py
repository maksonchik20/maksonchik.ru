import io
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .message_deletions import mark_messages_deleted
from .models import AdminChatFilter, FileType, Message, TelegramIncomingUpdate
from .views import create_message, process_telegram_update


def deletion(ids, *, chat_id=1234, connection="deletion-test"):
    return {"chat": {"id": chat_id}, "business_connection_id": connection, "message_ids": ids}


class MessageDeletionTests(TestCase):
    def setUp(self):
        self.photo = Message.objects.create(
            chat_id=1234, message_id=1, business_connection_id="deletion-test",
            file_id="original-photo", file_type=FileType.PHOTO, caption="photo caption",
            saved_photo_file_id="copied-photo", saved_photo_source_file_id="original-photo",
            payload="original payload",
        )
        self.text = Message.objects.create(chat_id=1234, message_id=2,
                                           business_connection_id="deletion-test", text="archived text")
        self.other_chat = Message.objects.create(chat_id=9999, message_id=1,
                                                 business_connection_id="deletion-test")
        self.other_connection = Message.objects.create(chat_id=1234, message_id=3,
                                                       business_connection_id="another-connection")

    def test_marks_text_and_photo_without_touching_content_or_other_contexts(self):
        received = timezone.now()
        self.assertEqual(mark_messages_deleted(deletion([1, 2, 3, 100]), received_at=received), 2)
        self.photo.refresh_from_db()
        self.assertTrue(self.photo.is_deleted)
        self.assertEqual(self.photo.deleted_at, received)
        self.assertEqual(self.photo.file_id, "original-photo")
        self.assertEqual(self.photo.available_photo_copy_id, "copied-photo")
        self.assertEqual(self.photo.caption, "photo caption")
        self.assertEqual(self.photo.payload, "original payload")
        self.text.refresh_from_db()
        self.assertTrue(self.text.is_deleted)
        self.assertEqual(self.text.text, "archived text")
        self.assertFalse(Message.objects.get(pk=self.other_chat.pk).is_deleted)
        self.assertFalse(Message.objects.get(pk=self.other_connection.pk).is_deleted)
        self.assertEqual(Message.objects.count(), 4)

    def test_duplicate_event_keeps_first_receipt_and_older_backfill_can_correct_it(self):
        first = timezone.now()
        self.assertEqual(mark_messages_deleted(deletion([1, 1]), received_at=first), 1)
        self.assertEqual(mark_messages_deleted(deletion([1]), received_at=first + timedelta(hours=1)), 0)
        earlier = first - timedelta(days=1)
        self.assertEqual(mark_messages_deleted(deletion([1]), received_at=earlier), 1)
        self.photo.refresh_from_db()
        self.assertEqual(self.photo.deleted_at, earlier)

    def test_missing_context_and_malformed_ids_do_not_mark_unrelated_messages(self):
        for event in (None, {}, deletion([True, "1", 0, -1]), deletion([1], connection=""),
                      deletion([1], chat_id="1234"), {**deletion([1]), "chat": []},
                      {**deletion([1]), "message_ids": "1"}):
            self.assertEqual(mark_messages_deleted(event), 0)
        self.assertFalse(Message.objects.filter(is_deleted=True).exists())

    @patch("webhook_tg.views.business_access_allowed", return_value=False)
    @patch("webhook_tg.views.get_business_connection")
    def test_expired_access_does_not_prevent_recording_a_known_deletion(self, connection, access):
        event = TelegramIncomingUpdate.objects.create(
            update_id=123, payload={"deleted_business_messages": deletion([1])},
            queue="business", status="processing", next_attempt_at=timezone.now(),
        )
        received = timezone.now() - timedelta(minutes=5)
        TelegramIncomingUpdate.objects.filter(pk=event.pk).update(created_at=received)
        process_telegram_update({"update_id": 123, **event.payload}, use_idempotency=False)
        self.photo.refresh_from_db()
        self.assertTrue(self.photo.is_deleted)
        self.assertEqual(self.photo.deleted_at, received)
        connection.assert_not_called()

    @patch("webhook_tg.views.business_access_allowed", return_value=True)
    @patch("webhook_tg.views.get_business_connection", side_effect=RuntimeError("Telegram timeout"))
    def test_notification_lookup_failure_does_not_lose_the_deletion(self, connection, access):
        with self.assertRaises(RuntimeError):
            process_telegram_update({"update_id": 456, "deleted_business_messages": deletion([1])},
                                    use_idempotency=False)
        self.photo.refresh_from_db()
        self.assertTrue(self.photo.is_deleted)

    @patch("webhook_tg.views.report_who_update_event")
    def test_replayed_message_does_not_clear_its_deletion_flag(self, report):
        mark_messages_deleted(deletion([1]))
        self.photo.refresh_from_db()
        first = self.photo.deleted_at
        create_message({"chat": {"id": 1234}, "message_id": 1, "business_connection_id": "deletion-test",
                        "photo": [{"file_id": "original-photo"}], "caption": "photo caption"})
        self.photo.refresh_from_db()
        self.assertTrue(self.photo.is_deleted)
        self.assertEqual(self.photo.deleted_at, first)

    def test_large_deletion_marks_all_messages_not_just_notification_limit(self):
        Message.objects.bulk_create([
            Message(chat_id=4321, message_id=mid, business_connection_id="deletion-test")
            for mid in range(1, 751)
        ])
        self.assertEqual(mark_messages_deleted(deletion(list(range(1, 751)), chat_id=4321)), 750)

    @patch("webhook_tg.telegram.dispatch_telegram_request")
    def test_backfill_uses_old_events_is_idempotent_and_sends_nothing(self, send):
        event = TelegramIncomingUpdate.objects.create(
            update_id=999, payload={"deleted_business_messages": deletion([1, 2])},
            queue="business", status="done", next_attempt_at=timezone.now(),
        )
        received = timezone.now() - timedelta(days=5)
        TelegramIncomingUpdate.objects.filter(pk=event.pk).update(created_at=received)
        for _ in range(2):
            call_command("backfill_message_deletions", stdout=io.StringIO())
        self.photo.refresh_from_db()
        self.assertTrue(self.photo.is_deleted)
        self.assertEqual(self.photo.deleted_at, received)
        self.assertEqual(Message.objects.filter(is_deleted=True).count(), 2)
        send.assert_not_called()


class DeletedMessageAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser(username="deleted-admin", password="test")
        cls.deleted_photo = Message.objects.create(
            chat_id=1111, message_id=1, file_type=FileType.PHOTO, file_id="deleted-photo",
            is_deleted=True, deleted_at=timezone.now(),
        )
        cls.available_photo = Message.objects.create(chat_id=1111, message_id=2, file_type=FileType.PHOTO,
                                                     file_id="regular-photo", text="not deleted photo")
        cls.deleted_text = Message.objects.create(chat_id=1111, message_id=3, text="deleted text",
                                                  is_deleted=True, deleted_at=timezone.now())

    def setUp(self):
        self.client.force_login(self.admin)
        self.url = reverse("admin:webhook_tg_message_changelist")

    def test_yes_no_and_file_type_filters_combine(self):
        response = self.client.get(self.url, {"is_deleted__exact": "1", "file_type__exact": "PHOTO"})
        self.assertEqual(list(response.context["cl"].result_list), [self.deleted_photo])
        response = self.client.get(self.url, {"is_deleted__exact": "0"})
        self.assertEqual(list(response.context["cl"].result_list), [self.available_photo])
        response = self.client.get(self.url)
        self.assertIn("is_deleted", response.context["cl"].list_display)
        self.assertIn("deleted_at", response.context["cl"].list_display)

    def test_photo_and_text_have_visible_deleted_badge_and_photo_still_has_preview(self):
        response = self.client.get(self.url, {"chat_id": 1111, "is_deleted__exact": "1"})
        content = str(response.context["wu_chat_html"])
        self.assertEqual(content.count('class="wu-deleted-badge"'), 2)
        self.assertIn("Удалено</span>", content)
        self.assertIn("Удаление зафиксировано:", content)
        self.assertIn('data-file-id="deleted-photo"', content)
        self.assertNotIn("not deleted photo", content)
        self.assertIn("is_deleted__exact=1", response.context["wu_chat_load_url"])

    def test_deleted_filter_survives_loading_older_chat_pages(self):
        for mid in range(10, 115):
            Message.objects.create(chat_id=2222, message_id=mid, file_type=FileType.PHOTO,
                                   file_id=f"deleted-{mid}", is_deleted=True)
        Message.objects.create(chat_id=2222, message_id=999, text="must be excluded")
        params = {"chat_id": 2222, "is_deleted__exact": "1", "file_type__exact": "PHOTO"}
        first = self.client.get(self.url, params)
        self.assertEqual(first.context["wu_chat_shown"], 100)
        self.assertIn("is_deleted__exact=1", first.context["wu_chat_load_url"])
        page = self.client.get(reverse("admin:webhook_tg_message_chat_page"),
                               {**params, "before_id": first.context["wu_chat_next_before"]}).json()
        self.assertEqual(page["loaded"], 5)
        self.assertEqual(page["html"].count('class="wu-deleted-badge"'), 5)
        self.assertNotIn("must be excluded", page["html"])

    def test_deletion_fields_are_readonly_and_filter_does_not_bypass_chat_access(self):
        response = self.client.get(reverse("admin:webhook_tg_message_change", args=[self.deleted_photo.pk]))
        fields = response.context["adminform"].form.fields
        self.assertNotIn("is_deleted", fields)
        self.assertNotIn("deleted_at", fields)
        user = get_user_model().objects.create_user(username="deleted-restricted", is_staff=True)
        user.user_permissions.add(Permission.objects.get(codename="view_message"))
        AdminChatFilter.objects.create(user=user, chat_id=9999)
        self.client.force_login(user)
        response = self.client.get(self.url, {"is_deleted__exact": "1"})
        self.assertEqual(list(response.context["cl"].result_list), [])
        response = self.client.get(reverse("admin:webhook_tg_message_chat_page"),
                                   {"chat_id": 1111, "is_deleted__exact": "1"})
        self.assertEqual(response.json()["loaded"], 0)

    @patch("webhook_tg.admin.get_telegram_file_path", side_effect=RuntimeError("file unavailable"))
    def test_download_error_does_not_mark_a_photo_as_deleted(self, get_file):
        response = self.client.get(reverse("admin:webhook_tg_message_preview_file"),
                                   {"file_id": self.available_photo.file_id})
        self.assertEqual(response.status_code, 502)
        self.available_photo.refresh_from_db()
        self.assertFalse(self.available_photo.is_deleted)
