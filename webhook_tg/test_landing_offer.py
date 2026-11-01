import json
import re
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from .models import WhoUpdateOnboardingFunnel
from .subscriptions import PLAN_CONFIG, TRIAL_DAYS


class WhoUpdateLandingOfferTest(TestCase):
    def test_offer_is_visible_near_first_button_and_uses_current_plans(self):
        response = self.client.get("/", HTTP_HOST="who-update.ru")
        content = response.content.decode()
        hero = content.split('<section class="hero container"', 1)[1].split(
            '<div class="demo"', 1
        )[0]

        self.assertIn(f"{TRIAL_DAYS} дней бесплатно после первого подключения", hero)
        self.assertIn("Без автосписаний", hero)
        self.assertIn("Удалённую до подключения переписку восстановить нельзя", hero)
        self.assertContains(response, 'id="pricing"')
        for fieldToValue in PLAN_CONFIG.values():
            self.assertContains(response, f'{fieldToValue["amount"]:.0f} ₽')
        self.assertContains(response, "повторное подключение не дают новые пробные дни")

    def test_landing_terms_and_schema_share_plan_source_of_truth(self):
        with patch.dict(PLAN_CONFIG, {
            "month": {"label": "1 месяц", "days": 30, "amount": Decimal("137.00")},
        }), patch("webhook_tg.seo_views.TRIAL_DAYS", 9):
            landing = self.client.get("/", HTTP_HOST="who-update.ru")
            terms = self.client.get("/terms/", HTTP_HOST="who-update.ru")

        self.assertContains(landing, "9 дней бесплатно после первого подключения")
        self.assertContains(landing, "137 ₽ в месяц")
        self.assertContains(terms, "пробный период 9 дней")
        self.assertContains(terms, "1 месяц — 137 ₽")
        self.assertNotContains(terms, "129 ₽")
        propertyToValue = json.loads(re.search(
            r'<script type="application/ld\+json">\s*(.*?)\s*</script>',
            landing.content.decode(),
            flags=re.DOTALL,
        ).group(1))
        self.assertEqual(propertyToValue["offers"][0]["price"], "137.00")

    def test_all_signup_links_preserve_tracking_and_instruction_starts_in_bot(self):
        response = self.client.get(
            "/?utm_source=yandex&utm_medium=cpc&yclid=landing-offer-test",
            HTTP_HOST="who-update.ru",
        )
        content = response.content.decode()
        funnel = WhoUpdateOnboardingFunnel.objects.get()
        telegram_links = re.findall(r'href="(https://t\.me/who_update_bot[^\"]*)"', content)

        self.assertTrue(telegram_links)
        self.assertEqual(set(telegram_links), {
            f"https://t.me/who_update_bot?start=trk_{funnel.tracking_code}",
        })
        instruction = content.split('<ol class="setup-steps">', 1)[1].split("</ol>", 1)[0]
        self.assertLess(instruction.index("Запустите бота"), instruction.index("Откройте настройки"))
        self.assertIn("согласившегося на проверку", instruction)

    def test_privacy_describes_current_host_without_inventing_retention_period(self):
        response = self.client.get("/privacy/", HTTP_HOST="who-update.ru")

        self.assertContains(response, "сервере FirstVDS")
        self.assertContains(response, "Яндекс Метрике")
        self.assertNotContains(response, "облачную инфраструктуру")
