from django.apps import AppConfig


class TestDocsConfig(AppConfig):
    """Test-only stand-ins for sub-ledger documents, used by the ledger's own tests.

    Installed only by ``config.settings_test``. Registers its models with the
    ledger document registry the way a real sub-ledger app (``invoicing``)
    does in its own ``ready()``. Every registered type needs a line hook;
    these register ``validate_lines``, which checks the entry against the
    document's amount (invoicing registers ``build_lines``). ``Unregistered``
    is deliberately left out of the registry.
    """

    name = "testdocs"
    verbose_name = "Test documents / مستندات اختبار"

    def ready(self):
        from ledger import documents

        from .models import Bill, Receipt, SalesDocument, validate_lines

        documents.register(Bill, post_permissions={"invoicing.approve_bill"},
                           reverse_permissions={"invoicing.reverse_document"}, validate_lines=validate_lines)
        documents.register(Receipt, post_permissions={"invoicing.post_receipt"},
                           reverse_permissions={"invoicing.reverse_document"}, validate_lines=validate_lines)
        documents.register(
            SalesDocument, post_permissions={"invoicing.approve_sales_document", "invoicing.post_on_clearance"},
            reverse_permissions={"invoicing.reverse_document"}, validate_lines=validate_lines,
        )
