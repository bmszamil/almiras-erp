"""Review item 8: invoicing rows change only as invoicing.services change them.

The model and queryset guards run everywhere; the database triggers (migration 0007) only on PostgreSQL,
where raw SQL that bypasses the ORM is refused too.
"""
from datetime import date

import pytest
from django.db import DatabaseError, connection, transaction

from invoicing import services
from invoicing.conftest import approve, final, invoice
from invoicing.models import (
    InvoicingError,
    PayableAllocation,
    ReceiptApplication,
    ReceivableAllocation,
    SalesDocument,
)
from invoicing.test_purchases import bill

pytestmark = pytest.mark.django_db
postgres_only = pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL triggers")


def _raw(sql, params=()):
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql, params)


def _refused(sql, params=(), match=None):
    with pytest.raises(DatabaseError, match=match):
        _raw(sql, params)


def _posted_receipt(w, inv):
    r = services.create_receipt(w.alm, user=w.clerk, customer=w.customer, date=date(2027, 1, 20), amount="1150.00")
    app = services.add_receipt_application(r, inv, "1150.00", user=w.clerk)
    return services.post_receipt(r, user=w.clerk2), app


# --- everywhere: the Application guards ------------------------------------------------------------

def test_applications_of_posted_receipts_never_change_through_the_orm(w):
    inv = final(w, invoice(w))
    receipt, app = _posted_receipt(w, inv)
    with pytest.raises(InvoicingError):
        ReceiptApplication.objects.filter(pk=app.pk).update(amount="1.00")
    with pytest.raises(InvoicingError):
        ReceiptApplication.objects.filter(pk=app.pk).delete()
    with pytest.raises(InvoicingError):
        ReceiptApplication.objects.get(pk=app.pk).delete()
    draft = services.create_receipt(w.alm, user=w.clerk, customer=w.customer, date=date(2027, 1, 20), amount="5")
    with pytest.raises(InvoicingError):  # not moved to another receipt either
        ReceiptApplication.objects.filter(pk=app.pk).update(receipt=draft)
    assert ReceiptApplication.objects.get(pk=app.pk).amount == app.amount


# --- PostgreSQL: the triggers ----------------------------------------------------------------------

@postgres_only
def test_final_sales_document_and_its_lines_never_change_in_the_database(w):
    doc = final(w, invoice(w))
    line = doc.lines.get()
    _refused("UPDATE invoicing_salesdocument SET total_gross = 1 WHERE id = %s", [doc.pk], "never changes")
    _refused("UPDATE invoicing_salesdocument SET zatca_hash = 'x' WHERE id = %s", [doc.pk])
    _refused("UPDATE invoicing_salesdocument SET status = 'draft' WHERE id = %s", [doc.pk])
    _refused("DELETE FROM invoicing_salesdocument WHERE id = %s", [doc.pk], "only drafts")
    _refused("UPDATE invoicing_salesdocumentline SET quantity = 2 WHERE id = %s", [line.pk], "never changes")
    _refused("DELETE FROM invoicing_salesdocumentline WHERE id = %s", [line.pk])
    _raw("UPDATE invoicing_salesdocument SET updated_at = now() WHERE id = %s", [doc.pk])  # the timestamp may move


@postgres_only
def test_issued_sales_document_changes_only_through_the_zatca_result(w):
    issued = approve(w, invoice(w))
    _refused("UPDATE invoicing_salesdocument SET total_net = 1 WHERE id = %s", [issued.pk])
    _refused("UPDATE invoicing_salesdocument SET status = 'final' WHERE id = %s", [issued.pk], "own posted entry")
    _refused("UPDATE invoicing_salesdocument SET status = 'rejected' WHERE id = %s", [issued.pk])  # not by ZATCA
    _raw("UPDATE invoicing_salesdocument SET zatca_hash = 'h' WHERE id = %s", [issued.pk])  # a ZATCA field
    assert w.zatca.clear(SalesDocument.objects.get(pk=issued.pk)).status == "final"


