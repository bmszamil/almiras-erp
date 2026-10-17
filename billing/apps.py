from django.apps import AppConfig


class BillingConfig(AppConfig):
    """Billing (docs/specs/billing.md): contract-based recurring and intercompany billing.

    Billing registers no ledger document type: it books nothing itself. Invoices and correction notes are
    invoicing documents, created through ``billing.invoicing_port`` and posted by invoicing (§5).
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "billing"
    verbose_name = "Billing / الفوترة الدورية"
