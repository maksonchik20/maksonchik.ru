import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (UserTg, WhoUpdatePaymentOrder as Order, WhoUpdatePartnerSettings as Settings,
                     WhoUpdatePartnerProfile as Profile, WhoUpdatePartnerCommission as Commission,
                     WhoUpdatePartnerWithdrawal as Withdrawal, WhoUpdateWithdrawalDraft as Draft, TelegramOutbox, BotChatEvent)
from .partners import (accrue_commission, partner_balance, partner_percent, create_withdrawal,
                       resolve_withdrawals, void_commissions, referral_statistics)
from .partner_bot import handle_money_message, handle_money_callback
from .payment_views import fulfill_order
from .subscriptions import referral_link, register_referral


class PartnerFixtures:
    def setUp(self):
        Settings.objects.update_or_create(pk=1, defaults={"started_at": timezone.now() - timedelta(days=1), "is_active": True, "commission_percent": Decimal("30")})
        self.partner = UserTg.objects.create(user_id=910001, chat_id=910001, username="partner_demo")
        self.buyer = UserTg.objects.create(user_id=910002, chat_id=910002, username="buyer_demo", referred_by=self.partner)
        self.admin = get_user_model().objects.create_superuser("partner-admin", "test@example.com", "test")

    def order(self, amount="100", **kwargs):
        fields = dict(user=self.buyer, plan="month", duration_days=30, amount=Decimal(amount), status=Order.Status.PAID, paid_at=timezone.now())
        fields.update(kwargs)
        return Order.objects.create(**fields)

    def credit(self, amount="100"):
        return accrue_commission(self.order(amount))

    def withdraw(self, amount="10", key=None):
        return create_withdrawal(self.partner, Decimal(amount), request_key=key or uuid.uuid4())


