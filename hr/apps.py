from django.apps import AppConfig


class HrConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "hr"
    verbose_name = "HR / الموارد البشرية"

    def ready(self):
        from . import checks  # noqa: F401  (registers hr.E001)
