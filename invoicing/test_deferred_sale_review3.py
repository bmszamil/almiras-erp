"""Bai' ajel ibra' review fixes of 2026-10-20, third round (invoicing R128 as amended; payroll §10.9 items 17 to 22),
with the real payroll invoicing port, real payroll runs and final settlements, invoicing's fake ZATCA and a fixed
Riyadh clock; anonymised fixtures, nothing external.

Finding 1: a rebate credit note rejected after the employee's final settlement leaves the amount outstanding after
exit on 1240, and the books stay consistent whether the rebate is then approved again (refused) or discarded.
Item 7: a rejection read only after every reduced instalment was collected comes back as one more instalment that a
real run collects, crediting 1240 to zero. Finding 4: a forged marker issues no credit note."""
from types import SimpleNamespace

import pytest

from invoicing import services as inv
from invoicing.conftest import zatca  # noqa: F401  (invoicing's fake ZATCA port, a fixture)
from invoicing.models import DeferredSaleAdjustment, InvoicingError, SalesDocument
from invoicing.test_deferred_sale import (  # noqa: F401  (the real port and its world)
    balance, bilingual, bw, invoicing, sale_doc, sold)
from invoicing.test_deferred_sale_review2 import note_of, rebate, sold_contract  # noqa: F401
from payroll import invoicing_port as payroll_port
from payroll import services as payroll
from payroll.conftest import D, amount, approved, calculated, clock, pclock, saudi, w  # noqa: F401
from payroll.models import DeferredSaleContract, DeferredSaleInstallment, DeferredSaleRebate, FinalSettlement, \
    PayrollError
from payroll.test_final_settlement import go_live, leaver, settle, terminate
from payroll.test_slice2_bai_ajel import bai_ajel


def books(w):
    return {a: balance(w.alm, a) for a in ("1240", "1241", "4610", "2200")}


def sell(w, emp, django_capture_on_commit_callbacks, **kw):
    with django_capture_on_commit_callbacks(execute=True):
        c = payroll.approve_deferred_sale(bai_ajel(w, emp, **kw), user=w.mp)
    w.zatca.report(sale_doc(c))
    payroll._refresh_deferred_sales(w.alm)
    return DeferredSaleContract.objects.get(pk=c.pk)


