from django.apps import AppConfig


class WebhookTgConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'webhook_tg'

    def ready(self):
        import webhook_tg.signals.auth_signals # noqa
