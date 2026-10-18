from django.apps import AppConfig


class TaxConfig(AppConfig):
    """Tax returns, VAT slice (docs/specs/tax-returns.md). Registers the module's ledger documents (R34):

    - ``VatReturn``: T1 offset, posted at approval with the ``tax.approve_return`` grant; never reversed.
    - ``ImportVatRecord`` (T3), ``TaxPayment`` (T2), ``TaxRefund`` (T4): approved and posted with
      ``tax.approve_payment`` by a person other than their creator, submitter or last modifier; reversed with
      ``tax.reverse_document``.

    Every type builds its own lines from its stored data, only inside a ``tax.services`` posting.
    """

    name = "tax"
    verbose_name = "Tax returns / الإقرارات الضريبية"

    def ready(self):
        from ledger import documents

        from . import services
        from .models import ImportVatRecord, TaxPayment, TaxRefund, VatReturn

        documents.register(VatReturn, post_permissions={"tax.approve_return"}, build_lines=services._vat_return_lines)
        documents.register(ImportVatRecord, post_permissions={"tax.approve_payment"},
                           reverse_permissions={"tax.reverse_document"}, build_lines=services._import_lines)
        documents.register(TaxPayment, post_permissions={"tax.approve_payment"},
                           reverse_permissions={"tax.reverse_document"}, build_lines=services._payment_lines)
        documents.register(TaxRefund, post_permissions={"tax.approve_payment"},
                           reverse_permissions={"tax.reverse_document"}, build_lines=services._refund_lines)
