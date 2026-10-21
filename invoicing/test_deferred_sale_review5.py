"""Bai' ajel balance after exit, fifth payroll review of 2026-10-21 (payroll §10.9 items 30 to 38; invoicing R128 note;
payroll OQ-33), with the real payroll invoicing port, real final settlements, invoicing's fake ZATCA and a fixed Riyadh
clock; anonymised fixtures, nothing external. Ported from the reviewer's test_zz_rv5_real.py.

The fixture of review 3 sells 1,200.00 + 120.00 profit + 198.00 VAT (receivable 1,518.00); the final settlement
collects 1,449.00 while a rebate note (60.00, gross 69.00) is pending, so prudence held back profit; ZATCA then rejects
the note. Item 3: on that rejection invoicing catches up the profit held back on the 1,449.00 already collected
(120.00 x 1,449.00 / 1,518.00 = 114.55, of which 60.00 was recognised), so 1241 holds only the profit on the 69.00
still outstanding (5.45) and a write-off no longer depends on whether a recovery came first."""
from datetime import date
from decimal import ROUND_HALF_UP

import pytest

from invoicing import services as inv
from invoicing.conftest import zatca  # noqa: F401  (invoicing's fake ZATCA port, a fixture)
from invoicing.models import DeferredProfitRelease, InvoicingError
from invoicing.test_deferred_sale import balance, bw, invoicing  # noqa: F401  (the real port and its world)
from invoicing.test_deferred_sale_review2 import note_of, rebate  # noqa: F401
from invoicing.test_deferred_sale_review3 import after_exit, books, settled_with_rejected_rebate  # noqa: F401
from invoicing.test_deferred_sale_review4 import left_after_exit  # noqa: F401
from ledger.models import JournalLine
from payroll import invoicing_port as payroll_port
from payroll import services as payroll
from payroll.conftest import D, clock, pclock, w  # noqa: F401
from payroll.models import DeferredSaleContract, DeferredSaleRebate, PayrollError

PROFIT, RECEIVABLE, COLLECTED = D("120.00"), D("1518.00"), D("1449.00")


def profit_to_date(collected):
    return (PROFIT * collected / RECEIVABLE).quantize(D("0.01"), rounding=ROUND_HALF_UP)


def _approve(w, x):
    return payroll.approve_deferred_sale_closure(payroll.submit_deferred_sale_closure(x, user=w.p), user=w.mp)


def _write_off(w, c):
    return _approve(w, payroll.prepare_deferred_sale_closure(DeferredSaleContract.objects.get(pk=c.pk), user=w.p,
                                                             kind="write_off", reason_ar="تعذر التحصيل"))


def _recover(w, c, amount, account="1100"):
    return _approve(w, payroll.prepare_deferred_sale_closure(
        DeferredSaleContract.objects.get(pk=c.pk), user=w.p, kind="recovery", amount=amount, reason_ar="سداد",
        bank_account=payroll._account(w.alm, account), received_on=date(2027, 3, 20)))


# --- item 3: the profit held back is caught up when the rebate note is rejected ------------------------------------

def test_item3_the_rejection_catches_up_the_held_back_profit_through_the_ledger(settled_with_rejected_rebate):
    w, c, r = settled_with_rejected_rebate
    assert r.status == "rejected"
    caught = DeferredProfitRelease.objects.get(source_ref=payroll_port.catch_up_ref(r))
    assert (caught.collected_amount, caught.collected_to_date) == (D("0.00"), COLLECTED)
    assert caught.profit_amount == profit_to_date(COLLECTED) - D("60.00") == D("54.55")
    assert caught.created_by_id == r.approved_by_id  # the rebate's approver; the deferred-sale system user posts
    entry = caught.journal_entry
    assert entry.status == "posted" and entry.document_type == "invoicing.deferredprofitrelease"
    assert sorted((l.account.code, l.debit, l.credit) for l in JournalLine.objects.filter(entry=entry)) == [
        ("1241", D("54.55"), D("0")), ("4610", D("0"), D("54.55"))]
    assert books(w) == {"1240": D("69.00"), "1241": D("-5.45"), "4610": D("-114.55"), "2200": D("-198.00")}
    # read again (alerts job, next run): never twice
    payroll._refresh_deferred_sales(w.alm)
    assert DeferredProfitRelease.objects.filter(source_ref__contains=":catch_up").count() == 1
    assert books(w)["4610"] == D("-114.55")


