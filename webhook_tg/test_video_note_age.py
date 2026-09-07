import copy
import io
import struct
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from PIL import Image

from .background_tasks import claim_next_task, process_claimed_task
from .models import BackgroundTask, TelegramOutbox, UserTg
from .outbox import deliver_outbox_item
from .video_note_age import (
    MAX_VIDEO_BYTES,
    MP4_EPOCH,
    PILOT_USER_ID,
    _download_video,
    check_video_note_age,
    mp4_creation_time,
    photo_creation_time,
    schedule_video_note_age_check,
)
from .views import process_telegram_update


def box(kind, data, *, extended=False):
    if extended:
        return struct.pack(">I4sQ", 1, kind, len(data) + 16) + data
    return struct.pack(">I4s", len(data) + 8, kind) + data


def video_file(created_at, *, version=0, extended=False):
    width = 8 if version == 1 else 4
    raw = int((created_at - MP4_EPOCH).total_seconds())
    header = bytes([version, 0, 0, 0]) + raw.to_bytes(width, "big") * 2
    header += (1000).to_bytes(4, "big") + (10000).to_bytes(width, "big")
    header += bytes(80)
    return box(b"ftyp", b"isom" + bytes(4)) + box(
        b"moov", box(b"mvhd", header), extended=extended
    ) + box(b"mdat", b"unused media bytes")


def photo_file(date=None, offset=None, *, digitized=False):
    exif = Image.Exif()
    tags = {}
    if date is not None:
        tags[36868 if digitized else 36867] = date
    if offset is not None:
        tags[36882 if digitized else 36881] = offset
    if tags:
        exif[34665] = tags
    output = io.BytesIO()
    Image.new("RGB", (1, 1)).save(output, format="JPEG", exif=exif)
    return output.getvalue()


class VideoMetadataTests(SimpleTestCase):
    def test_photo_date_respects_explicit_timezone(self):
        expected = datetime(2026, 9, 7, 12, 0, tzinfo=dt_timezone.utc)
        for date, offset in (("2026:09:07 15:00:00", "+03:00"), ("2026:09:07 06:30:00", "-05:30")):
            for digitized in (False, True):
                self.assertEqual(photo_creation_time(photo_file(date, offset, digitized=digitized)), expected)

    def test_photo_without_metadata_or_timezone_is_not_guessed(self):
        for data in (
            b"not an image", photo_file(), photo_file("2026:09:07 15:00:00"),
            photo_file("2026:99:99 15:00:00", "+03:00"),
            photo_file("2026:09:07 15:00:00", "+99:00"),
            photo_file("2026:09:07 15:00:00", "+03:90"),
            photo_file("2026:09:07 15:00:00", "Moscow"),
        ):
            self.assertIsNone(photo_creation_time(data))

    def test_reads_32_and_64_bit_dates_and_large_box_headers(self):
        date = datetime(2026, 9, 7, 12, 30, tzinfo=dt_timezone.utc)
        for version in (0, 1):
            for extended in (False, True):
                with self.subTest(version=version, extended=extended):
                    self.assertEqual(
                        mp4_creation_time(video_file(date, version=version, extended=extended)),
                        date,
                    )

    def test_skips_missing_zero_ambiguous_and_malformed_dates(self):
        date = datetime(2026, 9, 7, tzinfo=dt_timezone.utc)
        good = video_file(date)
        for data in (
            b"", b"not a video", good[:-3], video_file(MP4_EPOCH),
            box(b"ftyp", b"isom") + box(b"moov", b""),
            box(b"mvhd", bytes(100)), good + good,
            struct.pack(">I4s", 2, b"ftyp"),
            struct.pack(">I4s", 1, b"moov"),
            struct.pack(">I4sQ", 1, b"moov", 2**64 - 1),
            good.replace(b"mvhd\x00", b"mvhd\x02", 1),
        ):
            with self.subTest(data=data[:20]):
                self.assertIsNone(mp4_creation_time(data))

    @patch("webhook_tg.video_note_age.open_telegram_file_stream")
    @patch("webhook_tg.video_note_age.get_telegram_file_path", return_value="video/test.mp4")
    def test_stream_is_bounded_and_always_closed(self, get_path, open_stream):
        response = Mock()
        response.iter_content.return_value = [b"x" * MAX_VIDEO_BYTES, b"x"]
        open_stream.return_value = response
        self.assertIsNone(_download_video("test-file"))
        response.close.assert_called_once()

    @patch("webhook_tg.video_note_age.get_telegram_file_path")
    def test_download_error_does_not_expose_credential_url(self, get_path):
        get_path.side_effect = RuntimeError("https://example/botSECRET/video.mp4")
        with self.assertRaisesRegex(RuntimeError, "^Video note download failed$"):
            _download_video("test-file")

    @patch("webhook_tg.video_note_age.time.monotonic", side_effect=[0, 31])
    @patch("webhook_tg.video_note_age.open_telegram_file_stream")
    @patch("webhook_tg.video_note_age.get_telegram_file_path", return_value="video/test.mp4")
    def test_slow_stream_is_closed_and_retried(self, get_path, open_stream, monotonic):
        response = Mock()
        response.iter_content.return_value = [b"x"]
        open_stream.return_value = response
        with self.assertRaisesRegex(RuntimeError, "Video note download failed"):
            _download_video("test-file")
        response.close.assert_called_once()


