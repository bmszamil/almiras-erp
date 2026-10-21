from django.apps import AppConfig


class MigrationConfig(AppConfig):
    """Legacy data migration (docs/specs/migration.md): Falcon (or other legacy system) exports into opening balances
    and replayed journals, loaded through ledger.services only."""

    name = "migration"
    label = "migration"
    verbose_name = "Data migration / ترحيل البيانات"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from ledger import restricted

        from . import services

        # invoicing D31b review fix 1: an opening entry imported by this app may carry balances of restricted
        # accounts (e.g. 1140, the purification account); replayed journals may not.
        restricted.allow(None, document_type="", predicate=services._opening_entry_allowed)

        from . import approval_adapters

        approval_adapters.register()  # access R61: the module's documents go through the one approval engine
