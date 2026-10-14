from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone

from .config import START_TEXT
from .models import TelegramOutbox, UserTg, WhoUpdatePaymentOrder
from .payment_views import fulfill_order
from .subscriptions import (
    access_status_text,
    grant_referral_reward,
    notify_access_expired,
    start_trial_if_needed,
)
from .tests import NoTelegramApiTestCase, make_start_payload
from .views import process_telegram_update
from .wheel import PRIZES, spin


@override_settings(WHO_UPDATE_TRIAL_DAYS=7)
class TrialActivationTests(NoTelegramApiTestCase):
    def setUp(self):
        super().setUp()
        self.user_id = 710001
        self.update_id = 971000
        self.mock_post.return_value.json.return_value = {
            "ok": True,
            "result": {"message_id": 123, "chat": {"id": self.user_id}},
        }

    def send_command(self, text):
        self.update_id += 1
        updateFieldToValue = make_start_payload(
            chat_id=self.user_id,
            user_id=self.user_id,
            username="trial_user",
            update_id=self.update_id,
        )
        updateFieldToValue["message"]["text"] = text
        process_telegram_update(updateFieldToValue, use_idempotency=False)
        return UserTg.objects.get(user_id=self.user_id)

    def connect(self, *, enabled=True, connection_id="trial-connection", at=None, username="trial_user"):
        self.update_id += 1
        at = at or timezone.now()
        updateFieldToValue = {
            "update_id": self.update_id,
            "business_connection": {
                "id": connection_id,
                "user": {"id": self.user_id, "first_name": "Trial", "username": username},
                "user_chat_id": self.user_id,
                "date": int(at.timestamp()),
                "is_enabled": enabled,
            },
        }
        with patch("webhook_tg.views.timezone.now", return_value=at):
            process_telegram_update(updateFieldToValue, use_idempotency=False)
        return UserTg.objects.get(user_id=self.user_id)

    def create_connected_user(self, **fields):
        return UserTg.objects.create(
            user_id=self.user_id,
            chat_id=self.user_id,
            access_unlimited=False,
            business_is_connected=True,
            business_connection_id="trial-connection",
            **fields,
        )

    def test_repeated_commands_do_not_start_trial(self):
        for command in ("/start", "/start", "/status", "/subscription", "/subscribe", "/spin"):
            with self.subTest(command=command):
                user = self.send_command(command)
                self.assertFalse(user.access_unlimited)
                self.assertIsNone(user.trial_started_at)
                self.assertIsNone(user.access_expires_at)
                self.assertIsNone(user.access_expired_notified_at)

        welcome = list(TelegramOutbox.objects.filter(
            chat_id=self.user_id,
            method=TelegramOutbox.Method.SEND_PHOTO,
        ))
        self.assertEqual(len(welcome), 2)
        self.assertTrue(all(item.payload["caption"] == START_TEXT for item in welcome))
        self.assertFalse(TelegramOutbox.objects.filter(
            chat_id=self.user_id,
            idempotency_key__endswith=":expired-access",
        ).exists())

    def test_spin_as_first_command_does_not_start_trial(self):
        user = self.send_command("/spin")
        self.assertFalse(user.access_unlimited)
        self.assertIsNone(user.trial_started_at)
        self.assertIsNone(user.access_expires_at)

    def test_pending_status_does_not_claim_access_has_expired(self):
        user = self.send_command("/status")
        text = access_status_text(user).lower()
        self.assertIn("пробный период", text)
        self.assertIn("подключения", text)
        self.assertNotIn("закончился", text)

    def test_never_started_user_is_not_sent_an_expiry_notification(self):
        user = self.send_command("/start")
        with patch("webhook_tg.telegram.tg_send_message") as send_message:
            self.assertFalse(notify_access_expired(user))
        send_message.assert_not_called()
        user.refresh_from_db()
        self.assertIsNone(user.access_expired_notified_at)

    def test_expired_preconnection_bonus_keeps_trial_pending(self):
        UserTg.objects.create(
            user_id=self.user_id,
            chat_id=self.user_id,
            access_unlimited=False,
            access_expires_at=timezone.now() - timedelta(days=1),
            referral_bonus_days=7,
        )
        user = self.send_command("/start")
        self.assertIsNone(user.trial_started_at)
        welcome = TelegramOutbox.objects.get(
            chat_id=self.user_id,
            method=TelegramOutbox.Method.SEND_PHOTO,
        )
        self.assertEqual(welcome.payload["caption"], START_TEXT)
        self.assertIn("пробный период", access_status_text(user).lower())
        self.assertNotIn("закончился", access_status_text(user).lower())
        with patch("webhook_tg.telegram.tg_send_message") as send_message:
            self.assertFalse(notify_access_expired(user))
        send_message.assert_not_called()

    def test_first_connection_starts_seven_days_not_from_previous_start(self):
        user = self.send_command("/start")
        started_at = user.last_start_at
        connected_at = started_at + timedelta(days=3)

        user = self.connect(at=connected_at)

        self.assertEqual(user.last_start_at, started_at)
        self.assertEqual(user.trial_started_at, connected_at)
        self.assertEqual(user.access_expires_at, connected_at + timedelta(days=7))
        self.assertTrue(user.business_is_connected)

    def test_direct_first_connection_without_start_receives_trial(self):
        now = timezone.now()
        user = self.connect(at=now)
        self.assertIsNone(user.last_start_at)
        self.assertEqual(user.trial_started_at, now)
        self.assertEqual(user.access_expires_at, now + timedelta(days=7))

    def test_disabled_event_does_not_start_trial_but_first_enabled_event_does(self):
        now = timezone.now()
        user = self.connect(enabled=False, at=now)
        self.assertIsNone(user.trial_started_at)
        self.assertIsNone(user.access_expires_at)

        connected_at = now + timedelta(hours=2)
        user = self.connect(at=connected_at)
        self.assertEqual(user.trial_started_at, connected_at)
        self.assertEqual(user.access_expires_at, connected_at + timedelta(days=7))

    def test_duplicate_connection_does_not_extend_trial(self):
        now = timezone.now()
        self.connect(at=now)
        user = self.connect(at=now + timedelta(days=1))
        self.assertEqual(user.trial_started_at, now)
        self.assertEqual(user.access_expires_at, now + timedelta(days=7))

    def test_reconnect_same_or_new_connection_and_username_does_not_restart_expired_trial(self):
        now = timezone.now()
        self.connect(at=now)
        expired_at = now + timedelta(days=7)
        self.connect(enabled=False, at=now + timedelta(days=8))
        user = self.connect(at=now + timedelta(days=9))
        self.assertEqual(user.trial_started_at, now)
        self.assertEqual(user.access_expires_at, expired_at)

        self.connect(enabled=False, at=now + timedelta(days=10))
        user = self.connect(
            connection_id="replacement-connection",
            username="renamed_trial_user",
            at=now + timedelta(days=11),
        )
        self.assertEqual(user.username, "renamed_trial_user")
        self.assertEqual(user.trial_started_at, now)
        self.assertEqual(user.access_expires_at, expired_at)

    def test_trial_requires_enabled_connection_with_id(self):
        user = UserTg.objects.create(
            user_id=self.user_id,
            chat_id=self.user_id,
            access_unlimited=False,
        )
        self.assertFalse(start_trial_if_needed(user))
        user.business_is_connected = True
        user.save(update_fields=["business_is_connected"])
        self.assertFalse(start_trial_if_needed(user))
        user.refresh_from_db()
        self.assertIsNone(user.trial_started_at)
        self.assertIsNone(user.access_expires_at)

    def test_stale_user_instance_cannot_grant_second_trial(self):
        now = timezone.now()
        user = self.create_connected_user()
        stale_user = UserTg.objects.get(pk=user.pk)
        self.assertTrue(start_trial_if_needed(user, at=now))
        self.assertFalse(start_trial_if_needed(stale_user, at=now + timedelta(days=20)))
        user.refresh_from_db()
        self.assertEqual(user.trial_started_at, now)
        self.assertEqual(user.access_expires_at, now + timedelta(days=7))

    def test_existing_trial_deadline_is_preserved(self):
        now = timezone.now()
        original_start = now - timedelta(days=12)
        original_expiry = now - timedelta(days=5)
        user = self.create_connected_user(
            trial_started_at=original_start,
            access_expires_at=original_expiry,
        )
        self.assertFalse(start_trial_if_needed(user, at=now))
        user.refresh_from_db()
        self.assertEqual(user.trial_started_at, original_start)
        self.assertEqual(user.access_expires_at, original_expiry)

    def test_payment_before_connection_is_preserved_when_trial_starts(self):
        now = timezone.now()
        user = self.send_command("/start")
        order = WhoUpdatePaymentOrder.objects.create(
            user=user,
            plan=WhoUpdatePaymentOrder.Plan.MONTH,
            amount=Decimal("99.00"),
            duration_days=30,
            yookassa_payment_id="trial-before-connection-payment",
        )
        paymentFieldToValue = {
            "id": order.yookassa_payment_id,
            "status": "succeeded",
            "paid": True,
            "amount": {"value": "99.00", "currency": "RUB"},
            "metadata": {"service": "who_update", "order_id": str(order.public_id)},
        }
        with patch("webhook_tg.payment_views.get_payment", return_value=paymentFieldToValue):
            with patch("webhook_tg.payment_views.timezone.now", return_value=now):
                fulfill_order(order, order.yookassa_payment_id)
        user.refresh_from_db()
        self.assertIsNone(user.trial_started_at)
        paid_until = user.access_expires_at
        self.assertEqual(paid_until, now + timedelta(days=30))

        user = self.connect(at=now + timedelta(days=1))
        self.assertEqual(user.access_expires_at, paid_until + timedelta(days=7))

    def test_referral_days_before_connection_are_not_counted_twice(self):
        now = timezone.now()
        user = self.send_command("/start")
        invitee = UserTg.objects.create(user_id=710002, chat_id=710002, referred_by=user)
        self.assertTrue(grant_referral_reward(invitee, at=now))
        user.refresh_from_db()
        self.assertIsNone(user.trial_started_at)
        self.assertEqual(user.referral_bonus_days, 7)
        self.assertEqual(user.access_expires_at, now + timedelta(days=7))

        user = self.connect(at=now + timedelta(days=1))
        self.assertEqual(user.access_expires_at, now + timedelta(days=14))
        self.assertEqual(user.referral_bonus_days, 7)
        self.assertFalse(grant_referral_reward(invitee, at=now + timedelta(days=2)))

    def test_wheel_days_before_connection_are_preserved(self):
        now = timezone.now()
        user = self.send_command("/spin")
        with patch("webhook_tg.wheel.choose_prize", return_value=PRIZES[3]):
            with patch("webhook_tg.wheel.timezone.now", return_value=now):
                reward = spin(user)
        user.refresh_from_db()
        self.assertEqual(reward.granted_days, 3)
        self.assertIsNone(user.trial_started_at)
        self.assertEqual(user.access_expires_at, now + timedelta(days=3))

        user = self.connect(at=now + timedelta(days=1))
        self.assertEqual(user.access_expires_at, now + timedelta(days=10))

    def test_expired_extra_days_do_not_reduce_first_trial(self):
        now = timezone.now()
        self.create_connected_user(access_expires_at=now - timedelta(days=2))
        user = self.connect(at=now)
        self.assertEqual(user.trial_started_at, now)
        self.assertEqual(user.access_expires_at, now + timedelta(days=7))