class VideoNoteAgePilotTests(TestCase):
    def setUp(self):
        self.now = timezone.now().replace(microsecond=0)
        self.owner = UserTg.objects.create(
            user_id=PILOT_USER_ID, chat_id=PILOT_USER_ID,
            business_connection_id="pilot-connection", business_is_connected=True,
            access_unlimited=False, access_expires_at=self.now + timedelta(days=1),
        )
        self.msg = {
            "business_connection_id": "pilot-connection",
            "chat": {"id": 902, "type": "private"},
            "from": {"id": 902, "first_name": "<Александр & друг>", "username": "alex"},
            "message_id": 77,
            "date": int(self.now.timestamp()),
            "video_note": {"file_id": "test-file", "file_unique_id": "test-unique", "file_size": 1000},
        }

    def test_only_owner_incoming_unmarked_video_notes_are_scheduled(self):
        UserTg.objects.create(
            user_id=903, chat_id=903, business_connection_id="other-connection",
            business_is_connected=True,
        )
        variants = []
        for changes in (
            {"business_connection_id": "other-connection"},
            {"business_connection_id": "unknown"},
            {"business_connection_id": None},
            {"from": {"id": PILOT_USER_ID}},
            {"from": {}},
            {"chat": {"id": -1001, "type": "supergroup"}},
            {"forward_origin": {"type": "hidden_user"}},
            {"forward_date": self.msg["date"] - 500},
            {"video_note": {"file_id": "large", "file_size": MAX_VIDEO_BYTES + 1}},
            {"video_note": None, "document": {"file_id": "pdf-file", "mime_type": "application/pdf"}},
        ):
            variants.append({**self.msg, **changes})
        for msg in variants:
            with self.subTest(fields=msg.keys()):
                self.assertIsNone(schedule_video_note_age_check(msg))
        self.assertFalse(BackgroundTask.objects.exists())
        first = schedule_video_note_age_check(self.msg)
        self.assertEqual(schedule_video_note_age_check(self.msg).pk, first.pk)
        self.assertEqual(BackgroundTask.objects.count(), 1)

    @patch("webhook_tg.views.create_message")
    @patch("webhook_tg.views.log_bot_incoming")
    @patch("webhook_tg.views.maybe_delete_muted_business_message", return_value=False)
    def test_real_update_handler_schedules_new_notes_but_not_edits(self, mute, log, create):
        process_telegram_update({"update_id": 100, "business_message": self.msg}, use_idempotency=False)
        edited = copy.deepcopy(self.msg)
        edited["message_id"] = 78
        process_telegram_update({"update_id": 101, "edited_business_message": edited}, use_idempotency=False)
        self.assertEqual(BackgroundTask.objects.count(), 1)

    @patch("webhook_tg.video_note_age._download_video")
    def test_old_note_queues_one_owner_warning_with_safe_identity(self, download):
        download.return_value = video_file(self.now - timedelta(minutes=45))
        task = schedule_video_note_age_check(self.msg)
        claimed = claim_next_task(claimed_by="test")
        self.assertEqual(claimed.pk, task.pk)
        self.assertTrue(process_claimed_task(claimed))
        check_video_note_age(task)
        download.assert_called_once()
        task.refresh_from_db()
        self.assertEqual(task.status, BackgroundTask.Status.COMPLETED)
        item = TelegramOutbox.objects.get()
        self.assertEqual(item.chat_id, PILOT_USER_ID)
        self.assertEqual(item.method, TelegramOutbox.Method.SEND_VIDEO_NOTE_WITH_TEXT)
        self.assertEqual(item.payload["video_note"], "test-file")
        text = item.payload["text"]
        self.assertIn("&lt;Александр &amp; друг&gt; (@alex)", text)
        self.assertIn("45 мин.", text)
        self.assertIn("МСК", text)
        self.assertIn("не доказательство пересылки", text)
        self.assertIn('<a href="https://t.me/who_update_bot">@who_update_bot</a>', text)

    @patch("webhook_tg.outbox.dispatch_telegram_request")
    @patch("webhook_tg.video_note_age._download_video")
    def test_video_is_sent_before_text_and_successful_video_is_not_retried(self, download, dispatch):
        download.return_value = video_file(self.now - timedelta(minutes=45))
        check_video_note_age(schedule_video_note_age_check(self.msg))
        item = TelegramOutbox.objects.get()
        # First attempt: failed video means no text is sent.
        dispatch.return_value = (False, "Temporary Telegram timeout")
        self.assertEqual(deliver_outbox_item(item.pk), "failed")
        self.assertEqual([c.args[0] for c in dispatch.call_args_list], ["sendVideoNote"])
        item.refresh_from_db()
        self.assertFalse(item.payload.get("_video_note_sent"))

        # Second attempt: video succeeds, text needs a retry.
        dispatch.reset_mock()
        dispatch.side_effect = [(True, ""), (False, "Temporary Telegram timeout")]
        TelegramOutbox.objects.filter(pk=item.pk).update(next_attempt_at=timezone.now())
        self.assertEqual(deliver_outbox_item(item.pk), "failed")
        self.assertEqual([c.args[0] for c in dispatch.call_args_list], ["sendVideoNote", "sendMessage"])
        self.assertTrue(all(c.args[1] == PILOT_USER_ID for c in dispatch.call_args_list))
        self.assertEqual(dispatch.call_args_list[0].args[2], {"video_note": "test-file"})
        item.refresh_from_db()
        self.assertTrue(item.payload["_video_note_sent"])

        # Third attempt resumes at the text, including after a worker restart.
        dispatch.reset_mock()
        dispatch.side_effect = None
        dispatch.return_value = (True, "")
        TelegramOutbox.objects.filter(pk=item.pk).update(next_attempt_at=timezone.now())
        self.assertEqual(deliver_outbox_item(item.pk), "sent")
        self.assertEqual([c.args[0] for c in dispatch.call_args_list], ["sendMessage"])
        self.assertNotIn("video_note", dispatch.call_args.args[2])
        self.assertNotIn("_video_note_sent", dispatch.call_args.args[2])
        self.assertEqual(deliver_outbox_item(item.pk), "skipped")
        dispatch.assert_called_once()

    @patch("webhook_tg.outbox.dispatch_telegram_request", return_value=(False, "Forbidden: bot was blocked by the user"))
    @patch("webhook_tg.video_note_age._download_video")
    def test_blocked_video_drops_pair_without_sending_text(self, download, dispatch):
        download.return_value = video_file(self.now - timedelta(minutes=45))
        check_video_note_age(schedule_video_note_age_check(self.msg))
        item = TelegramOutbox.objects.get()
        self.assertEqual(deliver_outbox_item(item.pk), "dropped")
        self.assertEqual(dispatch.call_args.args[0], "sendVideoNote")
        dispatch.assert_called_once()

    @patch("webhook_tg.outbox.dispatch_telegram_request", return_value=(True, ""))
    @patch("webhook_tg.video_note_age._download_video")
    def test_media_types_copy_original_file_before_warning(self, download, dispatch):
        created = self.now - timedelta(minutes=45)
        photo = photo_file(created.strftime("%Y:%m:%d %H:%M:%S"), "+00:00")
        video = video_file(created)
        cases = (
            ("photo", [{"file_id": "small", "width": 90, "height": 90}, {"file_id": "original", "width": 1000, "height": 1000}], photo, "sendPhoto", "фото"),
            ("video", {"file_id": "original", "mime_type": "video/mp4"}, video, "sendVideo", "видео"),
            ("animation", {"file_id": "original"}, video, "sendAnimation", "анимация"),
            ("audio", {"file_id": "original", "mime_type": "audio/mp4"}, video, "sendAudio", "аудио"),
            ("document", {"file_id": "original", "mime_type": "image/jpeg"}, photo, "sendDocument", "фото"),
            ("document", {"file_id": "original", "file_name": "MOVIE.MOV"}, video, "sendDocument", "видео"),
        )
        for index, (kind, media, data, send_method, label) in enumerate(cases):
            with self.subTest(kind=kind, method=send_method, label=label):
                msg = {**self.msg, "video_note": None, "message_id": 200 + index, kind: media}
                task = schedule_video_note_age_check(msg)
                self.assertIsNotNone(task)
                download.return_value = data
                check_video_note_age(task)
                item = TelegramOutbox.objects.get(idempotency_key=task.idempotency_key)
                self.assertEqual(item.method, TelegramOutbox.Method.SEND_MEDIA_WITH_TEXT)
                self.assertEqual(item.payload["file_id"], "original")
                self.assertIn(label, item.payload["text"])
                dispatch.reset_mock()
                self.assertEqual(deliver_outbox_item(item.pk), "sent")
                self.assertEqual([c.args[0] for c in dispatch.call_args_list], [send_method, "sendMessage"])
                self.assertEqual(dispatch.call_args_list[0].args[2], {kind: "original"})
                self.assertTrue(all(c.args[1] == PILOT_USER_ID for c in dispatch.call_args_list))

    @patch("webhook_tg.outbox.dispatch_telegram_request")
    @patch("webhook_tg.video_note_age._download_video")
    def test_photo_text_retry_does_not_duplicate_photo(self, download, dispatch):
        created = self.now - timedelta(minutes=45)
        download.return_value = photo_file(created.strftime("%Y:%m:%d %H:%M:%S"), "+00:00")
        msg = {**self.msg, "video_note": None, "photo": [{"file_id": "photo"}]}
        task = schedule_video_note_age_check(msg)
        check_video_note_age(task)
        item = TelegramOutbox.objects.get()
        dispatch.side_effect = [(True, ""), (False, "Temporary timeout")]
        self.assertEqual(deliver_outbox_item(item.pk), "failed")
        dispatch.reset_mock()
        dispatch.side_effect = None
        dispatch.return_value = (True, "")
        TelegramOutbox.objects.filter(pk=item.pk).update(next_attempt_at=timezone.now())
        self.assertEqual(deliver_outbox_item(item.pk), "sent")
        self.assertEqual([c.args[0] for c in dispatch.call_args_list], ["sendMessage"])

    @patch("webhook_tg.video_note_age._download_video", return_value=photo_file())
    def test_stripped_photo_is_silent_and_other_accounts_are_excluded(self, download):
        msg = {**self.msg, "video_note": None, "photo": [{"file_id": "photo"}]}
        check_video_note_age(schedule_video_note_age_check(msg))
        self.assertFalse(TelegramOutbox.objects.exists())
        for field, media in (("photo", [{"file_id": "photo"}]), ("video", {"file_id": "video"})):
            outsider = {**self.msg, "video_note": None, "business_connection_id": "other-connection", field: media}
            self.assertIsNone(schedule_video_note_age_check(outsider))

    @patch("webhook_tg.video_note_age._download_video")
    def test_threshold_uses_telegram_date_even_when_worker_is_delayed(self, download):
        self.msg["date"] -= 3600
        sent_at = self.now - timedelta(hours=1)
        task = schedule_video_note_age_check(self.msg)
        for age in (0, 90, 180, -300):
            download.return_value = video_file(sent_at - timedelta(seconds=age))
            check_video_note_age(task)
            self.assertFalse(TelegramOutbox.objects.exists())
        download.return_value = video_file(sent_at - timedelta(seconds=181))
        check_video_note_age(task)
        self.assertEqual(TelegramOutbox.objects.count(), 1)

    @patch("webhook_tg.video_note_age._download_video")
    def test_unavailable_and_implausible_metadata_are_silent(self, download):
        task = schedule_video_note_age_check(self.msg)
        for data in (None, b"broken", video_file(MP4_EPOCH), video_file(datetime(1970, 1, 1, tzinfo=dt_timezone.utc))):
            download.return_value = data
            check_video_note_age(task)
        self.assertFalse(TelegramOutbox.objects.exists())

    @patch("webhook_tg.video_note_age._download_video")
    def test_worker_rechecks_owner_connection_and_access_before_download(self, download):
        task = schedule_video_note_age_check(self.msg)
        for changes in (
            {"business_is_connected": False},
            {"business_connection_id": "new-connection"},
            {"access_expires_at": self.now - timedelta(seconds=1)},
            {"chat_id": 999},
            {"user_id": 999},
        ):
            original = {key: getattr(self.owner, key) for key in changes}
            UserTg.objects.filter(pk=self.owner.pk).update(**changes)
            check_video_note_age(task)
            UserTg.objects.filter(pk=self.owner.pk).update(**original)
        download.assert_not_called()
        self.assertFalse(TelegramOutbox.objects.exists())

    @patch("webhook_tg.video_note_age._download_video")
    def test_disconnect_during_download_does_not_queue_warning(self, download):
        def disconnect(file_id):
            UserTg.objects.filter(pk=self.owner.pk).update(business_is_connected=False)
            return video_file(self.now - timedelta(minutes=10))
        download.side_effect = disconnect
        task = schedule_video_note_age_check(self.msg)
        check_video_note_age(task)
        self.assertFalse(TelegramOutbox.objects.exists())

    @patch("webhook_tg.video_note_age._download_video", side_effect=RuntimeError("temporary download error"))
    def test_network_failure_uses_existing_retry_queue(self, download):
        task = schedule_video_note_age_check(self.msg)
        claimed = claim_next_task(claimed_by="test")
        self.assertFalse(process_claimed_task(claimed))
        task.refresh_from_db()
        self.assertEqual(task.status, BackgroundTask.Status.PENDING)
        self.assertEqual(task.attempts, 1)
        self.assertEqual(task.max_attempts, 3)
        self.assertFalse(TelegramOutbox.objects.exists())
