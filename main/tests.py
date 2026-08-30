from unittest.mock import patch

from django.contrib import admin
from django.conf import settings
from django.test import RequestFactory, TestCase

from .models import Lead


class LeadFormTest(TestCase):
    @patch("main.views.tg_send_message", return_value=True)
    def test_contact_only_submission_is_accepted(self, send_message):
        response = self.client.post("/request/", {"contact": "@anna", "consent": "on"})
        self.assertEqual(response.status_code, 200)
        lead = Lead.objects.get()
        self.assertEqual(response.json()["lead_id"], lead.pk)
        self.assertEqual(lead.name, "")
        self.assertEqual(lead.message, "")
        self.assertIn("Не указано", send_message.call_args.args[1])

    def test_honeypot_does_not_create_a_conversion(self):
        response = self.client.post("/request/", {"company": "spam"})
        self.assertEqual(response.json(), {"ok": True})
        self.assertFalse(Lead.objects.exists())

    def test_contact_and_consent_are_independently_required(self):
        for data in ({"contact": "@anna"}, {"contact": "  ", "consent": "on"}):
            with self.subTest(data=data):
                self.assertEqual(self.client.post("/request/", data).status_code, 400)
        self.assertFalse(Lead.objects.exists())

    def test_optional_fields_still_have_length_limits(self):
        for extra in ({"name": "a" * 121}, {"message": "a" * 3001}):
            data = {"contact": "@anna", "consent": "on", **extra}
            self.assertEqual(self.client.post("/request/", data).status_code, 400)
        self.assertFalse(Lead.objects.exists())

    def test_home_has_estimate_cta_and_one_short_form(self):
        response = self.client.get("/")
        self.assertContains(response, 'href="#request" data-lead-open')
        self.assertContains(response, "Узнать стоимость и сроки")
        self.assertContains(response, "от 15&nbsp;000&nbsp;₽")
        self.assertContains(response, 'id="lead-form"', count=1)
        self.assertContains(response, 'id="lead-dialog"')
        self.assertContains(response, 'placeholder="Имя">')
        self.assertContains(response, 'main/lead-form.js')

    def test_home_places_lead_form_before_telegram_contact_section(self):
        response = self.client.get("/")
        html = response.content.decode()
        self.assertLess(html.index('id="request"'), html.index('id="contact"'))
        self.assertLess(html.index('id="lead-form"'), html.index("Обсудим ваш проект?"))
        self.assertContains(response, 'id="lead-form"', count=1)
        self.assertContains(response, 'id="contact"', count=1)

    @patch("main.views.tg_send_message", return_value=True)
    def test_submission_creates_lead_and_notifies_owner(self, send_message):
        response = self.client.post(
            "/request/",
            {
                "name": "Анна",
                "contact": "@anna",
                "message": "Нужен сайт",
                "consent": "on",
                "page_url": "https://maksonchik.ru/?utm_campaign=test",
                "page_title": "Главная",
                "utm_campaign": "test",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        lead = Lead.objects.get()
        self.assertEqual(lead.contact, "@anna")
        self.assertEqual(lead.utm_campaign, "test")
        self.assertTrue(lead.notification_sent)
        send_message.assert_called_once()

    def test_submission_requires_contact_and_consent(self):
        response = self.client.post("/request/", {"name": "Анна"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Lead.objects.count(), 0)

    @patch("main.views.tg_send_message", return_value=False)
    def test_lead_is_saved_when_telegram_is_unavailable(self, send_message):
        response = self.client.post(
            "/request/",
            {"name": "Анна", "contact": "+79990000000", "consent": "on"},
        )

        self.assertEqual(response.status_code, 200)
        lead = Lead.objects.get()
        self.assertFalse(lead.notification_sent)
        self.assertTrue(lead.notification_error)

    def test_global_form_is_rendered_on_key_pages(self):
        for path in ("/", "/blog/", "/privacy/"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'id="lead-form"')
                self.assertContains(response, 'href="/privacy/"')

    def test_bot_page_has_no_development_lead_form(self):
        response = self.client.get("/", HTTP_HOST="who-update.ru")

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'id="lead-form"')
        self.assertNotContains(response, "Узнайте стоимость и сроки")
        self.assertContains(response, "Без Telegram Premium")

    def test_lead_is_registered_in_admin(self):
        self.assertIn(Lead, admin.site._registry)

    def test_home_explains_what_is_included_in_every_site(self):
        response = self.client.get("/")

        self.assertContains(response, "В каждый сайт уже входит")
        self.assertContains(response, "Адаптивная версия для смартфонов, планшетов и ноутбуков")
        self.assertContains(response, "Подключение домена")
        self.assertContains(response, "Базовая SEO-настройка")
        self.assertContains(response, "Яндекс Метрики")
        self.assertContains(response, "Поддержка после запуска")

    def test_home_offers_custom_automation(self):
        response = self.client.get("/")

        self.assertContains(response, "Любая автоматизация")
        self.assertContains(response, "интеграции с сервисами")

    def test_coffee_shop_landing_contains_preorder_offer_and_lead_form(self):
        response = self.client.get("/services/site-for-coffee-shop/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Разработка сайта и Telegram-бота для вашей кофейни")
        self.assertContains(response, "Разработка для владельцев кофеен")
        self.assertContains(response, "Как работает предзаказ")
        self.assertContains(response, "Повторить прошлый заказ")
        self.assertContains(response, "каждый шестой кофе в подарок")
        self.assertContains(response, "Можно начать с небольшого MVP")
        self.assertContains(response, 'id="lead-form"')
        self.assertContains(response, 'href="/privacy/"')

    def test_flower_shop_landing_is_a_development_offer(self):
        response = self.client.get("/services/site-for-flower-shop/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Разработка сайта для вашего цветочного магазина")
        self.assertContains(response, "Разработка для владельцев цветочных магазинов")
        self.assertContains(response, 'id="lead-form"')

    def test_coffee_shop_landing_is_in_sitemap(self):
        response = self.client.get("/sitemap.xml")

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "https://maksonchik.ru/services/site-for-coffee-shop/",
        )

    def test_privacy_page_contains_operator_inn(self):
        response = self.client.get("/privacy/")

        self.assertContains(response, "ИНН 330640351621")


class ReverseProxySecuritySettingsTest(TestCase):
    def test_https_forwarding_and_csrf_origins_are_configured(self):
        request = RequestFactory().get(
            "/admin/",
            HTTP_HOST="maksonchik.ru",
            HTTP_X_FORWARDED_PROTO="https",
        )

        self.assertTrue(request.is_secure())
        self.assertIn("https://maksonchik.ru", settings.CSRF_TRUSTED_ORIGINS)
        self.assertIn("https://www.maksonchik.ru", settings.CSRF_TRUSTED_ORIGINS)
        self.assertIn("https://who-update.ru", settings.CSRF_TRUSTED_ORIGINS)
        self.assertIn("https://www.who-update.ru", settings.CSRF_TRUSTED_ORIGINS)
        self.assertTrue(settings.CSRF_COOKIE_SECURE)
        self.assertTrue(settings.SESSION_COOKIE_SECURE)
