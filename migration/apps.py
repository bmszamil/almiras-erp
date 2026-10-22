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

        from ledger import services as ledger

        # R16, R22 (M7): an import draft is posted only when its lines are unchanged and, for the opening entry, its
        # control accounts tie out to the open items loaded, whichever path posts it (command or engine inbox).
        ledger.register_post_check(services._ledger_post_check, code="import_not_ready")

        # R22, R23 (OQ-12): the sub-ledgers the opening entry ties out to, as loaded by migration.subledgers: open
        # receivables and payables (invoicing migrated open items, 1200 / 1210 / 2100 / 2110), end-of-service
        # provisions (payroll openings, 2500) and migrated bai' ajel contracts (1240 / 1241, W14).
        from invoicing import migration_port
        from invoicing import services as invoicing
        from payroll import services as payroll

        services.register_open_items_source(migration_port.open_item_balances, codes=services.CONTROL_CODES,
                                            per_source_system=True)
        services.register_open_items_source(payroll.opening_eosb_balances, codes={payroll.EOSB_PROVISION})
        services.register_open_items_source(invoicing.migrated_bai_ajel_balances,
                                            codes={invoicing.EMPLOYEE_RECEIVABLE, invoicing.DEFERRED_PROFIT})
        # fixed-assets R41: the opening asset register (cost per category cost account, accumulated depreciation per
        # accumulated account) on 1500, 1510, 1520, 1590 and the proposed 15xx codes once in a chart.
        from fixed_assets import migration_port as fixed_assets

        services.register_open_items_source(fixed_assets.opening_register_balances, codes=fixed_assets.GATED_CODES,
                                            per_source_system=True)

        from . import approval_adapters

        approval_adapters.register()  # access R61: the module's documents go through the one approval engine
