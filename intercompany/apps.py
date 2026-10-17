from django.apps import AppConfig


class IntercompanyConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "intercompany"
    verbose_name = "Intercompany billing / فوترة المجموعة"

    def ready(self):
        from . import signals  # noqa: F401  (cost pool account checks, R7)
