from decimal import Decimal
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from .models import UserTg,WhoUpdateWheelReward,WhoUpdatePaymentOrder
from .subscriptions import PLAN_CONFIG,plan_config_for_user,checkout_token,subscription_keyboard
from .wheel import spin_day
from .wheel_views import state


class PriceUpdateTests(TestCase):
    def setUp(self):
        self.user=UserTg.objects.create(user_id=9991,chat_id=9991,access_unlimited=False)

    def reward(self,prize):
        return WhoUpdateWheelReward.objects.create(user=self.user,prize=prize,spin_day=spin_day(),expires_at=timezone.now()+timedelta(hours=24))

    def test_new_prices_in_state_and_buttons(self):
        self.assertEqual({key:fieldToValue['amount'] for key,fieldToValue in PLAN_CONFIG.items()},{'month':Decimal('129.00'),'three_months':Decimal('249.00'),'year':Decimal('800.00')})
        fieldToValue=state(self.user)
        self.assertEqual([planFieldToValue['amount'] for planFieldToValue in fieldToValue['plans']],['129.00','249.00','800.00'])
        self.assertEqual([row[0]['text'] for row in subscription_keyboard(self.user)['inline_keyboard'][:3]],['1 месяц — 129 ₽','3 месяца — 249 ₽','1 год — 800 ₽'])

    @override_settings(ALLOWED_HOSTS=['who-update.ru'])
    def test_terms_and_structured_prices_are_updated(self):
        response=self.client.get('/terms/',HTTP_HOST='who-update.ru')
        self.assertContains(response,'1 месяц — 129 ₽')
        self.assertContains(response,'3 месяца — 249 ₽')
        self.assertContains(response,'1 год — 800 ₽')
        landing=self.client.get('/',HTTP_HOST='who-update.ru')
        self.assertContains(landing,'"price": "129"')
        self.assertContains(landing,'"price": "249"')
        self.assertContains(landing,'"price": "800"')

    def test_new_discount_amounts_and_double_duration(self):
        for prize,month,quarter,year in [('discount10','116.10','224.10','720.00'),('discount15','109.65','211.65','680.00'),('discount20','103.20','199.20','640.00'),('double','129.00','249.00','800.00')]:
            reward=self.reward(prize)
            self.assertEqual(plan_config_for_user(self.user,'month')['amount'],Decimal(month))
            self.assertEqual(plan_config_for_user(self.user,'month')['days'],60 if prize=='double' else 30)
            self.assertEqual(plan_config_for_user(self.user,'three_months')['amount'],Decimal(quarter))
            self.assertEqual(plan_config_for_user(self.user,'year')['amount'],Decimal(year))
            self.assertEqual(plan_config_for_user(self.user,'year')['days'],730 if prize=='double' else 365)
            reward.delete()

    @patch('webhook_tg.wheel_checkout.create_payment')
    def test_old_reserved_discount_order_keeps_price_on_screen_and_checkout(self,create):
        reward=self.reward('discount10')
        order=WhoUpdatePaymentOrder.objects.create(user=self.user,plan='year',amount=Decimal('539.10'),duration_days=365)
        reward.reserved_order=order
        reward.confirmation_url='https://pay.example/existing'
        reward.save(update_fields=['reserved_order','confirmation_url'])
        config=plan_config_for_user(self.user,'year')
        self.assertEqual(config['amount'],Decimal('539.10'))
        self.assertEqual(config['original_amount'],Decimal('800.00'))
        self.assertEqual(plan_config_for_user(self.user,'three_months')['amount'],Decimal('224.10'))
        self.assertEqual(state(self.user)['plans'][2]['amount'],'539.10')
        response=self.client.get(f'/bot/subscribe/year/{checkout_token(self.user,"year")}/')
        self.assertContains(response,'https://pay.example/existing')
        create.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.amount,Decimal('539.10'))

    @patch('webhook_tg.payment_views.create_payment',return_value={'id':'price-test','confirmation_url':'https://pay.example/new'})
    def test_new_year_payment_uses_800(self,create):
        response=self.client.get(f'/bot/subscribe/year/{checkout_token(self.user,"year")}/')
        self.assertContains(response,'https://pay.example/new')
        self.assertEqual(WhoUpdatePaymentOrder.objects.get().amount,Decimal('800.00'))
        self.assertEqual(create.call_args.kwargs['amount'],Decimal('800.00'))
