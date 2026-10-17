"""Project-owner decisions of 2026-10-17 in invoicing (D24, D26, D27, D30; R91 to R95; access R46, R50).

Acceptance tests AT-49, AT-73 (amended), AT-93 to AT-101 and access AT-53. All identifiers are made up.
"""
from datetime import date

import pytest

from core import access
from core.access.models import AccessLog
from core.access.testing import make_user, member
from invoicing import services
from invoicing.conftest import ADDRESS, D, final, invoice
from invoicing.models import InvoicingError
from invoicing.test_purchases import bill

pytestmark = pytest.mark.django_db


def receipt(w, amount="1150.00", *, user=None, entity=None, customer=None, applications=(), **fields):
    user = user or w.clerk
    r = services.create_receipt(entity or w.alm, user=user, customer=customer or w.customer, date=date(2027, 1, 20),
                                amount=amount, **fields)
    for target, value in applications:
        services.add_receipt_application(r, target, value, user=user)
    return r


# --- D26 / access R46: receipts posted by their recorder (AT-49, AT-73) -----------------------------------

def test_access_at49_recorder_posts_receipt_but_not_refund_bill_or_other_entity(w):
    services.update_settings(w.alm, user=w.cfo, segregation_required=False)
    r = receipt(w, "500.00")
    posted = services.post_receipt(r, user=w.clerk)  # K posts R
    assert (posted.status, posted.posted_by) == ("posted", w.clerk)
    row = AccessLog.objects.filter(event="permission_granted", actor=w.clerk, permission="invoicing.post_receipt",
                                   details__document=["invoicing.customerreceipt", r.pk]).get()
    assert row.details["self_post"] is True and row.grant_use is not None
    refund = receipt(w, "50.00", kind="refund")
    with pytest.raises(access.AccessDenied) as exc:  # K posts F
        services.post_receipt(refund, user=w.clerk)
    assert exc.value.code == "self_approval"
    c = make_user("c_both")
    member(c, w.alm, "accountant")
    member(c, w.alm, "approver")
    b = bill(w, user=c)  # C creates and submits B
    with pytest.raises(access.AccessDenied) as exc:  # despite segregation_required = False (ledger R35)
        services.approve_purchase_document(b, user=c)
    assert exc.value.code == "self_approval"
    sub_customer = services.create_customer(w.sub, user=w.sub_clerk, code="S1", name_ar="عميل التابعة",
                                            is_vat_registered=True, vat_number="330000000000003", **ADDRESS)
    sub_receipt = receipt(w, "10.00", user=w.sub_clerk, entity=w.sub, customer=sub_customer)
    with pytest.raises(access.AccessDenied) as exc:  # K posts a receipt of SUB
        services.post_receipt(sub_receipt, user=w.clerk)
    assert exc.value.code == "no_membership"
    # A second person still may post a receipt (the recorder is allowed, not required).
    assert services.post_receipt(receipt(w, "20.00"), user=w.clerk2).posted_by == w.clerk2


def test_at73_recording_needs_allocate_and_post_receipt_then_the_recorder_posts(w):
    u = make_user("allocate_only")
    member(u, w.alm, "payables_clerk")  # invoicing.allocate, no invoicing.post_receipt
    with pytest.raises(access.AccessDenied):
        receipt(w, user=u)
    inv = final(w, invoice(w))
    r = receipt(w, applications=[(inv, "1150.00")])  # K records it
    posted = services.post_receipt(r, user=w.clerk)  # K posts E5
    lines = sorted((l.account.code, l.debit, l.credit) for l in posted.journal_entry.lines.select_related("account"))
    assert lines == [("1100", D("1150.00"), D("0")), ("1200", D("0"), D("1150.00"))]
    with pytest.raises(InvoicingError):  # L then finds it posted
        services.post_receipt(r, user=w.clerk2)
    with pytest.raises(InvoicingError) as exc:
        receipt(w, purpose="advance")
    assert exc.value.code == "purpose"
