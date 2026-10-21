"""Bai' ajel balance left after exit (payroll review 4 of 2026-10-21, item 5; payroll §10.9 item 27, invoicing R128
note), with the real payroll invoicing port, real final settlements, invoicing's fake ZATCA and a fixed Riyadh clock;
anonymised fixtures, nothing external.

The fixture of review 3 leaves 69.00 outstanding after exit on 1240 (a rebate credit note rejected after the final
settlement) with 60.00 of profit not yet recognised on 1241. A write-off clears 1240 and 1241 (the unearned profit is
never income, the rest is the loss on 5100); a recovery collects 1240 into the bank and invoicing recognises the
profit on it through the port, so 4610 ends at the full fixed profit."""
from datetime import date

import pytest
from django.core.exceptions import PermissionDenied

from invoicing.conftest import zatca  # noqa: F401  (invoicing's fake ZATCA port, a fixture)
from invoicing.test_deferred_sale import balance, bw, invoicing  # noqa: F401  (the real port and its world)
from invoicing.test_deferred_sale_review2 import note_of, rebate  # noqa: F401
from invoicing.test_deferred_sale_review3 import after_exit, books, settled_with_rejected_rebate  # noqa: F401
from payroll import services as payroll
from payroll.conftest import D, clock, pclock, w  # noqa: F401
from payroll.models import DeferredSaleContract, PayrollError


@pytest.fixture
def left_after_exit(settled_with_rejected_rebate):
    w, c, r = settled_with_rejected_rebate
    payroll.discard_deferred_sale_rebate(r, user=w.p)
    # review 5 (item 3): the profit held back while the note was pending was caught up when it was rejected, so 1241
    # holds only the profit on the 69.00 still outstanding (120.00 x 69.00 / 1,518.00)
    assert books(w) == {"1240": D("69.00"), "1241": D("-5.45"), "4610": D("-114.55"), "2200": D("-198.00")}
    return w, DeferredSaleContract.objects.get(pk=c.pk)


def test_item5_write_off_clears_1240_and_the_unearned_profit_on_1241(left_after_exit):
    w, c = left_after_exit
    x = payroll.prepare_deferred_sale_closure(c, user=w.p, kind="write_off", reason_ar="تعذر التحصيل من الموظف السابق")
    assert x.profit_share == D("5.45")  # read from invoicing: the fixed profit less what was recognised
    expense = balance(w.alm, "5100")
    x = payroll.approve_deferred_sale_closure(payroll.submit_deferred_sale_closure(x, user=w.p), user=w.mp)
    assert x.profit_share == D("5.45") and x.journal_entry.status == "posted"
    assert books(w) == {"1240": D("0.00"), "1241": D("0.00"), "4610": D("-114.55"), "2200": D("-198.00")}
    assert balance(w.alm, "5100") - expense == D("63.55")  # the loss: the cost and VAT part of the balance
    assert after_exit(c) == D("0.00") and DeferredSaleContract.objects.get(pk=c.pk).status == "settled"


def test_item5_recovery_in_two_parts_collects_1240_and_recognises_the_rest_of_the_profit(left_after_exit):
    w, c = left_after_exit
    bank = payroll._account(w.alm, "1100")
    first = payroll.prepare_deferred_sale_closure(c, user=w.p, kind="recovery", amount="30.00", reason_ar="سداد جزئي",
                                                  bank_account=bank, received_on=date(2027, 3, 20))
    payroll.approve_deferred_sale_closure(payroll.submit_deferred_sale_closure(first, user=w.p), user=w.mp)
    b = books(w)
    assert b["1240"] == D("39.00") and -(b["1241"] + b["4610"]) == D("120.00") and b["4610"] < D("-114.55")
    second = payroll.prepare_deferred_sale_closure(c, user=w.p, kind="recovery", amount="39.00", reason_ar="سداد",
                                                   bank_account=bank, received_on=date(2027, 3, 20))
    payroll.approve_deferred_sale_closure(payroll.submit_deferred_sale_closure(second, user=w.p), user=w.mp)
    # the collection completing the receivable releases the rest: the whole fixed profit is income, nothing deferred
    assert books(w) == {"1240": D("0.00"), "1241": D("0.00"), "4610": D("-120.00"), "2200": D("-198.00")}
    assert DeferredSaleContract.objects.get(pk=c.pk).status == "settled"


def test_item5_the_unearned_profit_is_not_recognised_by_anyone_calling_invoicing_directly(left_after_exit):
    from invoicing import services as inv
    from invoicing.models import InvoicingError

    w, c = left_after_exit
    inst = c.installments.get(status="outstanding_after_exit")
    with pytest.raises(InvoicingError) as exc:
        inv.recognise_deferred_profit(c, user=w.mp, collected_amount="69.00", collected_on=date(2027, 3, 18),
                                      source_ref=f"payroll.deferredsaleinstallment:{inst.pk}")
    assert exc.value.code == "collection_not_from_payroll"
    x = payroll.prepare_deferred_sale_closure(c, user=w.p, kind="write_off", reason_ar="سبب")
    x = payroll.submit_deferred_sale_closure(x, user=w.p)
    with pytest.raises(PermissionDenied):  # the approver role holds no write-off right (the MP does)
        payroll.approve_deferred_sale_closure(x, user=w.c)
    with pytest.raises(PayrollError):  # one open closure per contract
        payroll.prepare_deferred_sale_closure(c, user=w.p, kind="write_off", reason_ar="سبب")
