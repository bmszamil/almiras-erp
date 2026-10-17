from django.apps import AppConfig


class ZatcaConfig(AppConfig):
    """ZATCA Fatoora Phase 2 e-invoicing (docs/specs/zatca.md). Creates no journal entries (§5)."""

    name = "zatca"
    verbose_name = "ZATCA e-invoicing / الفوترة الإلكترونية"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import checks  # noqa: F401  (registers zatca.E001)
