from datetime import datetime, timedelta, timezone

from django.test import TestCase

from .models import UserTg
from .subscriptions import register_referral
from .tests import NoTelegramApiTestCase, make_start_payload


CONNECTED_AT = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)


class ReferralEligibilityTests(TestCase):
    def setUp(self):
        self.inviter = UserTg.objects.create(user_id=810001, chat_id=810001)
        self.invitee = UserTg.objects.create(user_id=810002, chat_id=810002)
        self.payload = f"ref_{self.inviter.referral_code}"

    def test_new_user_can_register_referral(self):
        self.assertTrue(register_referral(self.invitee, self.payload))

        self.invitee.refresh_from_db()
        self.assertEqual(self.invitee.referred_by_id, self.inviter.pk)

    def test_prior_start_without_connection_does_not_prevent_referral(self):
        self.invitee.last_start_at = CONNECTED_AT
        self.invitee.trial_started_at = CONNECTED_AT
        self.invitee.save(update_fields=["last_start_at", "trial_started_at"])

        self.assertTrue(register_referral(self.invitee, self.payload))

        self.invitee.refresh_from_db()
        self.assertEqual(self.invitee.referred_by_id, self.inviter.pk)

    def test_each_connection_marker_prevents_referral_even_with_stale_instance(self):
        for field, value in (
            ("business_is_connected", True),
            ("business_connected_at", CONNECTED_AT),
            ("business_disconnected_at", CONNECTED_AT),
            ("business_connection_id", "legacy-connection"),
        ):
            with self.subTest(field=field):
                invitee = UserTg.objects.create(user_id=810003, chat_id=810003)
                UserTg.objects.filter(pk=invitee.pk).update(**{field: value})

                self.assertFalse(register_referral(invitee, self.payload))
                self.assertIsNone(invitee.referred_by_id)
                invitee.refresh_from_db()
                self.assertIsNone(invitee.referred_by_id)

    def test_disconnected_user_cannot_register_referral(self):
        self.invitee.business_is_connected = False
        self.invitee.business_connected_at = CONNECTED_AT
        self.invitee.business_disconnected_at = CONNECTED_AT + timedelta(days=1)
        self.invitee.save(update_fields=[
            "business_is_connected", "business_connected_at", "business_disconnected_at",
        ])

        self.assertFalse(register_referral(self.invitee, self.payload))

        self.invitee.refresh_from_db()
        self.assertIsNone(self.invitee.referred_by_id)
        self.assertIsNone(self.invitee.referral_rewarded_at)
        self.inviter.refresh_from_db()
        self.assertEqual(self.inviter.referral_bonus_days, 0)

    def test_existing_inviter_is_preserved_after_connection(self):
        stale = UserTg.objects.get(pk=self.invitee.pk)
        self.assertTrue(register_referral(self.invitee, self.payload))
        UserTg.objects.filter(pk=self.invitee.pk).update(
            business_is_connected=True, business_connected_at=CONNECTED_AT,
        )
        other_inviter = UserTg.objects.create(user_id=810004, chat_id=810004)

        self.assertFalse(register_referral(stale, f"ref_{other_inviter.referral_code}"))

        self.assertEqual(stale.referred_by_id, self.inviter.pk)
        self.invitee.refresh_from_db()
        self.assertEqual(self.invitee.referred_by_id, self.inviter.pk)


class ReferralStartEligibilityTests(NoTelegramApiTestCase):
    def test_referral_start_does_not_attribute_previously_connected_users(self):
        inviter = UserTg.objects.create(user_id=820001, chat_id=820001)
        for index, connected in enumerate((True, False)):
            with self.subTest(connected=connected):
                user_id = 820002 + index
                invitee = UserTg.objects.create(
                    user_id=user_id,
                    chat_id=user_id,
                    business_is_connected=connected,
                    business_connected_at=CONNECTED_AT,
                    business_disconnected_at=None if connected else CONNECTED_AT + timedelta(days=1),
                    access_unlimited=False,
                    trial_started_at=CONNECTED_AT,
                    access_expires_at=CONNECTED_AT + timedelta(days=7),
                )
                payload = make_start_payload(
                    chat_id=user_id, user_id=user_id, update_id=820100 + index,
                )
                payload["message"]["text"] = f"/start ref_{inviter.referral_code}"

                response = self.client.post(
                    "/webhook_tg/", data=payload, content_type="application/json",
                )

                self.assertEqual(response.status_code, 200)
                invitee.refresh_from_db()
                self.assertIsNone(invitee.referred_by_id)
                self.assertIsNone(invitee.referral_rewarded_at)
                self.assertIsNotNone(invitee.last_start_at)
        inviter.refresh_from_db()
        self.assertEqual(inviter.referral_bonus_days, 0)
