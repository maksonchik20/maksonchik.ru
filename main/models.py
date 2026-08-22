from django.db import models


class Lead(models.Model):
    name = models.CharField("Имя", max_length=120)
    contact = models.CharField("Телефон, email или Telegram", max_length=255)
    message = models.TextField("Задача", blank=True)
    page_url = models.URLField("Страница отправки", max_length=1000, blank=True)
    page_title = models.CharField("Название страницы", max_length=300, blank=True)
    utm_source = models.CharField(max_length=255, blank=True)
    utm_medium = models.CharField(max_length=255, blank=True)
    utm_campaign = models.CharField(max_length=255, blank=True)
    utm_content = models.CharField(max_length=255, blank=True)
    utm_term = models.CharField(max_length=255, blank=True)
    notification_sent = models.BooleanField("Уведомление отправлено", default=False)
    notification_error = models.TextField("Ошибка уведомления", blank=True)
    created_at = models.DateTimeField("Создана", auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Заявка с сайта"
        verbose_name_plural = "Заявки с сайта"
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.name} — {self.contact}"
