from django.apps import AppConfig


class HrConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "hr"
    verbose_name = "HR / الموارد البشرية"

    def ready(self):
        from . import checks  # noqa: F401  (registers hr.E001)

        from . import approval_adapters

        approval_adapters.register()  # access R61: the module's documents go through the one approval engine
