import builtins
import importlib.util
from datetime import timedelta
from unittest.mock import Mock, patch

from django.test import SimpleTestCase


class EventsChartImportTests(SimpleTestCase):
    def test_period_parser_does_not_import_plotting_libraries(self):
        original_import = builtins.__import__

        def lightweight_import(name, *args, **optionToValue):
            if name.split(".", 1)[0] in {"matplotlib", "numpy"}:
                raise AssertionError(f"Unexpected eager plotting import: {name}")
            return original_import(name, *args, **optionToValue)

        with patch("builtins.__import__", side_effect=lightweight_import):
            from . import events_chart
            spec = importlib.util.spec_from_file_location("webhook_tg._chart_import_probe", events_chart.__file__)
            probe = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(probe)
            self.assertEqual(probe.parse_events_period("3h"), timedelta(hours=3))
            self.assertIsNone(probe.parse_events_period("not-a-period"))

    def test_chart_still_renders_png(self):
        from .events_chart import build_outgoing_events_chart

        queryset = Mock()
        queryset.order_by.return_value = queryset
        queryset.filter.return_value = queryset
        queryset.count.return_value = 3
        with patch("webhook_tg.events_chart.BotOutgoingMessage.objects.filter", return_value=queryset):
            image, caption = build_outgoing_events_chart(timedelta(minutes=10))
        self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertIn("Всего: 3", caption)
