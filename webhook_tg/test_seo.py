import json
import re

from django.test import SimpleTestCase


def json_ld_blocks(response):
    html = response.content.decode("utf-8")
    blocks = re.findall(
        r'<script type="application/ld\+json">\s*(.*?)\s*</script>',
        html,
        flags=re.DOTALL,
    )
    return [json.loads(block) for block in blocks]


class SeoPagesTest(SimpleTestCase):
    def test_who_update_uses_single_canonical_url(self):
        response = self.client.get("/bot/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            '<link rel="canonical" href="https://maksonchik.ru/bot/">',
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
        self.assertEqual(response["Location"], "/bot/")

    def test_sitemap_contains_only_canonical_who_update_url(self):
        response = self.client.get("/sitemap.xml")
        content = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertIn("https://maksonchik.ru/bot/", content)
        self.assertNotIn("https://maksonchik.ru/who-update-bot/", content)

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
