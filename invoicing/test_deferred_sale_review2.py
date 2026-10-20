"""Bai' ajel ibra' review fixes of 2026-10-20, second round (invoicing R128 as amended; payroll §10.9 items 11 to 15),
with the real payroll invoicing port, payroll's fixtures (anonymised), invoicing's fake ZATCA and a fixed Riyadh
clock; nothing external.

Finding 1: ``create_deferred_sale_adjustment`` is refused outside payroll's approval of that rebate. Finding 2: only a
final credit note changes the sale's figures; a pending one waits (profit is recognised prudently meanwhile); a
rejected one never counts, the rebate goes back to ``rejected`` with an alert and is replaced. Finding 5: the credit
note's VAT is at the original invoice line's rate. Item 6: a real payroll run collects after a rebate and credits 1240.
"""
from datetime import date

import pytest

from core.access.models import AccessLog
from invoicing import payroll_port as port_module
from invoicing import services as inv
from invoicing.conftest import zatca  # noqa: F401  (invoicing's fake ZATCA port, a fixture)
from invoicing.models import DeferredSale, DeferredSaleAdjustment, InvoicingError, SalesDocument, VatRate
from invoicing.test_deferred_sale import (  # noqa: F401  (the real port and its world)
    balance, bilingual, bw, collect, entry_by_account, invoicing, sale_doc, sold)
from payroll import invoicing_port as payroll_port
from payroll import services as payroll
from payroll.conftest import D, amount, approved, asset_bill, calculated, clock, entry_lines, pclock, saudi, w  # noqa: F401
from payroll.models import DeferredSaleContract, DeferredSaleRebate, PayrollAlert
from payroll.test_slice2_bai_ajel import bai_ajel


@pytest.fixture
def sold_contract(sold):
    w, s1, c, doc = sold
    payroll._refresh_deferred_sales(w.alm)
    return w, s1, DeferredSaleContract.objects.get(pk=c.pk), doc


def rebate(w, c, amount="600.00"):
    return payroll.prepare_deferred_sale_rebate(c, user=w.p, amount=amount, reason_ar="سداد مبكر",
                                                request_ref="REQ-EARLY-1")


