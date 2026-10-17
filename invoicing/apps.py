from django.apps import AppConfig


class InvoicingConfig(AppConfig):
    """Invoicing (docs/specs/invoicing.md). Registers its document types with the ledger document registry.

    Every type registers a ``build_lines`` hook: the document builds its own journal lines from its
    stored data, so a caller cannot book other lines for it (access R34, R35).
    """

    name = "invoicing"
    verbose_name = "Invoicing / الفوترة"

    def ready(self):
        from ledger import documents

        from . import services
        from .models import CustomerReceipt, PurchaseDocument, SalesDocument, VendorPayment

        # Sales documents post only on ZATCA clearance or reporting (R41); they are never reversed (R17, OQ-28).
        documents.register(
            SalesDocument, post_permissions={"invoicing.post_on_clearance"},
            build_lines=services._sales_document_lines, validate_lines=services._check_document_rate,
            pending_documents=services._pending_sales_documents,
        )
        documents.register(
            PurchaseDocument, post_permissions={"invoicing.approve_bill"},
            reverse_permissions={"invoicing.reverse_document"},
            build_lines=services._purchase_document_lines, validate_lines=services._check_document_rate,
        )
        documents.register(
            CustomerReceipt, post_permissions={"invoicing.post_receipt"},
            reverse_permissions={"invoicing.reverse_document"},
            build_lines=services._receipt_lines, validate_lines=services._check_document_rate,
        )
        documents.register(
            VendorPayment, post_permissions={"invoicing.approve_vendor_payment"},
            reverse_permissions={"invoicing.reverse_document"},
            build_lines=services._payment_lines, validate_lines=services._check_document_rate,
        )