class PartnerTests(PartnerFixtures, TestCase):
    def test_all_users_earn_on_every_purchase_with_actual_amount_and_rounding(self):
        first = self.credit("99")
        second = self.credit("84.15")
        self.assertEqual(first.amount, Decimal("29.70"))
        self.assertEqual(second.amount, Decimal("25.25"))
        self.assertFalse(Profile.objects.exists())
        self.assertEqual(partner_balance(self.partner)["payable"], Decimal("54.95"))
        self.assertEqual(accrue_commission(first.order).pk, first.pk)
        self.assertEqual(Commission.objects.count(), 2)

    def test_rate_snapshot_overrides_and_pauses(self):
        first = self.credit()
        Profile.objects.create(user=self.partner, commission_percent=Decimal("12.5"))
        self.assertEqual(self.credit().amount, Decimal("12.50"))
        first.refresh_from_db()
        self.assertEqual(first.commission_percent, Decimal("30"))
        Profile.objects.filter(user=self.partner).update(is_active=False)
        self.assertIsNone(self.credit())
        Profile.objects.filter(user=self.partner).update(is_active=True, commission_percent=None)
        Settings.objects.filter(pk=1).update(is_active=False)
        self.assertEqual(partner_percent(self.partner), 0)
        self.assertIsNone(self.credit())
        self.assertEqual(self.withdraw("10").amount, Decimal("10"))  # Existing funds remain withdrawable.

    def test_unpaid_test_old_self_and_no_inviter_never_accrue(self):
        for changes in ({"status": "pending"}, {"is_test": True}, {"paid_at": None}, {"paid_at": timezone.now()-timedelta(days=2)}):
            with self.subTest(changes=changes):
                self.assertIsNone(accrue_commission(self.order(**changes)))
        self.buyer.referred_by = None
        self.buyer.save()
        self.assertIsNone(self.credit())
        self.buyer.referred_by = self.buyer
        self.buyer.save()
        self.assertIsNone(self.credit())
        self.buyer.referred_by = self.partner
        self.buyer.user_id = self.partner.user_id
        self.buyer.save()
        self.assertIsNone(self.credit())

    def test_payment_fulfillment_accrues_atomically_once(self):
        order = self.order(status="pending", paid_at=None)
        payload = {"id": "new-payment", "status": "succeeded", "paid": True, "amount": {"value": "100.00", "currency": "RUB"}, "metadata": {"service": "who_update", "order_id": str(order.public_id)}}
        with patch("webhook_tg.payment_views.get_payment", return_value=payload):
            fulfill_order(order, "new-payment")
            fulfill_order(order, "new-payment")
        self.assertEqual(Commission.objects.get().amount, Decimal("30"))
        self.buyer.refresh_from_db()
        expiry = self.buyer.access_expires_at
        with patch("webhook_tg.payment_views.get_payment", return_value=payload):
            fulfill_order(order, "new-payment")
        self.buyer.refresh_from_db()
        self.assertEqual(self.buyer.access_expires_at, expiry)

    def test_commission_failure_rolls_back_payment_and_access(self):
        order = self.order(status="pending", paid_at=None)
        payload = {"status": "succeeded", "paid": True, "amount": {"value": "100.00", "currency": "RUB"}, "metadata": {"service": "who_update", "order_id": str(order.public_id)}}
        before = self.buyer.access_expires_at
        with patch("webhook_tg.payment_views.get_payment", return_value=payload), patch("webhook_tg.partners.accrue_commission", side_effect=RuntimeError("db error")):
            with self.assertRaises(RuntimeError):
                fulfill_order(order, "payment")
        order.refresh_from_db(); self.buyer.refresh_from_db()
        self.assertEqual(order.status, "pending")
        self.assertEqual(self.buyer.access_expires_at, before)

    def test_verified_test_payment_does_not_credit(self):
        order = self.order(status="pending", paid_at=None)
        payload = {"test": True, "status": "succeeded", "paid": True, "amount": {"value": "100.00", "currency": "RUB"}, "metadata": {"service": "who_update", "order_id": str(order.public_id)}}
        with patch("webhook_tg.payment_views.get_payment", return_value=payload):
            fulfill_order(order, "sandbox")
        order.refresh_from_db()
        self.assertTrue(order.is_test)
        self.assertFalse(Commission.objects.exists())

    def test_referral_first_assignment_wins_even_with_stale_instance(self):
        invitee = UserTg.objects.create(user_id=910003, chat_id=910003)
        stale = UserTg.objects.get(pk=invitee.pk)
        self.assertTrue(register_referral(invitee, f"ref_{self.partner.referral_code}"))
        self.assertFalse(register_referral(stale, f"ref_{self.buyer.referral_code}"))
        invitee.refresh_from_db()
        self.assertEqual(invitee.referred_by, self.partner)
        duplicate = UserTg.objects.create(user_id=self.partner.user_id, chat_id=self.partner.chat_id)
        self.assertFalse(register_referral(duplicate, f"ref_{self.partner.referral_code}"))

    def test_partial_withdrawal_reservation_idempotency_and_rejection(self):
        self.credit()
        key = uuid.uuid4()
        first = self.withdraw("10.50", key)
        self.assertEqual(self.withdraw("10.50", key).pk, first.pk)
        self.assertEqual(partner_balance(self.partner), {"earned": Decimal("30"), "reserved": Decimal("10.5"), "paid": 0, "payable": Decimal("19.5")})
        with self.assertRaises(ValueError): self.withdraw("20")
        for invalid in ("0", "-1", "0.001", "NaN", "Infinity"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError): self.withdraw(invalid)
        resolve_withdrawals([first.pk], status="rejected", actor=self.admin, note="По просьбе пользователя")
        self.assertEqual(partner_balance(self.partner)["payable"], 30)
        self.assertEqual(resolve_withdrawals([first.pk], status="paid", actor=self.admin, note="Повтор"), [])
        self.assertEqual(Withdrawal.objects.count(), 1)

    def test_paid_withdrawal_remains_debited_and_cannot_change_again(self):
        self.credit()
        row = self.withdraw("25")
        resolve_withdrawals([row.pk], status="paid", actor=self.admin, note="Перевод №123")
        self.assertEqual(partner_balance(self.partner), {"earned": 30, "reserved": 0, "paid": 25, "payable": 5})
        self.assertEqual(resolve_withdrawals([row.pk], status="rejected", actor=self.admin, note="Повтор"), [])
        row.refresh_from_db()
        self.assertEqual(row.resolved_by, self.admin)
        with self.assertRaises(ValueError): self.withdraw("6")

    def test_void_cannot_remove_reserved_or_paid_funds(self):
        commission = self.credit()
        row = self.withdraw()
        with self.assertRaises(ValueError): void_commissions([commission.pk], actor=self.admin, reason="Возврат")
        resolve_withdrawals([row.pk], status="rejected", actor=self.admin, note="Отмена")
        self.assertEqual(void_commissions([commission.pk], actor=self.admin, reason="Возврат"), [commission.pk])
        self.assertEqual(partner_balance(self.partner)["payable"], 0)
        self.assertEqual(void_commissions([commission.pk], actor=self.admin, reason="Повтор"), [])
        self.assertEqual(accrue_commission(commission.order).status, "void")

    def test_referral_totals_do_not_multiply_payments_by_commissions(self):
        self.credit("100"); self.credit("200")
        self.order("999", is_test=True)
        self.order("500", status="pending")
        self.order("50", paid_at=timezone.now()-timedelta(days=3))
        UserTg.objects.create(user_id=910004, chat_id=910004, referred_by=self.partner)
        refs, summary = referral_statistics(self.partner)
        rows = list(refs)
        self.assertEqual(summary["referral_count"], 2)
        self.assertEqual(summary["revenue"], 350)
        self.assertEqual(summary["earned"], 90)
        self.assertEqual(rows[0].purchase_count, 3)
        self.assertEqual(rows[0].commission_total, 90)
        self.assertEqual(rows[1].purchase_total, 0)

    def test_admin_tabs_pagination_and_filter_links(self):
        self.credit()
        UserTg.objects.bulk_create([UserTg(user_id=920000+i, chat_id=920000+i, referred_by=self.partner) for i in range(51)])
        self.client.force_login(self.admin)
        for name in ("change", "dialog"):
            response = self.client.get(reverse(f"admin:webhook_tg_usertg_{name}", args=[self.partner.pk]), {"tab": "referrals", "ref_page": 2})
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'id="wu-tab-chat"')
            self.assertContains(response, 'id="wu-tab-referrals"')
            self.assertEqual(len(response.context["referral_page"]), 2)
            self.assertEqual(response.context["referral_summary"]["revenue"], 100)
        for model, query in (("whoupdatepaymentorder", {"user__id__exact": self.buyer.pk, "status__exact": "paid", "is_test__exact": "0"}), ("whoupdatepartnercommission", {"partner__id__exact": self.partner.pk, "buyer__id__exact": self.buyer.pk}), ("whoupdatepartnerwithdrawal", {"partner__id__exact": self.partner.pk})):
            response = self.client.get(reverse(f"admin:webhook_tg_{model}_changelist"), query)
            self.assertEqual(response.status_code, 200)

    def test_admin_financial_records_readonly_and_actions_confirmed(self):
        self.credit(); row = self.withdraw()
        self.client.force_login(self.admin)
        url = reverse("admin:webhook_tg_whoupdatepartnerwithdrawal_changelist")
        data = {"action": "mark_paid", "_selected_action": [row.pk]}
        response = self.client.post(url, data)
        self.assertContains(response, "Сначала переведите деньги")
        row.refresh_from_db(); self.assertEqual(row.status, "new")
        self.client.post(url, {**data, "confirm_partner_action": 1, "note": "Банк, перевод №123"})
        row.refresh_from_db(); self.assertEqual(row.status, "paid")
        response = self.client.get(reverse("admin:webhook_tg_whoupdatepartnerwithdrawal_change", args=[row.pk]))
        self.assertEqual(response.context["adminform"].form.fields, {})
        self.assertEqual(self.client.get(reverse("admin:webhook_tg_whoupdatepartnerwithdrawal_add")).status_code, 403)

    def test_staff_cannot_access_partner_accounts_or_dialog(self):
        staff = get_user_model().objects.create_user("staff", is_staff=True)
        self.client.force_login(staff)
        for url in (reverse("admin:webhook_tg_whoupdatepartnerwithdrawal_changelist"), reverse("admin:webhook_tg_whoupdatepartnercommission_changelist"), reverse("admin:webhook_tg_usertg_dialog", args=[self.partner.pk])):
            self.assertEqual(self.client.get(url).status_code, 403)


