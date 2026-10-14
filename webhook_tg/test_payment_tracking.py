from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from .models import (
    UserTg,
    WhoUpdateMetrikaConversion,
    WhoUpdateOnboardingFunnel,
    WhoUpdatePaymentOrder,
    WhoUpdateWheelReward,
)
from .payment_views import payment_checkout
from .subscriptions import checkout_token
from .wheel import spin_day
from .wheel_checkout import checkout
from .user_metrics import collect_daily_user_metrics


@override_settings(ALLOWED_HOSTS=["who-update.ru"], WHO_UPDATE_METRIKA_COUNTER_ID=112093587)
class PaymentTrackingTests(TestCase):
    confirmation_url = "https://yookassa.test/confirmation"

    def setUp(self):
        self.user = UserTg.objects.create(
            user_id=8472201,
            chat_id=8472201,
            access_unlimited=False,
        )

    def subscribe(self):
        return self.client.get(
            f"/bot/subscribe/month/{checkout_token(self.user, 'month')}/",
            HTTP_HOST="who-update.ru",
        )

    def reward(self):
        return WhoUpdateWheelReward.objects.create(
            user=self.user,
            prize="discount10",
            spin_day=spin_day(),
            expires_at=timezone.now() + timedelta(hours=24),
        )

    @patch("webhook_tg.payment_views.create_payment")
    def test_regular_checkout_captures_visit_without_counting_payment(self, create):
        create.return_value = {"id": "payment-test", "confirmation_url": self.confirmation_url}

        response = self.subscribe()

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "webhook_tg/payment_checkout.html")
        self.assertContains(response, self.confirmation_url)
        self.assertContains(response, "getClientID")
        self.assertContains(response, "continueToPayment, 1200")
        self.assertContains(response, "<noscript>")
        self.assertNotContains(response, "who_update_purchase")
        self.assertNotContains(response, "webvisor: true")
        self.assertContains(response, "defer: true")
        self.assertContains(response, "webvisor: false")
        self.assertContains(response, 'window.location.origin + "/bot/checkout/"')
        self.assertNotContains(response, checkout_token(self.user, "month"))
        self.assertEqual(response["Cache-Control"], "no-store")
        order = WhoUpdatePaymentOrder.objects.get()
        self.assertEqual(order.status, WhoUpdatePaymentOrder.Status.PENDING)
        self.assertEqual(order.amount, Decimal("129.00"))
        funnel = order.metrika_funnel
        self.assertEqual(funnel.user, self.user)
        self.assertEqual(funnel.metrika_counter_id, 112093587)
        self.assertEqual(funnel.landing_path, "/bot/checkout/")
        self.assertIsNotNone(funnel.landing_viewed_at)
        self.assertIsNone(funnel.telegram_started_at)
        self.assertFalse(WhoUpdateMetrikaConversion.objects.exists())
        create.assert_called_once()

        captured = self.client.post(
            "/bot/analytics/client-id/",
            {"tracking_code": funnel.tracking_code, "client_id": "1788111111123456789"},
            content_type="application/json",
            HTTP_HOST="who-update.ru",
        )
        self.assertEqual(captured.status_code, 200)
        funnel.refresh_from_db()
        self.assertEqual(funnel.metrika_client_id, "1788111111123456789")
        order.refresh_from_db()
        self.assertEqual(order.status, WhoUpdatePaymentOrder.Status.PENDING)
        self.assertFalse(WhoUpdateMetrikaConversion.objects.exists())

    def test_same_order_reuses_checkout_funnel(self):
        order = WhoUpdatePaymentOrder.objects.create(
            user=self.user,
            plan="month",
            amount=Decimal("99.00"),
            duration_days=30,
        )
        request = RequestFactory().get("/bot/subscribe/month/signed-token/", HTTP_HOST="who-update.ru")

        first = payment_checkout(request, order, self.confirmation_url)
        order.refresh_from_db()
        funnel_id = order.metrika_funnel_id
        second = payment_checkout(request, order, self.confirmation_url)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.metrika_funnel_id, funnel_id)
        self.assertEqual(WhoUpdateOnboardingFunnel.objects.count(), 1)
        self.assertEqual(WhoUpdatePaymentOrder.objects.count(), 1)

        metrics = collect_daily_user_metrics()
        self.assertEqual(metrics.landing_views, 0)
        self.assertEqual(metrics.started_users, 0)

    def test_owner_dashboard_excludes_checkout_from_landing_metrics(self):
        from owner_panel.stats import dashboard

        for path, client_id in (("/", "landing-client"), ("/bot/checkout/", "checkout-client")):
            WhoUpdateOnboardingFunnel.objects.create(
                user=self.user,
                landing_path=path,
                landing_viewed_at=timezone.now() - timedelta(seconds=1),
                metrika_client_id=client_id,
            )

        dashboardFieldToValue = dashboard({})

        self.assertEqual(sum(metricToValue["views"] for metricToValue in dashboardFieldToValue["series"]), 1)
        self.assertEqual(sum(metricToValue["visitors"] for metricToValue in dashboardFieldToValue["series"]), 1)

    @override_settings(ALLOWED_HOSTS=["maksonchik.ru"])
    def test_legacy_host_checkout_uses_who_update_counter(self):
        order = WhoUpdatePaymentOrder.objects.create(
            user=self.user,
            plan="month",
            amount=Decimal("99.00"),
            duration_days=30,
        )
        request = RequestFactory().get("/bot/subscribe/month/signed-token/", HTTP_HOST="maksonchik.ru")

        response = payment_checkout(request, order, self.confirmation_url)

        order.refresh_from_db()
        self.assertEqual(order.metrika_funnel.metrika_counter_id, 112093587)
        self.assertContains(response, '"counterId": 112093587')
        self.assertNotContains(response, "111680333")
        self.assertNotContains(response, "signed-token")

    @patch("webhook_tg.wheel_checkout.create_payment")
    def test_wheel_new_and_reused_order_keep_single_tracking_visit(self, create):
        create.return_value = {"id": "wheel-payment-test", "confirmation_url": self.confirmation_url}
        self.reward()

        first = self.subscribe()
        second = self.subscribe()

        self.assertContains(first, self.confirmation_url)
        self.assertContains(second, self.confirmation_url)
        self.assertEqual(WhoUpdatePaymentOrder.objects.count(), 1)
        self.assertEqual(WhoUpdateOnboardingFunnel.objects.count(), 1)
        order = WhoUpdatePaymentOrder.objects.get()
        self.assertEqual(order.amount, Decimal("116.10"))
        self.assertEqual(order.metrika_funnel.user, self.user)
        create.assert_called_once()

    @patch("webhook_tg.wheel_checkout.create_payment")
    def test_double_reward_checkout_shows_actual_duration_without_changing_price(self, create):
        create.return_value = {"id": "double-payment-test", "confirmation_url": self.confirmation_url}
        reward = self.reward()
        reward.prize = "double"
        reward.save(update_fields=["prize"])

        response = self.subscribe()

        order = WhoUpdatePaymentOrder.objects.get()
        self.assertEqual(order.duration_days, 60)
        self.assertEqual(order.amount, Decimal("129.00"))
        self.assertContains(response, "Доступ на 60 дн.")
        self.assertNotContains(response, order.get_plan_display())
        create.assert_called_once()

    @patch("webhook_tg.wheel_checkout.create_payment")
    def test_internal_wheel_checkout_without_request_keeps_redirect(self, create):
        create.return_value = {"id": "wheel-payment-test", "confirmation_url": self.confirmation_url}
        reward = self.reward()

        response = checkout(self.user, "month", reward.pk)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], self.confirmation_url)
        self.assertFalse(WhoUpdateOnboardingFunnel.objects.exists())