def test_item3_the_catch_up_is_refused_outside_payroll_s_marker(settled_with_rejected_rebate):
    w, c, r = settled_with_rejected_rebate
    for ref in (payroll_port.catch_up_ref(r), "payroll.deferredsalerebate:1:catch_up:x"):
        with pytest.raises(InvoicingError) as exc:
            inv.catch_up_deferred_profit(c, source_ref=ref, on=date(2027, 3, 20), approver=w.mp)
        assert exc.value.code == "collection_not_from_payroll"
    with payroll_port.catching_up(r), pytest.raises(InvoicingError) as exc:  # not the rebate's approver
        inv.catch_up_deferred_profit(c, source_ref=payroll_port.catch_up_ref(r), on=date(2027, 3, 20), approver=w.p)
    assert exc.value.code == "collection_not_from_payroll"


def test_item3_write_off_straight_leaves_only_the_profit_on_the_outstanding_balance(left_after_exit):
    w, c = left_after_exit
    expense = balance(w.alm, "5100")
    x = _write_off(w, c)
    unearned = PROFIT - profit_to_date(COLLECTED)
    assert x.profit_share == unearned == D("5.45")
    assert books(w) == {"1240": D("0.00"), "1241": D("0.00"), "4610": D("-114.55"), "2200": D("-198.00")}
    assert balance(w.alm, "5100") - expense == D("69.00") - unearned == D("63.55")


def test_item3_and_9_partial_recovery_then_write_off_on_the_real_port_differs_only_by_what_was_recovered(
        left_after_exit):
    """The reviewer's r2: 30.00 recovered, then the 39.00 left written off. Before the fix the recovery released the
    held-back profit (4610 -116.92 against -60.00 for a straight write-off). Now the two paths differ only by the
    recovery itself: its profit share (2.37) on 4610 and the cash (30.00) less that share on 5100; 1241 ends at zero
    in both and income less loss less cash recovered is the same (51.00)."""
    w, c = left_after_exit
    expense, bank = balance(w.alm, "5100"), balance(w.alm, "1100")
    _recover(w, c, "30.00")
    share = profit_to_date(COLLECTED + D("30.00")) - profit_to_date(COLLECTED)
    assert share == D("2.37")
    assert books(w) == {"1240": D("39.00"), "1241": D("-3.08"), "4610": D("-116.92"), "2200": D("-198.00")}
    y = _write_off(w, c)
    assert (y.amount, y.profit_share) == (D("39.00"), D("3.08"))
    assert books(w) == {"1240": D("0.00"), "1241": D("0.00"), "4610": D("-114.55") - share, "2200": D("-198.00")}
    loss = balance(w.alm, "5100") - expense
    assert loss == D("63.55") - (D("30.00") - share) == D("35.92")
    assert balance(w.alm, "1100") - bank == D("30.00")
    straight = D("114.55") - D("63.55")  # income less loss of the straight write-off (previous test)
    assert (D("114.55") + share) - loss - D("30.00") == straight == D("51.00")
    assert DeferredSaleContract.objects.get(pk=c.pk).status == "settled"


# --- item 1 on the real port (reviewer's r1) ----------------------------------------------------------------------

@pytest.mark.parametrize("code", ["1240", "1250"])
def test_item1_a_recovery_into_1240_or_1250_recognises_nothing_and_is_refused(left_after_exit, code):
    w, c = left_after_exit
    before = books(w)
    with pytest.raises(PayrollError) as exc:
        _recover(w, c, "69.00", account=code)
    assert exc.value.code == "bank_account_invalid"
    assert books(w) == before and DeferredSaleContract.objects.get(pk=c.pk).status == "sold"


# --- item 5 on the real port: the VAT share stays on 2200 ---------------------------------------------------------

def test_item5_the_vat_in_the_written_off_balance_stays_on_2200_and_is_listed(left_after_exit):
    w, c = left_after_exit
    x = _write_off(w, c)
    assert x.vat_share == D("9.00")  # 69.00 x 198.00 / 1,518.00
    assert books(w)["2200"] == D("-198.00")
    rows = payroll.bai_ajel_write_off_vat_list(w.alm, user=w.mp)
    assert [(r["closure"], r["vat_share"]) for r in rows] == [(x.number, D("9.00"))]
    assert DeferredSaleRebate.objects.filter(contract=c, status="discarded").exists()
