from django.apps import AppConfig


class TaxConfig(AppConfig):
    """Tax returns, VAT slice (docs/specs/tax-returns.md). Registers the module's ledger documents (R34):

    - ``VatReturn``: T1 offset, posted at approval with the ``tax.approve_return`` grant; never reversed (no reversal
      permission, and the ledger refuses ``ledger.reverse_entry`` on a registered document's entry).
    - ``ImportVatRecord`` (T3), ``TaxPayment`` (T2), ``TaxRefund`` (T4): approved and posted with
      ``tax.approve_payment`` by a person other than their creator, submitter or last modifier; reversed with
      ``tax.reverse_document``, only inside ``tax.services.reverse_tax_document`` (``reverse_guard``), so the
      payment, refund or record and its return never disagree with the ledger.

    Every type builds its own lines from its stored data, only inside a ``tax.services`` posting.
    """

    name = "tax"
    verbose_name = "Tax returns / الإقرارات الضريبية"

    def ready(self):
        from core.signals import entity_settings_changed
        from ledger import documents

        from . import services
        from .models import ImportVatRecord, TaxPayment, TaxRefund, VatReturn

        documents.register(VatReturn, post_permissions={"tax.approve_return"}, build_lines=services._vat_return_lines)
        documents.register(ImportVatRecord, post_permissions={"tax.approve_payment"},
                           reverse_permissions={"tax.reverse_document"}, build_lines=services._import_lines,
                           reverse_guard=services._reverse_guard)
        documents.register(TaxPayment, post_permissions={"tax.approve_payment"},
                           reverse_permissions={"tax.reverse_document"}, build_lines=services._payment_lines,
                           reverse_guard=services._reverse_guard)
        documents.register(TaxRefund, post_permissions={"tax.approve_payment"},
                           reverse_permissions={"tax.reverse_document"}, build_lines=services._refund_lines,
                           reverse_guard=services._reverse_guard)
        # R4 / review item 6: a VAT frequency change is dated and the schedule regenerated with it.
        entity_settings_changed.connect(services._on_entity_settings_changed, dispatch_uid="tax_vat_frequency")

        from . import approval_adapters

        approval_adapters.register()  # access R61: the module's documents go through the one approval engine
