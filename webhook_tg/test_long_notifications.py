import html
from html.parser import HTMLParser
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from .inner_models.BusinessConnection import BusinessConnection
from .models import Message, TelegramOutbox
from .outbox import deliver_outbox_item, enqueue_outbox
from .telegram_text import split_message_text, utf16_length
from .views import _send_edit_notification, build_message_update


class RenderedHTML(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.stack = []
        self.feed(source)
        self.close()
        assert not self.stack, "unclosed tag"

    def handle_starttag(self, tag, attrs):
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack.pop() == tag, "broken nesting"

    def handle_data(self, data):
        self.text.append(data)


def rendered(source):
    return "".join(RenderedHTML(source).text)


class SplitMessageTests(SimpleTestCase):
    def test_plain_text_limits_and_emoji_are_lossless(self):
        for source, count in (("x" * 4096, 1), ("x" * 4097, 2), ("😀" * 2048, 1),
                              ("😀" * 2049, 2), ("длинное слово\n" * 1000, None)):
            with self.subTest(length=len(source)):
                parts = split_message_text(source)
                self.assertEqual("".join(parts), source)
                self.assertTrue(all(0 < utf16_length(part) <= 4096 for part in parts))
                if count is not None:
                    self.assertEqual(len(parts), count)

    def test_nested_html_links_entities_and_quotes_survive_splitting(self):
        source = (
            '<b>Было:</b>\n<blockquote expandable><i>' + html.escape('😀 <текст> & "слово"\n' * 500)
            + '</i></blockquote>\n<b>Стало:</b>\n<blockquote><pre><code class="language-python">'
            + html.escape("print('<hello>')\n" * 300)
            + '</code></pre></blockquote>\n<a href="https://t.me/who_update_bot">@who_update_bot</a>'
        )
        parts = split_message_text(source, "HTML")
        self.assertGreater(len(parts), 2)
        self.assertEqual("".join(rendered(part) for part in parts), rendered(source))
        self.assertTrue(all(utf16_length(rendered(part)) <= 4000 for part in parts))
        self.assertIn('href="https://t.me/who_update_bot"', parts[-1])

    def test_html_length_counts_rendered_entities_and_keeps_short_payload_unchanged(self):
        for source in ('<b>' + '&amp;' * 2000 + '</b>', '<blockquote>' + 'я' * 4096 + '</blockquote>'):
            self.assertEqual(split_message_text(source, "HTML"), [source])

    def test_long_link_reopens_the_same_href_and_has_no_empty_parts(self):
        source = '<a href="https://example.com/?a=1&amp;b=2">' + '🙂' * 6000 + '</a>'
        parts = split_message_text(source, "HTML")
        self.assertEqual("".join(rendered(part) for part in parts), rendered(source))
        self.assertTrue(all(part.startswith('<a href="https://example.com/?a=1&amp;b=2">') for part in parts))
        self.assertTrue(all(rendered(part) for part in parts))

    def test_long_malformed_html_is_not_silently_truncated(self):
        with self.assertRaises(ValueError):
            split_message_text('<b>' + 'x' * 5000, "HTML")


class LongOutboxTests(TestCase):
    def make_item(self, **kwargs):
        payload = {"text": '<blockquote>' + 'я' * 7000 + '</blockquote>',
                   "parse_mode": "HTML", "disable_web_page_preview": True}
        payload.update(kwargs.pop("payload", {}))
        return enqueue_outbox(chat_id=123, method=kwargs.pop("method", "sendMessage"),
                              payload=payload, **kwargs)

    def ready(self, item):
        TelegramOutbox.objects.filter(pk=item.pk).update(next_attempt_at=timezone.now())

    @patch("webhook_tg.outbox.dispatch_telegram_request")
    def test_resume_from_failed_part_without_resending_successful_part(self, dispatch):
        keyboard = {"inline_keyboard": [[{"text": "Open", "url": "https://example.com"}]]}
        item = self.make_item(payload={"reply_markup": keyboard})
        original = item.payload["text"]
        dispatch.side_effect = [(True, ""), (False, "Temporary failure")]
        self.assertEqual(deliver_outbox_item(item.pk), "failed")
        self.assertEqual(dispatch.call_count, 2)
        first = dispatch.call_args_list[0].args[2]
        failed = dispatch.call_args_list[1].args[2]
        self.assertNotIn("reply_markup", first)
        self.assertEqual(failed["reply_markup"], keyboard)
        item.refresh_from_db()
        self.assertEqual(item.payload["_text_delivery"]["next"], 1)
        self.assertEqual(item.payload["text"], original)
        dispatch.reset_mock()
        dispatch.side_effect = None
        dispatch.return_value = (True, "")
        self.ready(item)
        self.assertEqual(deliver_outbox_item(item.pk), "sent")
        dispatch.assert_called_once_with("sendMessage", 123, failed)
        self.assertNotIn("_text_delivery", failed)
        self.assertTrue(utf16_length(rendered(failed["text"])) <= 4096)
        self.assertEqual(deliver_outbox_item(item.pk), "skipped")

    @patch("webhook_tg.outbox.dispatch_telegram_request")
    def test_media_is_not_repeated_when_second_text_part_fails(self, dispatch):
        item = self.make_item(method=TelegramOutbox.Method.SEND_MEDIA_WITH_TEXT,
                              payload={"media_kind": "video_note", "file_id": "circle-id"})
        dispatch.side_effect = [(True, ""), (True, ""), (False, "Temporary failure")]
        self.assertEqual(deliver_outbox_item(item.pk), "failed")
        item.refresh_from_db()
        self.assertTrue(item.payload["_media_sent"])
        self.assertEqual(item.payload["_text_delivery"]["next"], 1)
        dispatch.reset_mock()
        dispatch.side_effect = None
        dispatch.return_value = (True, "")
        self.ready(item)
        self.assertEqual(deliver_outbox_item(item.pk), "sent")
        self.assertEqual([call.args[0] for call in dispatch.call_args_list], ["sendMessage"])

    @patch("webhook_tg.outbox.dispatch_telegram_request", return_value=(True, ""))
    def test_short_message_has_one_unchanged_request(self, dispatch):
        item = self.make_item(payload={"text": "<b>короткий текст</b>"})
        self.assertEqual(deliver_outbox_item(item.pk), "sent")
        dispatch.assert_called_once_with("sendMessage", item.chat_id, item.payload)

    @patch("webhook_tg.outbox.dispatch_telegram_request", return_value=(False, "Bad Request: message is too long"))
    def test_unrecoverable_size_error_is_not_retried_forever(self, dispatch):
        item = self.make_item(payload={"text": "unsupported"})
        self.assertEqual(deliver_outbox_item(item.pk), "dropped")
        self.assertEqual(deliver_outbox_item(item.pk), "skipped")
        dispatch.assert_called_once()

    @patch("webhook_tg.outbox.dispatch_telegram_request", return_value=(True, ""))
    def test_already_completed_parts_are_not_resent_after_worker_restart(self, dispatch):
        item = self.make_item()
        self.assertEqual(deliver_outbox_item(item.pk), "sent")
        TelegramOutbox.objects.filter(pk=item.pk).update(status="pending", next_attempt_at=timezone.now())
        dispatch.reset_mock()
        self.assertEqual(deliver_outbox_item(item.pk), "sent")
        dispatch.assert_not_called()

    @patch("webhook_tg.outbox.dispatch_telegram_request", return_value=(True, ""))
    def test_real_edit_path_queues_before_sending_and_keeps_both_versions(self, dispatch):
        owner = BusinessConnection(user_chat_id=123, user_id=123)
        Message.objects.create(chat_id=456, message_id=789, text="старый текст " * 250)
        msg = {"chat": {"id": 456}, "message_id": 789, "edit_date": 1000,
               "from": {"id": 456, "first_name": "Sender"}, "text": "новый текст " * 260}
        expected = rendered(build_message_update(msg, owner))
        _send_edit_notification(msg, owner)
        item = TelegramOutbox.objects.get()
        self.assertEqual(item.status, "sent")
        self.assertEqual(item.chat_id, 123)
        sent = [call.args[2]["text"].split("\n\n", 1)[1] for call in dispatch.call_args_list]
        self.assertEqual("".join(rendered(part) for part in sent), expected)
        self.assertTrue(all(utf16_length(rendered(call.args[2]["text"])) <= 4096 for call in dispatch.call_args_list))
        dispatch.reset_mock()
        _send_edit_notification(msg, owner)
        dispatch.assert_not_called()
