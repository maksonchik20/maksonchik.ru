from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from .models import UserTg


class SubscriptionRolloutCommandTests(TestCase):
    def test_rollout_recalculates_access_for_every_existing_user(self):
        before = timezone.now()
        recent_start = before - timedelta(days=3)
        old_start = before - timedelta(days=20)
        recent = UserTg.objects.create(
            user_id=1,
            chat_id=1,
            last_start_at=recent_start,
        )
        old = UserTg.objects.create(
            user_id=2,
            chat_id=2,
            last_start_at=old_start,
        )
        missing = UserTg.objects.create(user_id=3, chat_id=3)

        call_command("rollout_who_update_subscriptions", "--apply", stdout=StringIO())
        after = timezone.now()

        recent.refresh_from_db()
        old.refresh_from_db()
        missing.refresh_from_db()
        self.assertFalse(recent.access_unlimited)
        self.assertEqual(recent.trial_started_at, recent_start)
        self.assertEqual(recent.access_expires_at, recent_start + timedelta(days=14))
        self.assertIsNone(recent.access_expired_notified_at)

        for expired in (old, missing):
            self.assertFalse(expired.access_unlimited)
            self.assertGreaterEqual(expired.access_expires_at, before)
            self.assertLessEqual(expired.access_expires_at, after)
            self.assertEqual(expired.access_expired_notified_at, expired.access_expires_at)

        self.assertEqual(old.trial_started_at, old_start)
        self.assertGreaterEqual(missing.trial_started_at, before)
        self.assertLessEqual(missing.trial_started_at, after)

    def test_dry_run_does_not_change_users(self):
        user = UserTg.objects.create(user_id=4, chat_id=4)
        call_command("rollout_who_update_subscriptions", stdout=StringIO())
        user.refresh_from_db()
        self.assertTrue(user.access_unlimited)
