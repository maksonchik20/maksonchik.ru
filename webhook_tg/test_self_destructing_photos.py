import base64
import io
import struct
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .models import AdminChatFilter, FileType, Message
from .photo_copies import remember_photo_copy
from .telegram import send_photo_bytes
from .telegram_file_ids import is_self_destructing_photo, telegram_file_type
from .views import _extract_file_data, _send_file_by_type, _send_view_once_copy, create_message


def file_id(kind, marker=1, version=4):
    raw = struct.pack("<IIQQ", kind | (1 << 25), 2, marker, 12345)
    encoded = bytearray()
    index = 0
    while index < len(raw):
        if raw[index]:
            encoded.append(raw[index])
            index += 1
        else:
            end = index
            while end < len(raw) and not raw[end] and end - index < 255:
                end += 1
            encoded.extend((0, end - index))
            index = end
    encoded.extend((61, version) if version == 4 else (version,))
    return base64.urlsafe_b64encode(encoded).decode().rstrip("=")


def photo_message(message_id=1, *, kind=22):
    return {
        "chat": {"id": 1234, "type": "private"},
        "message_id": message_id,
        "business_connection_id": "photo-test-connection",
        "has_protected_content": True,
        "from": {"id": 5678, "first_name": "Photo test"},
        "photo": [{"file_id": file_id(kind, message_id), "width": 1280, "height": 960}],
    }


class FileIdTypeTests(SimpleTestCase):
    def test_remote_photo_type_and_flags(self):
        for version in (2, 4):
            self.assertEqual(telegram_file_type(file_id(22, version=version)), 22)
            self.assertTrue(is_self_destructing_photo(file_id(22, version=version)))
        self.assertFalse(is_self_destructing_photo(file_id(2)))
        self.assertFalse(is_self_destructing_photo(file_id(23)))

    def test_malformed_generated_and_unknown_versions_are_not_guessed(self):
        for value in (None, "", "not a file", "x" * 1025, "FgA", file_id(22, version=3),
                      file_id(22, version=99), "☃", "FgACAAAE"):
            self.assertIsNone(telegram_file_type(value))

    def test_protected_regular_photo_is_not_self_destructing(self):
        self.assertEqual(_extract_file_data(photo_message(kind=2))[1], FileType.PHOTO)
        self.assertEqual(_extract_file_data(photo_message())[1], FileType.SELF_DESTRUCT_PHOTO)


class PhotoCopyTests(TestCase):
    def setUp(self):
        self.reporter = patch("webhook_tg.views.report_who_update_event").start()
        self.addCleanup(patch.stopall)

    def test_each_photo_keeps_its_original_and_own_copy_id(self):
        for mid in (1, 2):
            create_message(photo_message(mid))
            remember_photo_copy(file_id(22, mid), {"photo": [{"file_id": file_id(2, mid)}]})
        create_message(photo_message(1))
        self.assertEqual(Message.objects.count(), 2)
        for mid in (1, 2):
            message = Message.objects.get(message_id=mid)
            self.assertEqual(message.file_id, file_id(22, mid))
            self.assertEqual(message.available_photo_copy_id, file_id(2, mid))
            self.assertEqual(message.file_type, FileType.SELF_DESTRUCT_PHOTO)

    def test_replacement_photo_does_not_reuse_old_copy(self):
        create_message(photo_message())
        remember_photo_copy(file_id(22), {"photo": [{"file_id": file_id(2)}]})
        replacement = photo_message()
        replacement["photo"][0]["file_id"] = file_id(22, 9)
        create_message(replacement)
        self.assertEqual(Message.objects.get().available_photo_copy_id, "")

    @patch("webhook_tg.telegram.log_bot_outgoing")
    @patch("webhook_tg.telegram._telegram_post")
    def test_successful_upload_stores_returned_copy_id(self, post, outgoing):
        create_message(photo_message())
        post.return_value.json.return_value = {
            "ok": True, "result": {"photo": [{"file_id": file_id(2)}]},
        }
        self.assertTrue(send_photo_bytes(1234, b"jpeg", source_file_id=file_id(22)))
        self.assertEqual(Message.objects.get().available_photo_copy_id, file_id(2))
        self.assertEqual(outgoing.call_args.kwargs["payload"]["source_file_id"], file_id(22))

    @patch("webhook_tg.views.send_photo", return_value=True)
    @patch("webhook_tg.views.download_telegram_file_bytes")
    def test_deleted_photo_reuses_saved_copy_without_downloading_expired_original(self, download, send):
        create_message(photo_message())
        remember_photo_copy(file_id(22), {"photo": [{"file_id": file_id(2)}]})
        _send_file_by_type(1234, file_id(22), FileType.SELF_DESTRUCT_PHOTO, "deleted")
        download.assert_not_called()
        send.assert_called_once_with(1234, file_id(2), caption="deleted")

    @patch("webhook_tg.views.send_photo_bytes", return_value=True)
    @patch("webhook_tg.views.download_telegram_file_bytes", return_value=(b"jpeg", "photo.jpg"))
    def test_new_copy_passes_original_id_to_upload(self, download, send):
        create_message(photo_message())
        self.assertTrue(_send_view_once_copy(1234, file_id(22), FileType.PHOTO, "saved"))
        self.assertEqual(send.call_args.kwargs["source_file_id"], file_id(22))

    def test_backfill_is_idempotent_preserves_ids_and_skips_regular_photos(self):
        for mid, kind in ((1, 22), (2, 2)):
            Message.objects.create(chat_id=1234, message_id=mid,
                                   file_id=file_id(kind, mid), file_type=FileType.PHOTO)
        call_command("classify_self_destructing_photos", dry_run=True, stdout=io.StringIO())
        self.assertEqual(Message.objects.filter(file_type=FileType.PHOTO).count(), 2)
        for _ in range(2):
            call_command("classify_self_destructing_photos", stdout=io.StringIO())
        self.assertEqual(Message.objects.get(message_id=1).file_type, FileType.SELF_DESTRUCT_PHOTO)
        self.assertEqual(Message.objects.get(message_id=1).file_id, file_id(22))
        self.assertEqual(Message.objects.get(message_id=2).file_type, FileType.PHOTO)


class PhotoAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser(username="photo-admin", password="test")
        cls.private = Message.objects.create(
            chat_id=1234, message_id=1, file_type=FileType.SELF_DESTRUCT_PHOTO,
            file_id=file_id(22), saved_photo_file_id=file_id(2),
            saved_photo_source_file_id=file_id(22),
        )
        cls.regular = Message.objects.create(chat_id=1234, message_id=2, text="ordinary photo",
                                           file_type=FileType.PHOTO, file_id=file_id(2, 2))

    def setUp(self):
        self.client.force_login(self.admin)
        self.list_url = reverse("admin:webhook_tg_message_changelist")
        self.preview_url = reverse("admin:webhook_tg_message_preview_file")

    def test_filter_works_in_list_chat_and_ajax_history(self):
        params = {"file_type__exact": FileType.SELF_DESTRUCT_PHOTO}
        response = self.client.get(self.list_url, params)
        self.assertEqual(list(response.context["cl"].result_list), [self.private])
        params["chat_id"] = 1234
        response = self.client.get(self.list_url, params)
        self.assertIn("file_type__exact=SELF_DESTRUCT_PHOTO", response.context["wu_chat_load_url"])
        self.assertNotIn("ordinary photo", str(response.context["wu_chat_html"]))
        response = self.client.get(reverse("admin:webhook_tg_message_chat_page"), params)
        self.assertEqual(response.json()["loaded"], 1)
        self.assertIn(file_id(22), response.json()["html"])
        self.assertIn(file_id(2), response.json()["html"])

    @patch("webhook_tg.admin.open_telegram_file_stream")
    @patch("webhook_tg.admin.get_telegram_file_path", return_value="photos/copy.jpg")
    def test_preview_uses_copy_id_and_checks_original_message_permissions(self, path, stream):
        upstream = Mock()
        upstream.iter_content.return_value = [b"jpeg"]
        stream.return_value = upstream
        response = self.client.get(self.preview_url, {"file_id": file_id(22)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"jpeg")
        path.assert_called_once_with(file_id(2))
        upstream.close.assert_called_once()
        self.assertEqual(self.client.get(self.preview_url, {"file_id": "not-allowed"}).status_code, 403)

    @patch("webhook_tg.admin.get_telegram_file_path")
    def test_user_cannot_access_another_chat_copy(self, path):
        user = get_user_model().objects.create_user(username="restricted", is_staff=True)
        user.user_permissions.add(Permission.objects.get(codename="view_message"))
        AdminChatFilter.objects.create(user=user, chat_id=9999)
        self.client.force_login(user)
        self.assertEqual(self.client.get(self.preview_url, {"file_id": file_id(22)}).status_code, 403)
        path.assert_not_called()

    @patch("webhook_tg.admin.get_telegram_file_path", side_effect=RuntimeError(
        "Bad Request: wrong file_id or the file is temporarily unavailable"))
    def test_missing_expired_copy_has_clear_message(self, path):
        Message.objects.filter(pk=self.private.pk).update(saved_photo_file_id="")
        response = self.client.get(self.preview_url, {"file_id": file_id(22)})
        self.assertEqual(response.status_code, 410)
        self.assertIn("Сохранённой копии нет", response.content.decode())
