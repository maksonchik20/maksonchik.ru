import json

from django.test import TestCase
from django.utils import timezone

from .models import UserTg, WhoUpdateOnboardingFunnel
from .onboarding_analytics import (
    record_connection,
    record_demo_opened,
    record_reminder_sent,
    register_telegram_start,
)


class WhoUpdateLandingAttributionTests(TestCase):
    def test_landing_keeps_direct_attribution_in_telegram_deep_link(self):
        response = self.client.get(
            "/",
            {
                "utm_source": "yandex",
                "utm_campaign": "713813024",
                "utm_content": "group_1_ad_2",
                "utm_term": "удаленные сообщения",
                "utm_device": "mobile",
                "utm_region": "213",
                "yclid": "test-yandex-click-id",
            },
            HTTP_HOST="who-update.ru",
            HTTP_USER_AGENT="Mozilla/5.0",
        )

        self.assertEqual(response.status_code, 200)
        funnel = WhoUpdateOnboardingFunnel.objects.get()
        self.assertEqual(funnel.utm_source, "yandex")
        self.assertEqual(funnel.utm_campaign, "713813024")
        self.assertEqual(funnel.yclid, "test-yandex-click-id")
        self.assertEqual(funnel.utm_device, "mobile")
        self.assertEqual(funnel.utm_region, "213")
        self.assertContains(
            response,
            f"https://t.me/who_update_bot?start=trk_{funnel.tracking_code}",
        )

    def test_metrika_client_id_is_attached_to_funnel(self):
        funnel = WhoUpdateOnboardingFunnel.objects.create(landing_viewed_at=timezone.now())

        response = self.client.post(
            "/bot/analytics/client-id/",
            data=json.dumps(
                {"tracking_code": funnel.tracking_code, "client_id": "1234567890"}
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        funnel.refresh_from_db()
        self.assertEqual(funnel.metrika_client_id, "1234567890")


class WhoUpdateConnectionFunnelTests(TestCase):
    def setUp(self):
        self.user = UserTg.objects.create(
            user_id=101,
            chat_id=101,
            username="funnel_user",
        )

    def _started_funnel(self, update_id):
        funnel = WhoUpdateOnboardingFunnel.objects.create(
            landing_viewed_at=timezone.now(),
            utm_source="yandex",
            yclid=f"yclid-{update_id}",
        )
        return register_telegram_start(
            self.user,
            f"trk_{funnel.tracking_code}",
            update_id=update_id,
            started_at=timezone.now(),
        )

    def test_connection_is_classified_as_immediate(self):
        funnel = self._started_funnel(1001)

        record_connection(self.user)

        funnel.refresh_from_db()
        self.assertEqual(
            funnel.connection_stage,
            WhoUpdateOnboardingFunnel.ConnectionStage.IMMEDIATE,
        )

    def test_demo_open_is_attached_to_current_funnel(self):
        funnel = self._started_funnel(1004)

        record_demo_opened(self.user)

        funnel.refresh_from_db()
        self.assertIsNotNone(funnel.demo_opened_at)

    def test_connection_after_first_reminder_is_classified(self):
        funnel = self._started_funnel(1002)
        record_reminder_sent(self.user, 1, funnel_pk=funnel.pk)

        record_connection(self.user)

        funnel.refresh_from_db()
        self.assertEqual(
            funnel.connection_stage,
            WhoUpdateOnboardingFunnel.ConnectionStage.AFTER_FIRST_REMINDER,
        )

    def test_connection_after_second_reminder_is_classified(self):
        funnel = self._started_funnel(1003)
        record_reminder_sent(self.user, 1, funnel_pk=funnel.pk)
        record_reminder_sent(self.user, 2, funnel_pk=funnel.pk)

        record_connection(self.user)

        funnel.refresh_from_db()
        self.assertEqual(
            funnel.connection_stage,
            WhoUpdateOnboardingFunnel.ConnectionStage.AFTER_SECOND_REMINDER,
        )
