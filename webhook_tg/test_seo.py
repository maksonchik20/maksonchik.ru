import json
import re

from django.test import TestCase


def json_ld_blocks(response):
    html = response.content.decode("utf-8")
    blocks = re.findall(
        r'<script type="application/ld\+json">\s*(.*?)\s*</script>',
        html,
        flags=re.DOTALL,
    )
    return [json.loads(block) for block in blocks]


class SeoPagesTest(TestCase):
    def test_who_update_uses_single_canonical_url(self):
        response = self.client.get("/", HTTP_HOST="who-update.ru")

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            '<link rel="canonical" href="https://who-update.ru/">',
            html=True,
        )
        self.assertContains(response, "Посмотреть демонстрацию", count=3)
        self.assertContains(response, 'href="#features">Демонстрация работы</a>')
        self.assertNotContains(response, 'href="#how">Как это работает</a>')
        self.assertContains(response, 'class="feature-icon"', count=7)
        self.assertContains(response, "Уведомления в реальном времени")
        self.assertContains(response, "WhoUpdate работает 24/7, даже когда вы офлайн")
        self.assertNotContains(response, "Только ваши чаты")
        self.assertContains(response, "История после удаления переписки")
        self.assertContains(response, "/history @username")
        self.assertContains(response, "TXT-файл со всей доступной историей")
        self.assertContains(response, 'preload="none"')
        self.assertContains(response, ".demo-dialog, .demo-dialog * { cursor: auto; }")
        self.assertContains(response, "/who-update-demo-media/deleted-message.mp4")
        self.assertContains(response, "/who-update-demo-media/edited-message.mp4")
        self.assertContains(response, "/who-update-demo-media/hidden-media.mp4")
        self.assertContains(
            response,
            "Telegram Premium не нужен · Подключение за минуту · Бесплатно",
        )

    def test_who_update_alias_redirects_permanently(self):
        response = self.client.get("/who-update-bot/")

        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], "https://who-update.ru/")

    def test_old_bot_path_redirects_permanently_to_new_domain(self):
        response = self.client.get("/bot/")

        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], "https://who-update.ru/")

    def test_main_sitemap_no_longer_advertises_bot_subdirectory(self):
        response = self.client.get("/sitemap.xml")
        content = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("https://maksonchik.ru/bot/", content)
        self.assertNotIn("https://maksonchik.ru/who-update-bot/", content)

    def test_who_update_domain_has_own_root_and_metadata(self):
        response = self.client.get("/", HTTP_HOST="who-update.ru")

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            '<link rel="canonical" href="https://who-update.ru/">',
            html=True,
        )
        self.assertContains(response, 'content="WhoUpdate"')
        self.assertContains(response, "© WhoUpdate · who-update.ru")
        self.assertContains(response, "mc.yandex.ru/watch/112093587")
        self.assertNotContains(response, "mc.yandex.ru/watch/111680333")
        self.assertNotContains(response, 'id="lead-form"')

    def test_who_update_domain_redirects_legacy_bot_path_to_root(self):
        response = self.client.get("/bot/", HTTP_HOST="who-update.ru")

        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], "/")

    def test_who_update_domain_exposes_only_bot_surface(self):
        self.assertEqual(
            self.client.get("/admin/", HTTP_HOST="who-update.ru").status_code,
            404,
        )
        self.assertEqual(
            self.client.get("/services/online-store/", HTTP_HOST="who-update.ru").status_code,
            404,
        )

    def test_who_update_domain_has_own_sitemap_and_robots(self):
        sitemap = self.client.get("/sitemap.xml", HTTP_HOST="who-update.ru")
        robots = self.client.get("/robots.txt", HTTP_HOST="who-update.ru")

        self.assertContains(sitemap, "https://who-update.ru/")
        self.assertContains(sitemap, "https://who-update.ru/privacy/")
        self.assertNotContains(sitemap, "maksonchik.ru")
        self.assertContains(robots, "Sitemap: https://who-update.ru/sitemap.xml")
        self.assertContains(robots, "Disallow: /bot/payment/")

    def test_who_update_webmaster_verification_file_is_available(self):
        response = self.client.get(
            "/yandex_f8f11c5f646de698.html",
            HTTP_HOST="who-update.ru",
        )

        self.assertContains(response, "Verification: f8f11c5f646de698")

    def test_who_update_metrika_verification_file_is_available(self):
        response = self.client.get(
            "/yandex_73195eba1d0ba5f4.html",
            HTTP_HOST="who-update.ru",
        )

        self.assertContains(response, "Verification: 73195eba1d0ba5f4")

    def test_who_update_legal_pages_are_available(self):
        privacy = self.client.get("/privacy/", HTTP_HOST="who-update.ru")
        terms = self.client.get("/terms/", HTTP_HOST="who-update.ru")

        self.assertContains(privacy, "Политика обработки персональных данных")
        self.assertContains(privacy, "ИНН 330640351621")
        self.assertContains(terms, "Условия использования и оплаты")
        self.assertContains(terms, "пробный период 7 дней")
        self.assertContains(terms, "1 месяц — 99 ₽")
        self.assertContains(terms, "автоматического продления")

    def test_home_schema_uses_person_and_website(self):
        response = self.client.get("/")
        schema = json_ld_blocks(response)
        serialized = json.dumps(schema, ensure_ascii=False)

        self.assertNotIn("ProfessionalService", serialized)
        self.assertIn('"@type": "Person"', serialized)
        self.assertIn('"@type": "WebSite"', serialized)

    def test_veterinary_page_is_explicitly_about_development(self):
        response = self.client.get("/services/site-for-veterinary-clinic/")
        content = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Разработка сайтов для ветеринарных клиник под ключ", content)
        self.assertIn("Модуль записи к ветврачу", content)
        self.assertNotIn("ProfessionalService", json.dumps(json_ld_blocks(response)))

    def test_articles_expose_author_and_modified_date(self):
        response = self.client.get("/blog/sait-dlya-veterinarnoy-kliniki/")
        content = response.content.decode("utf-8")
        schema = json.dumps(json_ld_blocks(response), ensure_ascii=False)

        self.assertIn("Автор: Максим Горшунов", content)
        self.assertIn('"dateModified": "2026-08-18"', schema)