def approve_rebate(w, r, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        return payroll.approve_deferred_sale_rebate(r, user=w.mp)


def note_of(r):
    return DeferredSaleAdjustment.objects.get(source_ref=f"payroll.deferredsalerebate:{r.pk}", active=True).credit_note


def collect_all(w, c):
    while c.installments.filter(status="planned").exists():
        collect(w, c, str(c.installments.filter(status="planned").order_by("period", "pk").first().amount))


# --- finding 1: no adjustment outside payroll's approval of that rebate -----------------------------------------------

def test_f1_a_direct_adjustment_call_is_refused(sold_contract, django_capture_on_commit_callbacks):
    w, s1, c, doc = sold_contract
    port = port_module.PayrollInvoicingPort()
    with pytest.raises(InvoicingError) as exc:  # the reproduction: the sale approver, a made-up reference
        with django_capture_on_commit_callbacks(execute=True):
            inv.create_deferred_sale_adjustment(c, approver=w.mp, source_ref="manual-1", amount="100.00",
                                                reason_ar="يدوي")
    assert exc.value.code == "rebate_not_from_payroll" and bilingual(exc.value)
    r = rebate(w, c)
    ref = f"payroll.deferredsalerebate:{r.pk}"
    with pytest.raises(InvoicingError) as exc:  # through the port, naming a real draft rebate: still no marker
        port.create_bai_ajel_rebate(c, user=w.mp, source_ref=ref, amount=D("600.00"), reason_ar="سداد مبكر")
    assert exc.value.code == "rebate_not_from_payroll"
    with payroll_port.rebating(r, w.mp):  # inside the marker but at another amount, or for another reference
        for kwargs in ({"source_ref": ref, "amount": "100.00"}, {"source_ref": "manual-1", "amount": "600.00"}):
            with pytest.raises(InvoicingError) as exc:
                inv.create_deferred_sale_adjustment(c, approver=w.mp, reason_ar="سداد مبكر", **kwargs)
            assert exc.value.code == "rebate_not_from_payroll"
    assert not SalesDocument.objects.filter(doc_type="credit_note").exists()
    assert not DeferredSaleAdjustment.objects.exists()
    assert AccessLog.objects.filter(event=AccessLog.Event.PERMISSION_DENIED, actor=w.mp,
                                    details__reason="rebate_not_from_payroll").count() == 4
    r = approve_rebate(w, r, django_capture_on_commit_callbacks)  # payroll's approval: issued
    assert r.status == "approved" and note_of(r).status == "issued"


# --- finding 2: only a final credit note counts -----------------------------------------------------------------------

def test_f2_a_pending_credit_note_changes_no_figures_and_profit_waits_prudently(sold_contract,
                                                                              django_capture_on_commit_callbacks):
    w, s1, c, doc = sold_contract
    sale = DeferredSale.objects.get(sales_document=doc)
    r = approve_rebate(w, rebate(w, c), django_capture_on_commit_callbacks)
    note = note_of(r)
    assert note.status == "issued"  # awaiting ZATCA reporting
    assert inv._sale_figures(sale) == (D("3600.00"), D("38640.00"))  # unchanged while pending
    state = port_module.PayrollInvoicingPort().sale_state(c)
    assert state.receivable == D("38640.00")
    assert (balance(w.alm, "1240"), balance(w.alm, "2200")) == (D("38640.00"), D("-5040.00"))  # nothing booked
    first, _ = collect(w, c, "1610.00")  # prudence: never recognise profit the pending rebate may waive
    assert first.profit_amount == D("127.27")  # 3,000.00 x 1,610.00 / 37,950.00, not 150.00
    w.zatca.report(note)
    assert inv._sale_figures(sale) == (D("3000.00"), D("37950.00"))
    collect_all(w, c)
    assert (balance(w.alm, "4610"), balance(w.alm, "1241")) == (D("-3000.00"), D("0.00"))
    assert balance(w.alm, "2200") == D("-5040.00") + D("90.00")


def test_f2_a_rejected_credit_note_is_excluded_restores_alerts_and_is_replaced(sold_contract,
                                                                             django_capture_on_commit_callbacks):
    w, s1, c, doc = sold_contract
    sale = DeferredSale.objects.get(sales_document=doc)
    r = approve_rebate(w, rebate(w, c), django_capture_on_commit_callbacks)
    first = note_of(r)
    w.zatca.reject(first)
    assert inv._sale_figures(sale) == (D("3600.00"), D("38640.00"))
    assert inv._sale_figures(sale, prudent=True) == (D("3600.00"), D("38640.00"))  # a rejected one never counts
    payroll._refresh_deferred_sales(w.alm)  # payroll reads the rejection (run calculation, settlement, daily job)
    r = DeferredSaleRebate.objects.get(pk=r.pk)
    assert r.status == "rejected" and sum(i.amount for i in c.installments.all()) == D("38640.00")
    alerts = PayrollAlert.objects.filter(kind="bai_ajel_rebate")
    assert {a.recipient for a in alerts} >= {w.p, w.mp} and first.number in alerts.first().text_ar
    r = approve_rebate(w, r, django_capture_on_commit_callbacks)  # replaced: a new credit note, four eyes again
    new = note_of(r)
    assert new.pk != first.pk and r.credit_note_ref == new.number and r.status == "approved"
    assert not DeferredSaleAdjustment.objects.get(credit_note=first).active
    assert sum(i.amount for i in c.installments.all()) == D("38640.00") - D("690.00")  # reduced once
    w.zatca.report(new)
    collect_all(w, c)
    assert (balance(w.alm, "4610"), balance(w.alm, "1241")) == (D("-3000.00"), D("0.00"))
    assert balance(w.alm, "2200") == D("-5040.00") + D("90.00")


def test_f2_a_rejected_credit_note_never_lowers_profit_or_receivable_when_everything_is_collected(
        sold_contract, django_capture_on_commit_callbacks):
    """The reproduction (rebate 600, note rejected, all 24 collected) now ends with the full profit and 1241 at 0."""
    w, s1, c, doc = sold_contract
    r = approve_rebate(w, rebate(w, c), django_capture_on_commit_callbacks)
    w.zatca.reject(note_of(r))
    payroll._refresh_deferred_sales(w.alm)
    collect_all(w, c)
    assert (balance(w.alm, "4610"), balance(w.alm, "1241")) == (D("-3600.00"), D("0.00"))
    assert balance(w.alm, "2200") == D("-5040.00")  # no credit note was ever booked


# --- finding 5: the credit note's VAT rate is the original invoice line's ---------------------------------------------

def test_f5_the_credit_note_takes_the_vat_rate_of_the_original_invoice_line(sold_contract,
                                                                           django_capture_on_commit_callbacks):
    w, s1, c, doc = sold_contract
    original = doc.lines.get(income_account__code="1241").vat_rate
    assert original.rate == D("15.00")
    VatRate.objects.create(category="S", rate=D("20.00"), valid_from=date(2027, 3, 20), name_en="Test 20%",
                           name_ar="نسبة اختبار")  # test data: a new rate in force on the note's date
    assert inv._rate_on("S", date(2027, 3, 20)).rate == D("20.00")
    r = approve_rebate(w, rebate(w, c), django_capture_on_commit_callbacks)
    note = note_of(r)
    assert [line.vat_rate_id for line in note.lines.all()] == [original.pk]
    assert (note.total_vat, note.total_gross, r.vat_amount) == (D("90.00"), D("690.00"), D("90.00"))


# --- item 6: a real payroll run collects after a rebate and credits 1240 ----------------------------------------------

def test_item6_a_real_run_collects_after_a_rebate_crediting_1240(bw, django_capture_on_commit_callbacks):
    w = bw
    s1 = saudi(w)
    c = bai_ajel(w, s1, cost="1200.00", profit="120.00", term=12)  # receivable 1,518.00: 126.50 x 12
    with django_capture_on_commit_callbacks(execute=True):
        c = payroll.approve_deferred_sale(c, user=w.mp)
    w.zatca.report(sale_doc(c))
    payroll._refresh_deferred_sales(w.alm)
    r = approve_rebate(w, rebate(w, DeferredSaleContract.objects.get(pk=c.pk), amount="60.00"),
                       django_capture_on_commit_callbacks)  # gross 69.00 off the last instalment
    w.zatca.report(note_of(r))
    assert balance(w.alm, "1240") == D("1518.00") - D("69.00")
    approved(w, "2027-03")
    apr = calculated(w, "2027-04")
    assert amount(apr, s1, "deferred_sale_installment") == D("126.50")
    apr = payroll.approve_run(payroll.submit_run(apr, user=w.p), user=w.h)
    assert entry_lines(apr)["1240"] == D("-126.50")  # the run itself credits 1240 (E11)
    assert balance(w.alm, "1240") == D("1518.00") - D("69.00") - D("126.50")
    release = DeferredSale.objects.get(source_id=c.pk, active=True).releases.get(status="posted")
    assert release.profit_amount == D("5.24")  # (120 - 60) x 126.50 / (1,518 - 69) = 5.238...
    assert (balance(w.alm, "4610"), balance(w.alm, "1241")) == (D("-5.24"), D("-120.00") + D("60.00") + D("5.24"))
