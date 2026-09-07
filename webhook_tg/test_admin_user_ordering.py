from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import UserTg


class UserTgAdminOrderingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin_user = get_user_model().objects.create_superuser(
            username="ordering-admin", email="ordering@example.com", password="test-password"
        )
        now = timezone.now()
        cls.never_first = UserTg.objects.create(user_id=10, chat_id=10)
        cls.older = UserTg.objects.create(
            user_id=20, chat_id=20, last_start_at=now - timedelta(days=1)
        )
        cls.newer = UserTg.objects.create(user_id=30, chat_id=30, last_start_at=now)
        cls.never_last = UserTg.objects.create(user_id=40, chat_id=40)

    def setUp(self):
        self.client.force_login(self.admin_user)
        self.url = reverse("admin:webhook_tg_usertg_changelist")

    def changelist(self, params=None):
        response = self.client.get(self.url, params or {})
        self.assertEqual(response.status_code, 200)
        return response.context["cl"]

    def test_default_order_puts_missing_start_dates_last(self):
        self.assertEqual(
            list(self.changelist().result_list),
            [self.newer, self.older, self.never_last, self.never_first],
        )

    def test_sorting_start_column_both_directions_keeps_missing_dates_last(self):
        index = self.changelist().list_display.index("last_start_at")
        for order, dated in (
            (str(index), [self.older, self.newer]),
            (f"-{index}", [self.newer, self.older]),
        ):
            with self.subTest(order=order):
                self.assertEqual(
                    list(self.changelist({"o": order}).result_list),
                    dated + [self.never_last, self.never_first],
                )

    def test_other_column_sorting_is_preserved(self):
        index = self.changelist().list_display.index("user_id")
        self.assertEqual(
            list(self.changelist({"o": str(index)}).result_list),
            [self.never_first, self.older, self.newer, self.never_last],
        )

    def test_unlimited_access_is_not_a_list_column_but_remains_editable(self):
        changelist = self.changelist()
        self.assertNotIn("access_unlimited", changelist.list_display)
        response = self.client.get(
            reverse("admin:webhook_tg_usertg_change", args=[self.older.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("access_unlimited", response.context["adminform"].form.fields)
