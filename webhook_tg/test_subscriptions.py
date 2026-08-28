from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from .models import TelegramOutbox, UserTg, WhoUpdatePaymentOrder
from .payment_views import fulfill_order
from .subscriptions import (
    OWNER_TELEGRAM_ID,
    access_status_text,
    apply_rollout_policy,
    business_access_allowed,
    grant_referral_reward,
    checkout_token,
    plan_config_for_user,
    register_referral,
    start_trial_if_needed,
)


class WhoUpdateAccessTests(TestCase):
    def test_expired_status_contains_personal_referral_link(self):
        user = UserTg.objects.create(
            user_id=OWNER_TELEGRAM_ID,
            chat_id=OWNER_TELEGRAM_ID,
            access_unlimited=False,
            access_expires_at=timezone.now() - timedelta(minutes=1),
            referral_bonus_days=7,
        )

        message = access_status_text(user)

        self.assertIn("Доступ: <b>закончился</b>", message)
        self.assertIn("Бонус за приглашённых: <b>7 дн.</b>", message)
        self.assertIn("<b>+7 дней доступа</b>", message)
        self.assertIn(
            f"https://t.me/who_update_bot?start=ref_{user.referral_code}",
            message,
        )

    def test_rollout_does_not_limit_regular_user(self):
        user = UserTg.objects.create(user_id=100, chat_id=100)
        self.assertFalse(apply_rollout_policy(user))
        user.refresh_from_db()
        self.assertTrue(user.access_unlimited)
        self.assertTrue(user.has_active_access())

    def test_owner_gets_fourteen_day_trial(self):
        user = UserTg.objects.create(user_id=OWNER_TELEGRAM_ID, chat_id=OWNER_TELEGRAM_ID)
        before = timezone.now()
        self.assertTrue(apply_rollout_policy(user))
        self.assertTrue(start_trial_if_needed(user, at=before))
        user.refresh_from_db()
        self.assertFalse(user.access_unlimited)
        self.assertEqual(user.trial_started_at, before)
        self.assertEqual(user.access_expires_at, before + timedelta(days=14))

    @patch("webhook_tg.telegram.tg_send_message", return_value=True)
    def test_referral_reward_is_granted_once_after_connection(self, send_message):
        now = timezone.now()
        inviter = UserTg.objects.create(
            user_id=OWNER_TELEGRAM_ID,
            chat_id=OWNER_TELEGRAM_ID,
            access_unlimited=False,
            trial_started_at=now,
            access_expires_at=now + timedelta(days=14),
        )
        invitee = UserTg.objects.create(user_id=200, chat_id=200)
        self.assertTrue(register_referral(invitee, f"ref_{inviter.referral_code}"))
        invitee.refresh_from_db()

        with self.captureOnCommitCallbacks(execute=True):
            self.assertTrue(grant_referral_reward(invitee, at=now))
        with self.captureOnCommitCallbacks(execute=True):
            self.assertFalse(grant_referral_reward(invitee, at=now))

        inviter.refresh_from_db()
        invitee.refresh_from_db()
        self.assertEqual(inviter.referral_bonus_days, 7)
        self.assertEqual(inviter.access_expires_at, now + timedelta(days=21))
        self.assertIsNotNone(invitee.referral_rewarded_at)
        send_message.assert_called_once()

    @patch("webhook_tg.telegram.tg_send_message", return_value=True)
    def test_expired_owner_business_updates_are_blocked_once(self, send_message):
        user = UserTg.objects.create(
            user_id=OWNER_TELEGRAM_ID,
            chat_id=OWNER_TELEGRAM_ID,
            business_connection_id="expired-connection",
            access_unlimited=False,
            access_expires_at=timezone.now() - timedelta(seconds=1),
        )
        msg = {"business_connection_id": "expired-connection"}
        self.assertFalse(business_access_allowed(msg))
        self.assertFalse(business_access_allowed(msg))
        user.refresh_from_db()
        self.assertIsNotNone(user.access_expired_notified_at)
        send_message.assert_called_once()
        message = send_message.call_args.args[1]
        keyboard = send_message.call_args.kwargs["reply_markup"]
        self.assertIn("99 ₽", message)
        self.assertIn("https://t.me/who_update_bot?start=ref_", message)
        self.assertIn("/referral", message)
        self.assertEqual(len(keyboard["inline_keyboard"]), 3)