def approve(w, r, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        return payroll.approve_deferred_sale_rebate(r, user=w.mp)


def run(w, period, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        return payroll.approve_run(payroll.submit_run(calculated(w, period), user=w.p), user=w.h)


def after_exit(c):
    return sum((i.amount for i in DeferredSaleInstallment.objects.filter(contract=c, status="outstanding_after_exit")),
               D("0.00"))


# --- finding 1: rejected after the final settlement ---------------------------------------------------------------

@pytest.fixture
def settled_with_rejected_rebate(bw, django_capture_on_commit_callbacks):
    """N1 (final-dues undertaking) buys for 1,200.00 + 120.00 profit + VAT 198.00 (12 x 126.50); a rebate of 60.00
    (gross 69.00) is pending when the final settlement recovers the rest (1,449.00) and is approved; ZATCA then
    rejects the credit note and payroll reads it."""
    w = bw
    n1 = leaver(w)
    c = sell(w, n1, django_capture_on_commit_callbacks, first="2027-03", undertaking=True)
    t = terminate(w, n1)
    go_live(w)
    r = approve(w, rebate(w, c, amount="60.00"), django_capture_on_commit_callbacks)
    assert note_of(r).status == "issued"
    with django_capture_on_commit_callbacks(execute=True):
        fs = payroll.approve_final_settlement(FinalSettlement.objects.get(pk=settle(w, t).pk), user=w.c)
    assert fs.status == "approved"
    # prudence: the pending 60.00 is not recognised; 1240 keeps the 69.00 the pending note would have credited
    assert books(w) == {"1240": D("69.00"), "1241": D("-60.00"), "4610": D("-60.00"), "2200": D("-198.00")}
    w.zatca.reject(note_of(r))
    payroll._refresh_deferred_sales(w.alm)
    return w, c, DeferredSaleRebate.objects.get(pk=r.pk)


def consistent(w, c):
    """1240 is exactly what is outstanding after exit; no credit note counts, so the profit not yet recognised
    (on 1241) and the profit recognised (4610) add up to the full fixed profit; no planned instalment is left."""
    b = books(w)
    assert b["1240"] == after_exit(c) == D("69.00")
    assert -(b["1241"] + b["4610"]) == D("120.00") and b["2200"] == D("-198.00")
    assert not c.installments.filter(status="planned").exists()
    return b


def test_f1_rejected_after_the_settlement_is_outstanding_after_exit_and_discard_keeps_the_books(
        settled_with_rejected_rebate):
    w, c, r = settled_with_rejected_rebate
    assert r.status == "rejected"
    before = consistent(w, c)
    payroll.discard_deferred_sale_rebate(r, user=w.p)
    assert consistent(w, c) == before
    assert DeferredSaleRebate.objects.get(pk=r.pk).status == "discarded"


def test_f1_rejected_after_the_settlement_is_not_approved_again_and_the_books_are_unchanged(
        settled_with_rejected_rebate, django_capture_on_commit_callbacks):
    w, c, r = settled_with_rejected_rebate
    before = consistent(w, c)
    notes = SalesDocument.objects.filter(doc_type="credit_note").count()
    with pytest.raises(PayrollError) as exc:
        approve(w, r, django_capture_on_commit_callbacks)
    assert exc.value.code == "rebate_after_exit" and bilingual(exc.value)
    assert SalesDocument.objects.filter(doc_type="credit_note").count() == notes  # no new credit note
    assert consistent(w, c) == before
    payroll.discard_deferred_sale_rebate(DeferredSaleRebate.objects.get(pk=r.pk), user=w.p)
    assert consistent(w, c) == before


# --- item 7: rejection read only after every instalment was collected (reproduction x4), real runs ---------------

def test_item7_a_rejection_read_after_full_collection_is_collected_by_a_real_run_and_1240_ends_at_zero(
        bw, django_capture_on_commit_callbacks):
    w = bw
    s1 = saudi(w)
    c = sell(w, s1, django_capture_on_commit_callbacks, cost="200.00", profit="100.00", term=2)  # 172.50 x 2
    approved(w, "2027-03")
    r = approve(w, rebate(w, c, amount="50.00"), django_capture_on_commit_callbacks)  # gross 57.50 off May
    assert [i.amount for i in c.installments.order_by("period")] == [D("172.50"), D("115.00")]
    run(w, "2027-04", django_capture_on_commit_callbacks)
    run(w, "2027-05", django_capture_on_commit_callbacks)
    assert not c.installments.filter(status="planned").exists()  # all collected while the note was pending
    assert books(w) == {"1240": D("57.50"), "1241": D("-50.00"), "4610": D("-50.00"), "2200": D("-45.00")}
    w.zatca.reject(note_of(r))  # ZATCA rejects; payroll reads it with the next run calculation
    jun = calculated(w, "2027-06")
    assert DeferredSaleRebate.objects.get(pk=r.pk).status == "rejected"
    assert amount(jun, s1, "deferred_sale_installment") == D("57.50")  # nothing was waived: collected next
    with django_capture_on_commit_callbacks(execute=True):
        payroll.approve_run(payroll.submit_run(jun, user=w.p), user=w.h)
    assert books(w) == {"1240": D("0.00"), "1241": D("0.00"), "4610": D("-100.00"), "2200": D("-45.00")}


# --- finding 4: a forged marker (reproduction x5) -----------------------------------------------------------------

def test_f4_a_forged_marker_issues_no_credit_note_through_the_real_port(sold_contract,
                                                                     django_capture_on_commit_callbacks):
    w, s1, c, doc = sold_contract
    r = rebate(w, c)
    ref = f"payroll.deferredsalerebate:{r.pk}"
    with pytest.raises(InvoicingError) as exc:
        with payroll_port.rebating(SimpleNamespace(pk=r.pk), w.mp, token="f" * 64, grant=None):
            with django_capture_on_commit_callbacks(execute=True):
                inv.create_deferred_sale_adjustment(c, approver=w.mp, source_ref=ref, amount="600.00", reason_ar="x")
    assert exc.value.code == "rebate_not_from_payroll"
    assert not DeferredSaleAdjustment.objects.exists()
    assert not SalesDocument.objects.filter(doc_type="credit_note").exists()
    payroll.discard_deferred_sale_rebate(r, user=w.p)
    assert sum(i.amount for i in c.installments.filter(status="planned")) == D("38640.00")
