import hashlib
import hmac
import json
from collections import Counter
from datetime import datetime, timedelta, timezone as datetime_timezone
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import urlencode

from django.core import signing
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import UserTg, WhoUpdateWheelReward, WhoUpdatePaymentOrder, TelegramOutbox
from .payment_views import fulfill_order, _enqueue_user_paid_notification
from .subscriptions import (
    PLAN_CONFIG, CHECKOUT_SIGNING_SALT, checkout_token, subscription_keyboard,
    plan_config_for_user, expired_access_text, access_status_text,
)
from .wheel import (
    PILOT_USER_ID, PRIZES, PRIZE_WEIGHTS, active_reward, authenticate,
    choose_prize, plan_text, reward_config, spin, spin_day, grant_instant_days, reset_spin,
)
from .yookassa import YooKassaError
from .wheel_views import state


def init_data(user_id=PILOT_USER_ID, age=0, **extra):
    from env import TOKEN_BOT
    data = {"auth_date": str(int(timezone.now().timestamp()) - age), "user": json.dumps({"id": user_id}), **extra}
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    key = hmac.new(b"WebAppData", TOKEN_BOT.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


class WheelTests(TestCase):
    def setUp(self):
        chooser = patch("webhook_tg.wheel.choose_prize", return_value=PRIZES[0])
        chooser.start()
        self.addCleanup(chooser.stop)
        self.owner = UserTg.objects.create(user_id=PILOT_USER_ID, chat_id=PILOT_USER_ID, access_unlimited=False)
        self.regular = UserTg.objects.create(user_id=4321, chat_id=4321, access_unlimited=False)

    def reward(self, prize="discount15"):
        return WhoUpdateWheelReward.objects.create(user=self.owner, spin_day=spin_day(), prize=prize, expires_at=timezone.now() + timedelta(hours=24))

    def api(self, action="state", data=None, **body):
        return self.client.post(f"/bot/wheel/api/{action}/", body, HTTP_AUTHORIZATION="tma " + (data or init_data()))

    def subscribe(self, plan="month", token=None):
        return self.client.get(f"/bot/subscribe/{plan}/{token or checkout_token(self.owner, plan)}/")

    def test_owner_authentication_including_telegram_signature_field(self):
        self.assertEqual(authenticate(init_data(signature="signed-extra-field")).pk, self.owner.pk)

    def test_invalid_authentication_and_unknown_user_never_mutate(self):
        for value in ["", "user=1", init_data(999999), init_data(-1), init_data(age=3601), init_data(age=-100),
                      init_data().replace("auth_date=", "auth_date=0"), init_data() + "&user=123"]:
            with self.subTest(value=value[:20]):
                response = self.client.post("/bot/wheel/api/spin/", HTTP_AUTHORIZATION="tma " + value)
                self.assertEqual(response.status_code, 403)
        self.assertEqual(WhoUpdateWheelReward.objects.count(), 0)
        self.assertEqual(WhoUpdatePaymentOrder.objects.count(), 0)

    def test_read_only_state_does_not_spin(self):
        self.assertIsNone(self.api().json()["reward"])
        self.assertEqual(WhoUpdateWheelReward.objects.count(), 0)

    def test_probabilities_remain_server_side(self):
        page = self.client.get("/bot/wheel/").content.decode()
        rules = page.split("<details>", 1)[1].split("</details>", 1)[0]
        for value in ["%", "Шансы", "вероятност", "35", "25"]:
            self.assertNotIn(value, rules)
        for action in ["state", "spin"]:
            data = self.api(action).json()
            self.assertEqual(len(data["prizes"]), len(PRIZES))
            self.assertTrue(all(set(prize) == {"id", "label"} for prize in data["prizes"]))
            self.assertNotIn("probability", json.dumps(data))
            self.assertNotIn("weight", json.dumps(data))

    @override_settings(ALLOWED_HOSTS=["who-update.ru"])
    def test_standalone_domain_routes(self):
        self.assertEqual(self.client.get("/bot/wheel/", HTTP_HOST="who-update.ru").status_code, 200)
        self.assertEqual(self.client.post("/bot/wheel/api/spin/", HTTP_HOST="who-update.ru").status_code, 403)

    def test_shell_contains_no_personal_checkout_and_denies_get_mutation(self):
        response = self.client.get("/bot/wheel/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="app" hidden')
        self.assertNotContains(response, str(PILOT_USER_ID))
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(self.client.get("/bot/wheel/api/spin/").status_code, 405)

    def test_exact_prize_probability_intervals(self):
        counts = Counter()
        for ticket in range(100):
            with patch("webhook_tg.wheel.secrets.randbelow", return_value=ticket):
                counts[choose_prize()["id"]] += 1
        self.assertEqual(counts, dict(zip([p["id"] for p in PRIZES], PRIZE_WEIGHTS)))

    @patch("webhook_tg.wheel.choose_prize", return_value=PRIZES[5])
    def test_spin_is_idempotent_and_ignores_client_prize(self, choose):
        first = self.api("spin", prize="discount20", user_id=4321).json()
        second = self.api("spin").json()
        self.assertEqual(first["reward"], second["reward"])
        self.assertEqual(first["reward"]["prize"], "double")
        self.assertEqual(choose.call_count, 1)
        self.assertEqual(WhoUpdateWheelReward.objects.count(), 1)
        self.assertEqual(TelegramOutbox.objects.filter(idempotency_key__startswith="wheel-reward:").count(), 1)

    def test_spin_can_be_called_for_regular_user(self):
        self.assertEqual(spin(self.regular).user,self.regular)

    def test_free_days_extend_active_access_without_order_and_only_once(self):
        now = timezone.now()
        self.owner.access_expires_at = now + timedelta(days=10)
        self.owner.access_expired_notified_at = now - timedelta(days=1)
        self.owner.save(update_fields=["access_expires_at", "access_expired_notified_at"])
        for prize, days in [(PRIZES[3], 3), (PRIZES[4], 7)]:
            with patch("webhook_tg.wheel.choose_prize", return_value=prize):
                reward = spin(self.owner)
                expected = self.owner.access_expires_at + timedelta(days=days)
                self.owner.refresh_from_db()
                self.assertEqual(self.owner.access_expires_at, expected)
                self.assertEqual(reward.access_expires_at_after, expected)
                self.assertEqual(reward.granted_days, days)
                self.assertIsNotNone(reward.used_at)
                self.assertIsNone(self.owner.access_expired_notified_at)
                self.assertFalse(WhoUpdatePaymentOrder.objects.exists())
                self.assertIsNone(active_reward(self.owner))
                self.assertEqual(state(self.owner)["reward"]["status"], "granted")
                self.assertEqual(spin(self.owner).pk, reward.pk)
                self.assertFalse(grant_instant_days(reward))
                self.owner.refresh_from_db()
                self.assertEqual(self.owner.access_expires_at, expected)
                notice = TelegramOutbox.objects.get(idempotency_key=f"wheel-free-days:{reward.pk}")
                self.assertIn("Ничего оплачивать не нужно", notice.payload["text"])
                self.assertNotIn("/subscribe/", json.dumps(notice.payload))
                self.assertTrue(reset_spin(reward))
        self.assertEqual(self.owner.access_expires_at, now + timedelta(days=20))
        self.assertEqual(WhoUpdateWheelReward.objects.filter(granted_days__gt=0).count(), 2)

    def test_free_days_start_now_when_access_has_expired(self):
        now = timezone.now()
        self.owner.access_expires_at = now - timedelta(days=20)
        self.owner.save(update_fields=["access_expires_at"])
        with patch("webhook_tg.wheel.choose_prize", return_value=PRIZES[4]), patch("webhook_tg.wheel.timezone.now", return_value=now):
            spin(self.owner)
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.access_expires_at, now + timedelta(days=7))

    def test_free_days_do_not_downgrade_unlimited_access(self):
        self.owner.access_unlimited = True
        self.owner.save(update_fields=["access_unlimited"])
        reward = self.reward("days3")
        self.assertTrue(grant_instant_days(reward))
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.access_unlimited)
        self.assertIsNone(self.owner.access_expires_at)

    def test_existing_free_days_can_be_granted_once_without_checkout(self):
        reward = self.reward("days3")
        self.assertTrue(grant_instant_days(reward))
        expiry = UserTg.objects.get(pk=self.owner.pk).access_expires_at
        self.assertFalse(grant_instant_days(reward))
        self.assertEqual(UserTg.objects.get(pk=self.owner.pk).access_expires_at, expiry)
        token = signing.dumps({"user_id":self.owner.user_id,"plan":"month","wheel_reward":reward.pk},salt=CHECKOUT_SIGNING_SALT)
        self.assertEqual(self.subscribe(token=token).status_code, 409)
        self.assertFalse(WhoUpdatePaymentOrder.objects.exists())

    def test_ungranted_free_days_cannot_create_payment(self):
        reward = self.reward("days7")
        token = signing.dumps({"user_id":self.owner.user_id,"plan":"month","wheel_reward":reward.pk},salt=CHECKOUT_SIGNING_SALT)
        self.assertEqual(self.subscribe(token=token).status_code, 409)
        self.assertFalse(WhoUpdatePaymentOrder.objects.exists())

    def test_existing_paid_or_reserved_reward_is_not_granted_twice(self):
        reward = self.reward("days3")
        reward.reserved_order = WhoUpdatePaymentOrder.objects.create(user=self.owner,plan="month",amount=Decimal("99"),duration_days=33)
        reward.save(update_fields=["reserved_order"])
        self.assertFalse(grant_instant_days(reward))
        self.owner.refresh_from_db()
        self.assertIsNone(self.owner.access_expires_at)

    def test_free_days_and_reward_are_rolled_back_together_on_failure(self):
        with patch("webhook_tg.wheel.choose_prize",return_value=PRIZES[3]), patch("webhook_tg.outbox.enqueue_outbox",side_effect=RuntimeError("database unavailable")):
            with self.assertRaises(RuntimeError):
                spin(self.owner)
        self.owner.refresh_from_db()
        self.assertIsNone(self.owner.access_expires_at)
        self.assertFalse(WhoUpdateWheelReward.objects.exists())

    def test_free_days_grant_works_for_regular_users(self):
        reward = WhoUpdateWheelReward.objects.create(user=self.regular,spin_day=spin_day(),prize="days7",expires_at=timezone.now()+timedelta(hours=24))
        self.assertTrue(grant_instant_days(reward))
        self.regular.refresh_from_db()
        self.assertGreater(self.regular.access_expires_at, timezone.now()+timedelta(days=6))
        self.assertFalse(grant_instant_days(reward))

    def test_new_spin_at_moscow_midnight_keeps_history(self):
        before = datetime(2026, 9, 8, 20, 59, 59, tzinfo=datetime_timezone.utc)
        after = before + timedelta(seconds=1)
        with patch("webhook_tg.wheel.timezone.now", return_value=before):
            first = spin(self.owner)
            self.assertEqual(spin(self.owner).pk, first.pk)
            status = state(self.owner)
            self.assertFalse(status["can_spin"])
            self.assertEqual(status["next_spin_at"], "2026-09-09T00:00:00+03:00")
        with patch("webhook_tg.wheel.timezone.now", return_value=after):
            self.assertTrue(state(self.owner)["can_spin"])
            second = spin(self.owner)
            self.assertNotEqual(first.pk, second.pk)
            self.assertEqual(spin(self.owner).pk, second.pk)
            self.assertEqual(active_reward(self.owner).pk, second.pk)
        self.assertEqual(WhoUpdateWheelReward.objects.count(), 2)

    def test_database_enforces_one_spin_per_day(self):
        self.reward()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.reward("double")

    def test_used_prize_does_not_allow_extra_spin_today_or_restore_old_prize(self):
        yesterday = timezone.now() - timedelta(days=1)
        with patch("webhook_tg.wheel.timezone.now", return_value=yesterday):
            old = spin(self.owner)
        today = spin(self.owner)
        today.used_at = timezone.now()
        today.save(update_fields=["used_at"])
        self.assertIsNone(active_reward(self.owner))
        self.assertFalse(state(self.owner)["can_spin"])
        self.assertEqual(spin(self.owner).pk, today.pk)
        self.assertNotEqual(old.pk, today.pk)

    def test_new_prize_rejects_old_unreserved_checkout_link(self):
        yesterday = timezone.now() - timedelta(days=1)
        with patch("webhook_tg.wheel.timezone.now", return_value=yesterday):
            old = spin(self.owner)
            token = checkout_token(self.owner, "month")
        # Still-valid 24h prize from yesterday must nevertheless be superseded.
        old.expires_at = timezone.now() + timedelta(hours=1)
        old.save(update_fields=["expires_at"])
        spin(self.owner)
        self.assertEqual(self.subscribe(token=token).status_code, 409)
        self.assertFalse(WhoUpdatePaymentOrder.objects.exists())

    @patch("webhook_tg.wheel_checkout.create_payment", return_value={"id": "old-payment", "confirmation_url": "https://pay.example/old"})
    def test_new_day_preserves_reserved_order_and_old_link(self, create):
        yesterday = timezone.now() - timedelta(days=1)
        with patch("webhook_tg.wheel.timezone.now", return_value=yesterday):
            old = self.reward("double")
            token = checkout_token(self.owner, "month")
            self.assertContains(self.subscribe(token=token), "https://pay.example/old")
        order = WhoUpdatePaymentOrder.objects.get()
        new = spin(self.owner)
        self.assertContains(self.subscribe(token=token), "https://pay.example/old")
        self.assertEqual(create.call_count, 1)
        order.refresh_from_db()
        self.assertEqual(order.duration_days, 60)
        self.assertEqual(order.amount, Decimal("99.00"))
        self.assertNotEqual(old.pk, new.pk)

    def test_ui_has_daily_rules_without_private_test_copy(self):
        response = self.client.get("/bot/wheel/")
        self.assertContains(response, "00:00 по Москве")
        self.assertContains(response, "1 вращение в день")
        for value in ["Личный тест", "только владельцу", "вращение на аккаунт"]:
            self.assertNotContains(response, value)

    def test_regular_user_prices_without_reward_and_wheel_button(self):
        self.reward("double")
        for plan, config in PLAN_CONFIG.items():
            self.assertEqual(plan_config_for_user(self.regular, plan), config)
        keyboard = subscription_keyboard(self.regular)
        self.assertEqual([row[0]["text"] for row in keyboard["inline_keyboard"][:3]], ["1 месяц — 99 ₽", "3 месяца — 249 ₽", "1 год — 800 ₽"])
        self.assertIn("web_app", keyboard["inline_keyboard"][3][0])
        self.assertNotIn("<s>", expired_access_text(self.regular))
        self.assertNotIn("wheel_reward", signing.loads(checkout_token(self.regular, "month"), salt=CHECKOUT_SIGNING_SALT))

    def test_all_prizes_all_tariffs_display_consistent_actual_values(self):
        for prize in PRIZES:
            reward = self.reward(prize["id"])
            if prize.get("extra_days"):
                for plan, base in PLAN_CONFIG.items():
                    self.assertEqual(plan_config_for_user(self.owner, plan), base)
                    self.assertEqual(reward_config(base, reward), base)
                reward.delete()
                continue
            for plan, base in PLAN_CONFIG.items():
                config = plan_config_for_user(self.owner, plan)
                if prize.get("discount"):
                    expected = (base["amount"] * Decimal(100 - prize["discount"]) / 100).quantize(Decimal(".01"))
                    self.assertEqual(config["amount"], expected)
                else:
                    self.assertEqual(config["days"], base["days"] * prize.get("multiplier", 1) + prize.get("extra_days", 0))
                self.assertIn("<s>", plan_text(config))
                self.assertIn("\u0336", plan_text(config, button=True))
                self.assertNotIn("<s>", plan_text(config, button=True))
            self.assertIn("<s>", expired_access_text(self.owner))
            self.assertIn("<s>", access_status_text(self.owner))
            reward.delete()

    def test_expired_prize_cannot_be_respinned_or_charge_full_price_with_old_link(self):
        reward = self.reward()
        token = checkout_token(self.owner, "month")
        reward.expires_at = timezone.now() - timedelta(seconds=1)
        reward.save()
        self.assertIsNone(active_reward(self.owner))
        self.assertEqual(spin(self.owner).pk, reward.pk)
        self.assertEqual(self.subscribe(token=token).status_code, 409)
        self.assertEqual(WhoUpdatePaymentOrder.objects.count(), 0)

    @patch("webhook_tg.wheel_checkout.create_payment", return_value={"id": "test-payment", "confirmation_url": "https://pay.example/test"})
    def test_discount_payment_amount_matches_ui_and_order_reused(self, create):
        reward = self.reward()
        self.assertContains(self.subscribe(), "https://pay.example/test")
        self.assertContains(self.subscribe(), "https://pay.example/test")
        self.assertEqual(self.subscribe(plan="year").status_code, 409)
        order = WhoUpdatePaymentOrder.objects.get()
        self.assertEqual(order.amount, Decimal("84.15"))
        self.assertEqual(order.duration_days, 30)
        self.assertEqual(create.call_count, 1)
        self.assertEqual(create.call_args.kwargs["amount"], Decimal("84.15"))
        self.assertEqual(create.call_args.kwargs["idempotence_key"], str(order.public_id))
        reward.refresh_from_db()
        self.assertIsNone(reward.used_at)
        self.assertEqual(self.api().json()["reward"]["reserved_plan"], "month")

    @patch("webhook_tg.wheel_checkout.create_payment")
    def test_provider_timeout_keeps_same_order_idempotency_key(self, create):
        self.reward()
        create.side_effect = YooKassaError("timeout")
        self.assertEqual(self.subscribe().status_code, 502)
        key = create.call_args.kwargs["idempotence_key"]
        create.side_effect = None
        create.return_value = {"id": "test-payment", "confirmation_url": "https://pay.example/test"}
        self.assertContains(self.subscribe(), "https://pay.example/test")
        self.assertEqual(create.call_args.kwargs["idempotence_key"], key)
        self.assertEqual(WhoUpdatePaymentOrder.objects.count(), 1)

    @patch("webhook_tg.wheel_checkout.create_payment", return_value={"id": "test-payment", "confirmation_url": "https://pay.example/test"})
    @patch("webhook_tg.payment_views.get_payment")
    def test_double_fulfillment_exactly_once_with_snapshot_after_expiry(self, get_payment, create):
        reward = self.reward("double")
        token = checkout_token(self.owner, "year")
        self.assertContains(self.subscribe("year", token), "https://pay.example/test")
        order = WhoUpdatePaymentOrder.objects.get()
        self.assertEqual(order.duration_days, 730)
        reward.expires_at = timezone.now() - timedelta(days=1)
        reward.save(update_fields=["expires_at"])
        get_payment.return_value = {"status": "succeeded", "paid": True, "amount": {"value": "800.00", "currency": "RUB"},
                                    "metadata": {"service": "who_update", "order_id": str(order.public_id)}}
        with self.captureOnCommitCallbacks(execute=True):
            fulfilled = fulfill_order(order, "test-payment")
            fulfill_order(order, "test-payment")
        self.owner.refresh_from_db(); reward.refresh_from_db()
        self.assertIsNotNone(reward.used_at)
        self.assertEqual(self.owner.access_expires_at, fulfilled.paid_at + timedelta(days=730))
        self.assertEqual(self.subscribe("year", token).status_code, 409)
        self.assertEqual(plan_config_for_user(self.owner, "year")["days"], 365)
        notice = TelegramOutbox.objects.get(idempotency_key=f"who-update-payment-user-paid:{order.public_id}")
        self.assertIn("<s>1 год</s> 2 года", notice.payload["text"])

    @patch("webhook_tg.wheel_checkout.create_payment", return_value={"id": "test-payment", "confirmation_url": "https://pay.example/test"})
    @patch("webhook_tg.payment_views.get_payment")
    def test_wrong_provider_amount_never_fulfills_or_consumes_reward(self, get_payment, create):
        reward = self.reward()
        self.subscribe()
        order = WhoUpdatePaymentOrder.objects.get()
        get_payment.return_value = {"status": "succeeded", "paid": True, "amount": {"value": "1.00", "currency": "RUB"},
                                   "metadata": {"service": "who_update", "order_id": str(order.public_id)}}
        fulfill_order(order, "test-payment")
        order.refresh_from_db(); reward.refresh_from_db(); self.owner.refresh_from_db()
        self.assertEqual(order.status, "pending")
        self.assertIsNone(reward.used_at)
        self.assertIsNone(self.owner.access_expires_at)