class MoneyBotTests(PartnerFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.credit("1000")
        self.sequence = 700000
        mock = patch("webhook_tg.partner_bot.answer_callback_query", return_value=True)
        mock.start(); self.addCleanup(mock.stop)

    def message(self, text, user=None, **extra):
        self.sequence += 1
        user = user or self.partner
        data = {"update_id": self.sequence, "message": {"chat": {"id": user.chat_id, "type": "private"}, "from": {"id": user.user_id}, "text": text, **extra}}
        return handle_money_message(data)

    def callback(self, action, token=None, user=None):
        self.sequence += 1
        user = user or self.partner
        token = token or Draft.objects.get(partner=self.partner).token
        return handle_money_callback({"id": str(self.sequence), "data": f"money:{action}:{token.hex}", "from": {"id": user.user_id}, "message": {"chat": {"id": user.chat_id, "type": "private"}}})

    def begin(self):
        self.message("/referral"); self.callback("begin")

    def test_referral_combines_connection_bonus_commission_balance_and_withdrawal(self):
        self.withdraw("40")
        paid = self.withdraw("20")
        resolve_withdrawals([paid.pk], status="paid", actor=self.admin, note="Перевод №123")

        self.assertTrue(self.message("/referral"))

        notice = TelegramOutbox.objects.get()
        self.assertEqual(notice.chat_id, self.partner.chat_id)
        self.assertEqual(notice.method, TelegramOutbox.Method.SEND_MESSAGE)
        self.assertEqual(notice.payload["parse_mode"], "HTML")
        text = notice.payload["text"]
        self.assertIn("+7 дней", text)
        self.assertIn("впервые", text)
        self.assertIn("подключ", text)
        self.assertIn("30%", text)
        self.assertIn("каждой", text)
        self.assertIn("покупки", text)
        self.assertIn("повторной", text)
        self.assertEqual(text.count(referral_link(self.partner)), 1)
        self.assertIn("Начислено: <b>300 ₽</b>", text)
        self.assertIn("Доступно к выводу: <b>240 ₽</b>", text)
        self.assertIn("В заявках: 40 ₽", text)
        self.assertIn("Выплачено: 20 ₽", text)
        self.assertNotIn("/money", text)
        draft = Draft.objects.get(partner=self.partner)
        self.assertFalse(draft.active)
        self.assertEqual(notice.payload["reply_markup"]["inline_keyboard"], [[{
            "text": "Создать заявку на вывод", "callback_data": f"money:begin:{draft.token.hex}",
        }]])

    def test_referral_keeps_configured_commission_rates_and_connection_bonus(self):
        Settings.objects.filter(pk=1).update(commission_percent=Decimal("20"))
        self.message("/referral")
        text = TelegramOutbox.objects.latest("id").payload["text"]
        self.assertIn("20%", text)
        self.assertIn("+7 дней", text)
        self.assertNotIn("30%", text)

        Profile.objects.create(user=self.partner, commission_percent=Decimal("12.5"))
        self.message("/referral")
        text = TelegramOutbox.objects.latest("id").payload["text"]
        self.assertIn("12,5%", text)
        self.assertIn("+7 дней", text)
        self.assertNotIn("20%", text)
        self.assertIn("Доступно к выводу: <b>300 ₽</b>", text)

    def test_referral_keeps_connection_bonus_and_existing_funds_when_commissions_pause(self):
        for pause in ("global", "partner"):
            with self.subTest(pause=pause):
                Settings.objects.filter(pk=1).update(is_active=pause != "global")
                Profile.objects.update_or_create(user=self.partner, defaults={"is_active": pause != "partner"})
                self.assertTrue(self.message("/referral"))
                text = TelegramOutbox.objects.latest("id").payload["text"]
                self.assertIn("+7 дней", text)
                self.assertIn("приостановлено", text)
                self.assertNotIn("30%", text)
                self.assertIn("Доступно к выводу: <b>300 ₽</b>", text)
                self.callback("begin")
                self.assertTrue(Draft.objects.get(partner=self.partner).active)

    def test_referral_aliases_open_the_same_panel(self):
        for command in ("/ref", "/referral@who_update_bot", "/ref@who_update_bot"):
            with self.subTest(command=command):
                self.assertTrue(self.message(command))
                notice = TelegramOutbox.objects.latest("id")
                self.assertIn("+7 дней", notice.payload["text"])
                self.assertIn("30%", notice.payload["text"])
                self.assertIn(referral_link(self.partner), notice.payload["text"])
                self.assertIn("reply_markup", notice.payload)

    def test_removed_money_command_does_not_open_the_panel(self):
        self.assertFalse(self.message("/money"))
        self.assertFalse(self.message("/money@who_update_bot"))
        self.assertFalse(TelegramOutbox.objects.exists())
        self.assertFalse(Draft.objects.exists())

    def test_referral_full_flow_reserves_only_after_yes_and_deduplicates(self):
        self.begin(); self.message("120,50")
        token = Draft.objects.get(partner=self.partner).token
        self.assertFalse(Withdrawal.objects.exists())
        self.assertIn("120,5 рублей", TelegramOutbox.objects.latest("id").payload["text"])
        buttons = TelegramOutbox.objects.latest("id").payload["reply_markup"]["inline_keyboard"][0]
        self.assertEqual([b["text"] for b in buttons], ["Да", "Отменить"])
        self.callback("yes", token); self.callback("yes", token)
        self.assertEqual(Withdrawal.objects.count(), 1)
        self.assertEqual(partner_balance(self.partner)["payable"], Decimal("179.50"))
        self.assertFalse(Draft.objects.get(partner=self.partner).active)
        for notice in TelegramOutbox.objects.filter(idempotency_key__startswith="money:callback:").order_by("id")[1:]:
            self.assertIn("/referral", notice.payload["text"])
            self.assertNotIn("/money", notice.payload["text"])

    def test_cancel_invalid_amounts_and_old_buttons(self):
        self.begin()
        for value in ("текст", "0", "-1", "1.001", "301", "1e2", "NaN"):
            self.message(value)
            self.assertIsNone(Draft.objects.get(partner=self.partner).amount)
        self.message("100"); old = Draft.objects.get(partner=self.partner).token
        self.message("200"); current = Draft.objects.get(partner=self.partner).token
        self.callback("yes", old)
        self.assertFalse(Withdrawal.objects.exists())
        self.callback("cancel", current); self.callback("yes", current)
        self.assertFalse(Withdrawal.objects.exists())
        self.assertEqual(partner_balance(self.partner)["payable"], 300)
        self.assertIn("/referral", TelegramOutbox.objects.latest("id").payload["text"])

    def test_other_user_cannot_confirm_and_balance_is_rechecked(self):
        self.begin(); self.message("300")
        token = Draft.objects.get(partner=self.partner).token
        self.callback("yes", token, user=self.buyer)
        self.assertFalse(Withdrawal.objects.exists())
        self.withdraw("10")
        self.callback("yes", token)
        self.assertEqual(Withdrawal.objects.count(), 1)
        self.assertIn("Недостаточно", TelegramOutbox.objects.latest("id").payload["text"])

    def test_commands_expiry_restart_and_non_private_messages(self):
        self.begin(); self.message("10")
        token = Draft.objects.get(partner=self.partner).token
        self.assertFalse(self.message("/status"))
        self.callback("yes", token)
        self.assertFalse(Withdrawal.objects.exists())
        self.begin(); self.message("10")
        Draft.objects.filter(partner=self.partner).update(updated_at=timezone.now()-timedelta(hours=1))
        self.callback("yes")
        self.assertFalse(Withdrawal.objects.exists())
        self.assertIn("/referral", TelegramOutbox.objects.latest("id").payload["text"])
        count = TelegramOutbox.objects.count()
        for command in ("/referral", "/ref"):
            self.assertFalse(self.message(command, business_connection_id="business"))
            self.assertFalse(self.message(command, chat={"id": -1, "type": "group"}))
            self.assertFalse(self.message(command, chat={"id": self.buyer.chat_id, "type": "private"}))
        self.assertEqual(TelegramOutbox.objects.count(), count)

    def test_cancel_returns_to_referral_panel(self):
        self.begin()
        self.assertTrue(self.message("/cancel"))
        self.assertFalse(Draft.objects.get(partner=self.partner).active)
        text = TelegramOutbox.objects.latest("id").payload["text"]
        self.assertIn("/referral", text)
        self.assertNotIn("/money", text)

    def test_old_withdrawal_button_in_group_points_to_private_referral(self):
        with patch("webhook_tg.partner_bot.answer_callback_query") as answer:
            self.assertTrue(handle_money_callback({
                "id": "group-callback", "data": f"money:begin:{uuid.uuid4().hex}",
                "from": {"id": self.partner.user_id}, "message": {"chat": {"id": -1, "type": "group"}},
            }))
        answer.assert_called_once()
        self.assertIn("/referral", answer.call_args.args[1])
        self.assertNotIn("/money", answer.call_args.args[1])
        self.assertFalse(TelegramOutbox.objects.exists())
        self.assertFalse(Draft.objects.exists())

    def test_update_retry_does_not_reset_a_draft(self):
        self.begin(); self.message("10")
        token = Draft.objects.get(partner=self.partner).token
        self.sequence -= 1
        self.message("10")
        self.assertEqual(Draft.objects.get(partner=self.partner).token, token)

    def test_referral_reaches_main_dispatcher_once_without_active_access(self):
        from .views import process_telegram_update
        self.partner.access_unlimited = False
        self.partner.access_expires_at = timezone.now()-timedelta(days=1)
        self.partner.save()
        for update_id, command in enumerate(("/referral", "/ref"), start=780001):
            with self.subTest(command=command), patch("webhook_tg.views.send_message_reliably") as send:
                count = TelegramOutbox.objects.count()
                data = {"update_id": update_id, "message": {"message_id": update_id, "chat": {"id": self.partner.chat_id, "type": "private"}, "from": {"id": self.partner.user_id}, "text": command}}
                process_telegram_update(data, use_idempotency=False)
                process_telegram_update(data, use_idempotency=False)
                self.assertEqual(TelegramOutbox.objects.count(), count + 1)
                notice = TelegramOutbox.objects.get(idempotency_key=f"money:message:{update_id}")
                self.assertIn("+7 дней", notice.payload["text"])
                self.assertIn("30%", notice.payload["text"])
                self.assertIn("reply_markup", notice.payload)
                self.assertTrue(BotChatEvent.objects.filter(chat_id=self.partner.chat_id, text=command).exists())
                send.assert_not_called()

    def test_dispatcher_has_no_legacy_money_or_non_private_referral_panel(self):
        from .views import process_telegram_update
        cases = (
            ("/money", {"id": self.partner.chat_id, "type": "private"}),
            ("/referral", {"id": -1, "type": "group"}),
            ("/ref", {"id": self.buyer.chat_id, "type": "private"}),
        )
        for update_id, (command, chat) in enumerate(cases, start=790001):
            with self.subTest(command=command, chat=chat), patch("webhook_tg.views.send_message_reliably") as send, patch("webhook_tg.views.create_message"):
                process_telegram_update({
                    "update_id": update_id,
                    "message": {"message_id": update_id, "chat": chat, "from": {"id": self.partner.user_id}, "text": command},
                }, use_idempotency=False)
                send.assert_not_called()
                self.assertFalse(TelegramOutbox.objects.exists())
                self.assertFalse(Draft.objects.exists())
        self.partner.refresh_from_db()
        self.assertEqual(self.partner.chat_id, self.partner.user_id)


from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from django.db import connection, connections
from django.test import TransactionTestCase
from unittest import skipUnless


@skipUnless(connection.vendor == "postgresql", "Row-lock concurrency requires PostgreSQL")
class PartnerConcurrencyTests(PartnerFixtures, TransactionTestCase):
    def concurrent(self, work):
        barrier = Barrier(2)
        def run():
            try:
                barrier.wait(timeout=5)
                return work()
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run) for _ in range(2)]
            return [future.result(timeout=15) for future in futures]

    def test_parallel_requests_cannot_overdraw_balance(self):
        self.credit()
        def work():
            try:
                self.withdraw("20")
                return "created"
            except ValueError:
                return "insufficient"
        self.assertCountEqual(self.concurrent(work), ["created", "insufficient"])
        self.assertEqual(partner_balance(self.partner)["payable"], 10)

    def test_parallel_confirmations_and_payment_retries_are_idempotent(self):
        order = self.order()
        ids = self.concurrent(lambda: accrue_commission(order).pk)
        self.assertEqual(len(set(ids)), 1)
        key = uuid.uuid4()
        ids = self.concurrent(lambda: self.withdraw("20", key).pk)
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(Withdrawal.objects.count(), 1)
