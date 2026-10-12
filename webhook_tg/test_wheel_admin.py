from datetime import timedelta
from decimal import Decimal

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.admin.models import LogEntry, CHANGE
from django.test import TestCase, RequestFactory
from django.urls import reverse
from django.utils import timezone

from .models import UserTg, WhoUpdatePaymentOrder, WhoUpdateWheelReward
from .wheel import spin_day, spin, reset_spin
from .wheel_views import state


class WheelRewardAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = get_user_model().objects.create_superuser("wheel-admin", "admin@example.com", "test-password")
        cls.now = timezone.now()
        cls.winner = UserTg.objects.create(user_id=1394340082, chat_id=1394340082, username="wheel_owner", first_name="Максим")
        cls.old = WhoUpdateWheelReward.objects.create(user=cls.winner, spin_day=spin_day()-timedelta(days=1), prize="days3", expires_at=cls.now+timedelta(hours=1))
        cls.current = WhoUpdateWheelReward.objects.create(user=cls.winner, spin_day=spin_day(), prize="double", expires_at=cls.now+timedelta(hours=24))
        cls.samples = {"superseded": cls.old, "active": cls.current}
        for index, status in enumerate(["used", "expired", "reserved"]):
            user = UserTg.objects.create(user_id=100+index, chat_id=100+index)
            reward = WhoUpdateWheelReward.objects.create(user=user, spin_day=spin_day(), prize="discount10", expires_at=cls.now-timedelta(hours=1), used_at=cls.now if status=="used" else None)
            if status=="reserved":
                order = WhoUpdatePaymentOrder.objects.create(user=user, plan="month", duration_days=30, amount=Decimal("89.10"))
                reward.reserved_order = order
                reward.save(update_fields=["reserved_order"])
                # A reserved historical prize must not be labelled superseded.
                reward.spin_day -= timedelta(days=1)
                reward.save(update_fields=["spin_day"])
                WhoUpdateWheelReward.objects.create(user=user, spin_day=spin_day(), prize="days7", expires_at=cls.now+timedelta(hours=24))
            cls.samples[status] = reward

    def setUp(self):
        self.client.force_login(self.staff)
        self.url = reverse("admin:webhook_tg_whoupdatewheelreward_changelist")

    def test_list_shows_winner_prize_dates_and_links(self):
        response = self.client.get(self.url)
        self.assertContains(response, "Выигрыши колеса")
        self.assertContains(response, "@wheel_owner")
        self.assertContains(response, "×2 срока")
        self.assertContains(response, "1394340082")
        self.assertContains(response, "Дата выигрыша")
        self.assertContains(response, reverse("admin:webhook_tg_usertg_change", args=[self.winner.pk]))
        rows = list(response.context["cl"].result_list)
        self.assertEqual(rows, sorted(rows, key=lambda row: (row.created_at,row.pk), reverse=True))

    def test_status_filters_include_reserved_historical_rewards(self):
        for status, reward in self.samples.items():
            response = self.client.get(self.url, {"reward_status":status})
            self.assertEqual(response.status_code, 200)
            ids = [row.pk for row in response.context["cl"].result_list]
            self.assertIn(reward.pk, ids)
            self.assertTrue(all(row._reward_status==status for row in response.context["cl"].result_list))

    def test_search_by_username_name_and_telegram_id_and_filter_prize(self):
        for query in ["wheel_owner", "Максим", "1394340082"]:
            response = self.client.get(self.url, {"q":query,"prize":"double"})
            self.assertEqual(list(response.context["cl"].result_list), [self.current])

    def test_free_grant_has_own_status_and_readonly_receipt(self):
        from .wheel import grant_instant_days
        self.assertTrue(grant_instant_days(self.old))
        response = self.client.get(self.url, {"reward_status": "granted"})
        self.assertEqual([row.pk for row in response.context["cl"].result_list], [self.old.pk])
        self.assertContains(response, "Начислен бесплатно")
        detail = self.client.get(reverse("admin:webhook_tg_whoupdatewheelreward_change", args=[self.old.pk]))
        self.assertContains(detail, "Дней начислено без оплаты")
        self.assertContains(detail, "Доступ после начисления")

    def test_audit_log_is_read_only_even_for_superuser(self):
        url = reverse("admin:webhook_tg_whoupdatewheelreward_change", args=[self.current.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="_save"')
        self.assertNotContains(response, 'class="deletelink"')
        self.assertEqual(self.client.post(url, {"prize":"discount20"}).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:webhook_tg_whoupdatewheelreward_add")).status_code, 403)
        self.assertEqual(self.client.post(reverse("admin:webhook_tg_whoupdatewheelreward_delete", args=[self.current.pk]), {"post":"yes"}).status_code, 403)
        self.current.refresh_from_db()
        self.assertEqual(self.current.prize, "double")

    def test_anonymous_and_unprivileged_staff_cannot_view_winners(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)
        staff = get_user_model().objects.create_user("no-permission", is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_related_users_orders_and_statuses_do_not_create_n_plus_one_queries(self):
        model_admin = admin.site._registry[WhoUpdateWheelReward]
        request = RequestFactory().get(self.url)
        with self.assertNumQueries(1):
            rows = list(model_admin.get_queryset(request))
            for row in rows:
                model_admin.winner(row)
                model_admin.payment_link(row)
                model_admin.reward_status(row)

    def reset_action(self, reward):
        return self.client.post(self.url, {"action":"reset_daily_spin", "_selected_action":[str(reward.pk)], "index":"0"})

    def test_reset_action_allows_exactly_one_new_spin_and_logs_change(self):
        original_day, original_prize = self.current.spin_day, self.current.prize
        self.assertFalse(state(self.winner)["can_spin"])
        self.assertEqual(self.reset_action(self.current).status_code, 302)
        self.current.refresh_from_db()
        self.assertIsNotNone(self.current.spin_reset_at)
        self.assertEqual((self.current.spin_day,self.current.prize), (original_day,original_prize))
        self.assertTrue(state(self.winner)["can_spin"])
        self.assertEqual(LogEntry.objects.filter(object_id=str(self.current.pk), action_flag=CHANGE).count(), 1)
        # Repeating the same submitted form is idempotent.
        self.reset_action(self.current)
        self.assertEqual(LogEntry.objects.filter(object_id=str(self.current.pk), action_flag=CHANGE).count(), 1)
        new = spin(self.winner)
        self.assertNotEqual(new.pk, self.current.pk)
        self.assertEqual(new.spin_day, original_day)
        self.assertFalse(state(self.winner)["can_spin"])
        self.assertEqual(spin(self.winner).pk,new.pk)
        self.reset_action(self.current)  # An old form must not reset the new spin.
        self.assertFalse(state(self.winner)["can_spin"])
        self.reset_action(new)
        self.assertTrue(state(self.winner)["can_spin"])

    def test_reset_does_not_change_reserved_order_or_access(self):
        order = WhoUpdatePaymentOrder.objects.create(user=self.winner, plan="month", amount=Decimal("99.00"), duration_days=60)
        self.current.reserved_order = order
        self.current.confirmation_url = "https://pay.example/reserved"
        self.current.save(update_fields=["reserved_order","confirmation_url"])
        old_expiry = self.winner.access_expires_at
        self.reset_action(self.current)
        self.current.refresh_from_db();order.refresh_from_db();self.winner.refresh_from_db()
        self.assertEqual(self.current.reserved_order_id,order.pk)
        self.assertEqual(self.current.confirmation_url,"https://pay.example/reserved")
        self.assertEqual((order.amount,order.duration_days,order.status),(Decimal("99.00"),60,"pending"))
        self.assertEqual(self.winner.access_expires_at,old_expiry)

    def test_old_rewards_cannot_be_reset(self):
        self.assertFalse(reset_spin(self.old))
        self.assertFalse(reset_spin(self.samples["reserved"]))
        self.assertFalse(state(self.winner)["can_spin"])

    def test_view_only_staff_cannot_invoke_reset_action(self):
        staff = get_user_model().objects.create_user("reward-viewer",is_staff=True)
        staff.user_permissions.add(Permission.objects.get(codename="view_whoupdatewheelreward"))
        self.client.force_login(staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code,200)
        self.assertNotContains(response,"Сбросить дату вращения")
        self.reset_action(self.current)
        self.current.refresh_from_db()
        self.assertIsNone(self.current.spin_reset_at)
        request = RequestFactory().post(self.url)
        request.user = staff
        self.assertEqual(admin.site._registry[WhoUpdateWheelReward].reset_daily_spin(request,WhoUpdateWheelReward.objects.all()).status_code,403)
