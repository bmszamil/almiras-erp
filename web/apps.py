from django.apps import AppConfig


class WebConfig(AppConfig):
    """The Arabic/English web interface (docs/specs/web.md). Holds no business logic."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "web"
    verbose_name = "Web interface / واجهة الويب"

    def ready(self):
        from . import checks  # noqa: F401  (registers the deploy checks web.E001, web.E002)
