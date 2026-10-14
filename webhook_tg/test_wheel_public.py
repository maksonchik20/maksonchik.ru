from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from .models import UserTg, WhoUpdateWheelReward, WhoUpdatePaymentOrder, TelegramOutbox
from .test_wheel import init_data
from .wheel import PRIZES, spin, active_reward, reset_spin, is_pilot
from .wheel_announcement import announcement_payload
from .subscriptions import checkout_token
from .views import process_telegram_update


class PublicWheelTests(TestCase):
    def setUp(self):
        self.user = UserTg.objects.create(user_id=4567,chat_id=4567,access_unlimited=False,access_expires_at=timezone.now()+timedelta(days=2))

    def test_signed_nonowner_state_and_daily_spin(self):
        auth = 'tma '+init_data(self.user.user_id)
        response = self.client.post('/bot/wheel/api/state/',HTTP_AUTHORIZATION=auth)
        self.assertEqual(response.status_code,200)
        self.assertIsNone(response.json()['reward'])
        with patch('webhook_tg.wheel.choose_prize',return_value=PRIZES[3]):
            response = self.client.post('/bot/wheel/api/spin/',HTTP_AUTHORIZATION=auth)
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()['reward']['status'],'granted')
            self.assertEqual(response.json()['reward']['granted_days'],3)
            self.assertFalse(response.json()['can_spin'])
            self.client.post('/bot/wheel/api/spin/',HTTP_AUTHORIZATION=auth)
        self.assertEqual(WhoUpdateWheelReward.objects.count(),1)
        self.assertEqual(WhoUpdatePaymentOrder.objects.count(),0)
        reward = WhoUpdateWheelReward.objects.get()
        self.assertTrue(reset_spin(reward))
        self.assertFalse(reset_spin(reward))

    @patch('webhook_tg.wheel_checkout.create_payment',return_value={'id':'public-test','confirmation_url':'https://pay.example/test'})
    def test_public_discount_checkout_and_cross_user_reward_isolation(self,create):
        with patch('webhook_tg.wheel.choose_prize',return_value=PRIZES[1]):
            reward = spin(self.user)
        other = UserTg.objects.create(user_id=5678,chat_id=5678,access_unlimited=False)
        self.assertIsNone(active_reward(other))
        token = checkout_token(self.user,'month')
        self.assertContains(self.client.get(f'/bot/subscribe/month/{token}/'),'https://pay.example/test')
        order = WhoUpdatePaymentOrder.objects.get()
        self.assertEqual(order.user,self.user)
        self.assertEqual(order.amount,Decimal('109.65'))
        self.assertEqual(order.duration_days,30)
        self.assertEqual(create.call_count,1)
        from .wheel_checkout import checkout
        self.assertEqual(checkout(other,'month',reward.pk).status_code,409)
        self.assertEqual(create.call_count,1)

    def test_group_chats_and_unknown_id_are_not_eligible(self):
        group = UserTg.objects.create(user_id=9876,chat_id=-777)
        self.assertFalse(is_pilot(group))
        self.assertEqual(self.client.post('/bot/wheel/api/state/',HTTP_AUTHORIZATION='tma '+init_data(9876)).status_code,403)
        with self.assertRaises(PermissionError): spin(group)

    @patch('webhook_tg.views.log_bot_incoming')
    @patch('webhook_tg.views.send_message_reliably')
    def test_spin_command_just_sends_announcement_with_button(self,send,log):
        before = self.user.access_expires_at
        for text in ['/spin','/spin@who_update_bot']:
            process_telegram_update({'update_id':991,'message':{'message_id':1,'date':int(timezone.now().timestamp()),'from':{'id':4567},'chat':{'id':4567,'type':'private'},'text':text}},use_idempotency=False)
            self.assertEqual(send.call_args.args,(4567,announcement_payload()['text']))
            self.assertEqual(send.call_args.kwargs['reply_markup'],announcement_payload()['reply_markup'])
        self.user.refresh_from_db()
        self.assertEqual(self.user.access_expires_at,before)
        self.assertFalse(WhoUpdateWheelReward.objects.exists())

    def test_broadcast_is_dry_run_by_default_and_idempotent(self):
        UserTg.objects.create(user_id=9876,chat_id=-999)
        UserTg.objects.create(user_id=4567,chat_id=4567)  # Legacy duplicate row.
        call_command('announce_wheel',stdout=StringIO())
        self.assertFalse(TelegramOutbox.objects.exists())
        call_command('announce_wheel',send=True,stdout=StringIO())
        call_command('announce_wheel',send=True,stdout=StringIO())
        notice = TelegramOutbox.objects.get()
        self.assertEqual(notice.chat_id,4567)
        self.assertEqual(notice.payload,announcement_payload())
