from datetime import datetime, time, timedelta
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from .models import UserTg, WhoUpdateOnboardingFunnel
from .user_metrics import DailyUserMetrics, collect_daily_user_metrics, daily_user_metrics_text


class DailyUserMetricsTests(TestCase):
    def setUp(self):
        self.day = timezone.localdate()
        self.now = timezone.make_aware(
            datetime.combine(self.day, time(hour=12)),
            timezone.get_current_timezone(),
        )
        self.first = UserTg.objects.create(
            user_id=101,
            chat_id=101,
            access_unlimited=False,
            access_expires_at=self.now - timedelta(minutes=10),
            business_disconnected_at=self.now - timedelta(minutes=5),
        )
        self.second = UserTg.objects.create(
            user_id=202,
            chat_id=202,
            access_unlimited=False,
            access_expires_at=self.now + timedelta(hours=1),
        )

    def test_collects_unique_people_for_moscow_day(self):
        event_at = self.now - timedelta(minutes=1)
        WhoUpdateOnboardingFunnel.objects.create(
            user=self.first,
            landing_viewed_at=event_at,
            metrika_client_id="client-one",
            telegram_started_at=event_at,
            connected_at=event_at,
        )
        WhoUpdateOnboardingFunnel.objects.create(
            user=self.first,
            landing_viewed_at=event_at,
            metrika_client_id="client-one",
            telegram_started_at=event_at,
        )
        WhoUpdateOnboardingFunnel.objects.create(
            user=self.second,
            landing_viewed_at=event_at,
            metrika_client_id="client-two",
            telegram_started_at=event_at,
        )
        WhoUpdateOnboardingFunnel.objects.create(
            landing_viewed_at=event_at,
            metrika_client_id="",
        )

        metrics = collect_daily_user_metrics(day=self.day, at=self.now)

        self.assertEqual(metrics.unique_landing_visitors, 2)
        self.assertEqual(metrics.landing_views, 4)
        self.assertEqual(metrics.started_users, 2)
        self.assertEqual(metrics.connected_users, 1)
        self.assertEqual(metrics.disconnected_users, 1)
        self.assertEqual(metrics.expired_users, 1)

    def test_report_text_contains_all_requested_metrics(self):
        text = daily_user_metrics_text(
            DailyUserMetrics(
                day=self.day,
                unique_landing_visitors=10,
                landing_views=15,
                started_users=7,
                connected_users=4,
                disconnected_users=2,
                expired_users=3,
            )
        )

        self.assertIn("Уникальные посетители: <b>10</b>", text)
        self.assertIn("Написали /start: <b>7</b>", text)
        self.assertIn("Подключились: <b>4</b>", text)
        self.assertIn("Отключились: <b>2</b>", text)
        self.assertIn("Доступ закончился: <b>3</b>", text)

    @patch("webhook_tg.management.commands.send_daily_user_metrics.enqueue_outbox")
    @patch("webhook_tg.management.commands.send_daily_user_metrics.collect_daily_user_metrics")
    def test_command_queues_one_idempotent_report(self, collect_mock, enqueue_mock):
        collect_mock.return_value = DailyUserMetrics(
            day=self.day,
            unique_landing_visitors=10,
            landing_views=15,
            started_users=7,
            connected_users=4,
            disconnected_users=2,
            expired_users=3,
        )

        call_command("send_daily_user_metrics", "--date", self.day.isoformat())

        kwargs = enqueue_mock.call_args.kwargs
        self.assertEqual(kwargs["idempotency_key"], f"daily-user-metrics:{self.day}")
        self.assertEqual(kwargs["payload"]["parse_mode"], "HTML")

    @patch("webhook_tg.management.commands.send_daily_user_metrics.enqueue_outbox")
    @patch("webhook_tg.management.commands.send_daily_user_metrics.collect_daily_user_metrics")
    def test_preview_does_not_use_daily_key(self, collect_mock, enqueue_mock):
        collect_mock.return_value = DailyUserMetrics(
            day=self.day,
            unique_landing_visitors=10,
            landing_views=15,
            started_users=7,
            connected_users=4,
            disconnected_users=2,
            expired_users=3,
        )

        call_command(
            "send_daily_user_metrics",
            "--date",
            self.day.isoformat(),
            "--preview",
        )

        key = enqueue_mock.call_args.kwargs["idempotency_key"]
        self.assertRegex(key, r"^preview-user-metrics:[0-9a-f-]{36}$")
