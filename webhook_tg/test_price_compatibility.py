from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from .models import UserTg, WhoUpdatePaymentOrder, WhoUpdateWheelReward
from .payment_views import fulfill_order
from .subscriptions import PLAN_CONFIG, checkout_token, plan_config_for_user
from .wheel import spin_day


class PriceCompatibilityTests(TestCase):
    def setUp(self):
        self.user = UserTg.objects.create(
            user_id=8888881,
            chat_id=8888881,
            access_unlimited=False,
        )
        new_month_price = patch.dict(PLAN_CONFIG["month"], amount=Decimal("129.00"))
        new_month_price.start()
        self.addCleanup(new_month_price.stop)
        block_network = patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("Network is disabled in price compatibility tests"),
        )
        block_network.start()
        self.addCleanup(block_network.stop)

    @patch("webhook_tg.payment_views.get_payment")
    def test_existing_pending_payment_preserves_legacy_amount_and_term(self, get_payment):
        order = WhoUpdatePaymentOrder.objects.create(
            user=self.user,
            plan="month",
            amount=Decimal("99.00"),
            duration_days=30,
        )
        get_payment.return_value = {
            "status": "succeeded",
            "paid": True,
            "amount": {"value": "99.00", "currency": "RUB"},
            "metadata": {"service": "who_update", "order_id": str(order.public_id)},
        }
        paid_at = timezone.now()

        with patch("webhook_tg.payment_views.timezone.now", return_value=paid_at):
            fulfill_order(order, "compatibility-legacy-payment")

        order.refresh_from_db()
        self.user.refresh_from_db()
        self.assertEqual(order.status, WhoUpdatePaymentOrder.Status.PAID)
        self.assertEqual(order.amount, Decimal("99.00"))
        self.assertEqual(order.duration_days, 30)
        self.assertEqual(self.user.access_expires_at, paid_at + timedelta(days=30))

    @patch("webhook_tg.wheel_checkout.create_payment")
    def test_old_reserved_discount_order_and_link_are_not_repriced(self, create_payment):
        order = WhoUpdatePaymentOrder.objects.create(
            user=self.user,
            plan="month",
            amount=Decimal("84.15"),
            duration_days=30,
        )
        WhoUpdateWheelReward.objects.create(
            user=self.user,
            prize="discount15",
            spin_day=spin_day(),
            expires_at=timezone.now() - timedelta(minutes=1),
            reserved_order=order,
            confirmation_url="https://pay.example/legacy-8415",
        )
        with patch.dict(PLAN_CONFIG["month"], amount=Decimal("99.00")):
            old_token = checkout_token(self.user, "month")

        fieldToValue = plan_config_for_user(self.user, "month")
        self.assertEqual(fieldToValue["amount"], Decimal("84.15"))
        self.assertEqual(fieldToValue["original_amount"], Decimal("129.00"))
        self.assertEqual(fieldToValue["days"], 30)
        response = self.client.get(f"/bot/subscribe/month/{old_token}/")

        self.assertContains(response, "https://pay.example/legacy-8415")
        create_payment.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.amount, Decimal("84.15"))
        self.assertEqual(order.duration_days, 30)
        self.assertEqual(order.status, WhoUpdatePaymentOrder.Status.PENDING)
        self.assertEqual(WhoUpdatePaymentOrder.objects.count(), 1)

    @patch(
        "webhook_tg.payment_views.create_payment",
        return_value={"id": "compatibility-new", "confirmation_url": "https://pay.example/new-129"},
    )
    def test_unopened_old_link_creates_order_at_current_price(self, create_payment):
        with patch.dict(PLAN_CONFIG["month"], amount=Decimal("99.00")):
            old_token = checkout_token(self.user, "month")

        response = self.client.get(f"/bot/subscribe/month/{old_token}/")

        self.assertContains(response, "129")
        self.assertContains(response, "https://pay.example/new-129")
        self.assertEqual(WhoUpdatePaymentOrder.objects.get().amount, Decimal("129.00"))
        self.assertEqual(create_payment.call_args.kwargs["amount"], Decimal("129.00"))
