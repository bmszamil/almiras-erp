from django.apps import AppConfig


class MigrationConfig(AppConfig):
    """Legacy data migration (docs/specs/migration.md): Falcon (or other legacy system) exports into opening balances
    and replayed journals, loaded through ledger.services only."""

    name = "migration"
    label = "migration"
    verbose_name = "Data migration / ترحيل البيانات"
    default_auto_field = "django.db.models.BigAutoField"