@postgres_only
def test_submitted_sales_document_changes_only_by_approval_or_return(w):
    doc = invoice(w)
    line = doc.lines.get()
    _refused("UPDATE invoicing_salesdocument SET notes_ar = 'x' WHERE id = %s", [doc.pk])
    _refused("UPDATE invoicing_salesdocument SET status = 'issued' WHERE id = %s", [doc.pk], "only by approval")
    _refused("UPDATE invoicing_salesdocumentline SET unit_price = 1 WHERE id = %s", [line.pk], "re-priced")
    _raw("UPDATE invoicing_salesdocument SET status = 'draft', modified_at = now() WHERE id = %s", [doc.pk])
    _raw("UPDATE invoicing_salesdocumentline SET unit_price = 1 WHERE id = %s", [line.pk])  # a draft again
    _refused("INSERT INTO invoicing_salesdocument (status) VALUES ('final')")


@postgres_only
def test_posted_bill_receipt_and_payment_never_change_in_the_database(w):
    b = services.approve_purchase_document(bill(w), user=w.cfo)
    inv = final(w, invoice(w))
    receipt, app = _posted_receipt(w, inv)
    pay = services.create_vendor_payment(w.alm, user=w.ap, vendor=w.vendor, date=date(2027, 1, 25), amount="10")
    pay = services.approve_vendor_payment(services.submit_vendor_payment(pay, user=w.ap), user=w.cfo)
    for table, pk in (("purchasedocument", b.pk), ("customerreceipt", receipt.pk), ("vendorpayment", pay.pk)):
        _refused(f"UPDATE invoicing_{table} SET due_date = NULL, number = 'X' WHERE id = %s", [pk])
        _refused(f"UPDATE invoicing_{table} SET status = 'reversed' WHERE id = %s", [pk], "reversal entry")
        _refused(f"UPDATE invoicing_{table} SET status = 'draft' WHERE id = %s", [pk])
        _refused(f"DELETE FROM invoicing_{table} WHERE id = %s", [pk], "only drafts")
    _refused("UPDATE invoicing_purchasedocument SET total_gross = 1 WHERE id = %s", [b.pk])
    _refused("UPDATE invoicing_customerreceipt SET amount = 1 WHERE id = %s", [receipt.pk])
    _refused("UPDATE invoicing_purchasedocumentline SET unit_price = 1 WHERE document_id = %s", [b.pk])
    _refused("UPDATE invoicing_receiptapplication SET amount = 1 WHERE id = %s", [app.pk], "never changes")
    _refused("DELETE FROM invoicing_receiptapplication WHERE id = %s", [app.pk])
    services.reverse_document(b, user=w.accountant)  # the services' own transitions still work
    _refused("UPDATE invoicing_purchasedocument SET reversal_entry_id = NULL WHERE id = %s", [b.pk])


@postgres_only
def test_submitted_bill_and_payment_change_only_by_approval(w):
    b = bill(w)
    _refused("UPDATE invoicing_purchasedocument SET vendor_total_gross = 1 WHERE id = %s", [b.pk])
    _refused("UPDATE invoicing_purchasedocument SET status = 'posted' WHERE id = %s", [b.pk], "only by approval")
    draft = bill(w, number="A-18", submit=False)
    _refused("UPDATE invoicing_purchasedocument SET status = 'posted', number = 'X' WHERE id = %s", [draft.pk])
    _raw("UPDATE invoicing_purchasedocument SET due_date = '2027-02-01' WHERE id = %s", [draft.pk])  # a draft


@postgres_only
def test_allocations_are_append_only_in_the_database(w):
    inv = final(w, invoice(w))
    receipt, _ = _posted_receipt(w, inv)
    alloc = ReceivableAllocation.objects.get(receipt=receipt)
    _refused("UPDATE invoicing_receivableallocation SET amount = 1 WHERE id = %s", [alloc.pk], "append-only|never changed")
    _refused("DELETE FROM invoicing_receivableallocation WHERE id = %s", [alloc.pk])
    services.unallocate(alloc, user=w.clerk)  # an un-allocation is a new row
    b = services.approve_purchase_document(bill(w), user=w.cfo)
    vcn = services.approve_purchase_document(
        bill(w, [("5300", "1", "100.00")], number="VCN-1", doc_type="vendor_credit_note", original_document=b),
        user=w.cfo)
    row = PayableAllocation.objects.get(vendor_credit_note=vcn)
    _refused("DELETE FROM invoicing_payableallocation WHERE id = %s", [row.pk])
