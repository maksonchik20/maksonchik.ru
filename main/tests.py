from unittest.mock import patch

from django.contrib import admin
from django.test import TestCase

from .models import Lead


class LeadFormTest(TestCase):
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
        for path in ("/", "/bot/", "/blog/", "/privacy/"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'id="lead-form"')
                self.assertContains(response, 'href="/privacy/"')

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
