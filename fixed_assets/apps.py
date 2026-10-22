from django.apps import AppConfig


class FixedAssetsConfig(AppConfig):
    """Fixed assets (docs/specs/fixed-assets.md, slice 1). Registers its two document types with the ledger registry
    (R38), the asset accounts as restricted accounts (R9) and its engine adapters (R36)."""

    name = "fixed_assets"
    label = "fixed_assets"
    verbose_name = "Fixed assets / الأصول الثابتة"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from ledger import documents, restricted

        from . import services, template
        from .models import AssetEvent, DepreciationRun

        # R38: runs and events book their own lines; validate_lines refuses anything else (FA2, FA4 to FA6, FA8).
        documents.register(
            DepreciationRun, post_permissions={"fixed_assets.run_depreciation"},
            reverse_permissions={"fixed_assets.reverse_document"},
            build_lines=services._run_lines, validate_lines=services._validate_run_lines,
            pending_documents=services._pending_runs, reverse_guard=services._reverse_guard,
        )
        documents.register(
            AssetEvent, post_permissions={"fixed_assets.capitalise", "fixed_assets.impair", "fixed_assets.dispose"},
            reverse_permissions={"fixed_assets.reverse_document"},
            build_lines=services._event_lines, validate_lines=services._validate_event_lines,
            pending_documents=services._pending_events, reverse_guard=services._reverse_guard,
        )
        # R9: cost and accumulated accounts are restricted (not control) accounts. Allowed: fixed-asset runs and
        # events on all of them; bills, vendor credit notes and mirror bills on the cost accounts only; the migration
        # opening entry (its allowance covers every restricted account, migration/apps.py). Reversals follow their
        # original. Manual journals, replayed legacy journals, bank transactions and payroll entries are refused.
        for code in template.RESTRICTED_CODES:
            cost = code in template.COST_CODES
            restricted.restrict(
                code, name_en=services.ACCOUNT_NAMES[code][0], name_ar=services.ACCOUNT_NAMES[code][1],
                posted_by_en=("depreciation runs and asset events (fixed assets), vendor bills and credit notes "
                              "(invoicing), migration opening balances, and their reversals" if cost else
                              "depreciation runs and asset events (fixed assets), migration opening balances, and "
                              "their reversals"),
                posted_by_ar=("مسيرات الاستهلاك وأحداث الأصول (الأصول الثابتة)، وفواتير الموردين وإشعاراتهم الدائنة "
                              "(الفوترة)، والأرصدة الافتتاحية المرحّلة، وعكسها" if cost else
                              "مسيرات الاستهلاك وأحداث الأصول (الأصول الثابتة)، والأرصدة الافتتاحية المرحّلة، وعكسها"),
            )
            restricted.allow(code, document_type="fixed_assets.depreciationrun")
            restricted.allow(code, document_type="fixed_assets.assetevent")
            if cost:
                restricted.allow(code, document_type="invoicing.purchasedocument")

        from . import approval_adapters

        approval_adapters.register()  # access R61: every fixed-asset approval goes through the one engine (R36)
