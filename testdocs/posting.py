"""Test helpers: book lines as a registered sub-ledger document (testdocs stand-in), never as a manual journal.

Invoicing R97 (policy A): manual journals never post to the sub-ledger control accounts 1200, 1210, 2100 and 2110;
the testdocs types stand in for invoicing documents and declare them (``testdocs/apps.py``). Tests that need an open
item or a balance on a control account book it through :func:`post_document`, as a sub-ledger module would.
"""
from decimal import Decimal

from core import access
from ledger import services

from .models import Bill, Receipt, SalesDocument

POST_PERMISSION = {Bill: "invoicing.approve_bill", Receipt: "invoicing.post_receipt",
                   SalesDocument: "invoicing.approve_sales_document"}


def post_document(entity, on, lines, *, user, approver, model=Bill, description="Sub-ledger document (test)",
                  reference=""):
    """Create a stored ``model`` document of ``entity`` (its amount = the entry's debits, which the type's
    ``validate_lines`` checks) by ``user`` and post ``lines`` for it with ``approver``'s posting grant."""
    built = services._build_lines(entity, on, list(lines), user)  # priced as create_entry will price them
    amount = sum((line.debit for line in built), Decimal("0"))
    document = model.objects.create(entity=entity, created_by=user, amount=amount)
    grant = access.require(approver, entity, POST_PERMISSION[model], document=document)
    return services.create_entry(entity, on, description, list(lines), user=user, reference=reference, post=True,
                                 grant=grant, document=document)


def reverse_document(entry, *, user, **kwargs):
    """Reverse a stand-in document's entry with its registered reversal permission (never ``ledger.reverse_entry``)."""
    from ledger import documents

    document = documents.get(entry.document_type).model.objects.get(pk=entry.document_id)
    grant = access.require(user, entry.entity, "invoicing.reverse_document", document=document)
    return services.reverse_entry(entry, grant, **kwargs)