@override_settings(
    YOOKASSA_SHOP_ID="shop",
    YOOKASSA_SECRET_KEY="test_secret",
    WHO_UPDATE_PAYMENT_WEBHOOK_TOKEN="webhook-secret",
)
class WhoUpdatePaymentTests(TestCase):
    def setUp(self):
        self.user = UserTg.objects.create(
            user_id=OWNER_TELEGRAM_ID,
            chat_id=OWNER_TELEGRAM_ID,
            access_unlimited=False,
            access_expires_at=timezone.now() + timedelta(days=2),
        )
        self.order = WhoUpdatePaymentOrder.objects.create(
            user=self.user,
            plan=WhoUpdatePaymentOrder.Plan.MONTH,
            duration_days=30,
            amount=Decimal("99.00"),
            yookassa_payment_id="payment-1",
        )

    def payment_payload(self):
        return {
            "id": "payment-1",
            "status": "succeeded",
            "paid": True,
            "amount": {"value": "99.00", "currency": "RUB"},
            "metadata": {"service": "who_update", "order_id": str(self.order.public_id)},
        }

    def test_month_price_is_the_same_for_owner_and_regular_user(self):
        regular_user = UserTg.objects.create(user_id=300, chat_id=300)
        self.assertEqual(plan_config_for_user(self.user, "month")["amount"], Decimal("99.00"))
        self.assertEqual(plan_config_for_user(regular_user, "month")["amount"], Decimal("99.00"))
        self.assertEqual(
            plan_config_for_user(self.user, "three_months")["amount"],
            Decimal("199.00"),
        )

    @patch("webhook_tg.payment_views.create_payment")
    def test_month_checkout_creates_order_and_owner_notification(self, create_payment_mock):
        create_payment_mock.return_value = {
            "id": "test-one-ruble-payment",
            "confirmation_url": "https://yookassa.test/confirmation",
        }
        response = self.client.get(f"/bot/subscribe/month/{checkout_token(self.user, 'month')}/")

        self.assertEqual(response.status_code, 302)
        order = WhoUpdatePaymentOrder.objects.get(yookassa_payment_id="test-one-ruble-payment")
        self.assertEqual(order.amount, Decimal("99.00"))
        self.assertEqual(create_payment_mock.call_args.kwargs["amount"], Decimal("99.00"))
        notification = TelegramOutbox.objects.get(
            dedup_key=f"who-update-payment-open:{order.public_id}"
        )
        self.assertIn("переход к оплате", notification.payload["text"])
        self.assertIn("99.00 ₽", notification.payload["text"])

    @patch("webhook_tg.payment_views.tg_send_message", return_value=True)
    @patch("webhook_tg.payment_views.get_payment")
    def test_fulfillment_is_verified_and_idempotent(self, get_payment_mock, send_message):
        get_payment_mock.return_value = self.payment_payload()
        original_expiry = self.user.access_expires_at
        with self.captureOnCommitCallbacks(execute=True):
            fulfill_order(self.order, "payment-1")
        with self.captureOnCommitCallbacks(execute=True):
            fulfill_order(self.order, "payment-1")

        self.user.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, WhoUpdatePaymentOrder.Status.PAID)
        self.assertEqual(self.user.access_expires_at, original_expiry + timedelta(days=30))
        self.assertEqual(get_payment_mock.call_count, 1)
        send_message.assert_called_once()
        notification = TelegramOutbox.objects.get(
            dedup_key=f"who-update-payment-paid:{self.order.public_id}"
        )
        self.assertIn("получена оплата", notification.payload["text"])
        self.assertIn("99.00 ₽", notification.payload["text"])

    @patch("webhook_tg.payment_views.get_payment")
    def test_forwarded_webhook_fulfills_order(self, get_payment_mock):
        get_payment_mock.return_value = self.payment_payload()
        response = self.client.post(
            "/bot/yookassa/webhook/",
            data={
                "event": "payment.succeeded",
                "object": self.payment_payload(),
            },
            content_type="application/json",
            HTTP_X_WHO_UPDATE_PAYMENT_TOKEN="webhook-secret",
        )
        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, WhoUpdatePaymentOrder.Status.PAID)

    def test_untrusted_webhook_is_rejected(self):
        response = self.client.post(
            "/bot/yookassa/webhook/",
            data={},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
