"""Payroll slice 2 (§10.8 item 8): bai' ajel wired to the real invoicing port (invoicing R112, E28; payroll R47 as
amended, E12, E13). The contract is sold once the sale invoice is posted on ZATCA reporting; each installment a run
collects (and a final-settlement recovery) recognises its profit share through invoicing; a run reversal gives it
back. These tests use payroll's fake port; payroll/test_slice2_bai_ajel_real.py runs the real port."""
from datetime import date

import pytest

from payroll import services
from payroll.conftest import D, amount, approved, asset_bill, calculated, saudi
from payroll.models import DeferredSaleContract, DeferredSaleInstallment, PayrollError


def bai_ajel(w, emp, cost="1200.00", profit="120.00", term=12, first="2027-04", undertaking=False):
    c = services.create_deferred_sale(emp, user=w.p, asset_description_ar="جهاز", asset_description_en="Device",
                                      cost=cost, profit_amount=profit, term_months=term, first_period=first,
                                      consent_ref="SIGNED-1", final_dues_undertaking=undertaking)
    c = services.approve_deferred_sale_terms(services.submit_deferred_sale_terms(c, user=w.p), user=w.mp)
    return services.record_asset_acquisition(c, user=w.p, purchase_bill=asset_bill(w, cost),
                                             possession_date=date(2027, 2, 16), possession_evidence_ref="DELIVERY-1")


# --- with payroll's fake port --------------------------------------------------------------------------------------

def test_r47_sold_only_once_the_invoice_is_posted_then_collected_with_profit_and_reversed(w):
    w.invoicing.posted = False
    s1 = saudi(w)
    c = services.approve_deferred_sale(bai_ajel(w, s1), user=w.mp, sale_date=date(2027, 3, 20))
    assert c.status == "sale_invoiced" and c.installments.count() == 12 and c.number.startswith("ALM-BAJ-")
    approved(w, "2027-03")
    apr = calculated(w, "2027-04")
    assert amount(apr, s1, "deferred_sale_installment") == 0  # the invoice is not posted yet: nothing collected
    w.invoicing.posted = True  # ZATCA reported it and invoicing posted it
    apr = services.calculate_run(apr, user=w.p)
    assert DeferredSaleContract.objects.get(pk=c.pk).status == "sold"
    assert amount(apr, s1, "deferred_sale_installment") == D("126.50")  # (1,320.00 + VAT 198.00) / 12
    apr = services.approve_run(services.submit_run(apr, user=w.p), user=w.h)
    inst = DeferredSaleInstallment.objects.get(contract=c, run=apr)
    assert w.invoicing.recognised == [(c.pk, inst.deducted_amount, date(2027, 4, 30),
                                       f"payroll.deferredsaleinstallment:{inst.pk}", w.h.pk)]
    services.reverse_run(apr, user=w.c, reason="wrong month")
    assert w.invoicing.reversed == [(c.pk, f"payroll.deferredsaleinstallment:{inst.pk}", w.c.pk)]


def test_r47_rejected_invoice_is_approved_again_never_while_it_is_live(w):
    w.invoicing.posted, w.invoicing.status = False, "reported_pending"
    s1 = saudi(w)
    c = services.approve_deferred_sale(bai_ajel(w, s1), user=w.mp, sale_date=date(2027, 3, 20))
    with pytest.raises(PayrollError) as exc:
        services.approve_deferred_sale(c, user=w.mp)
    assert exc.value.code == "sale_status"
    w.invoicing.status = "rejected"
    c = services.approve_deferred_sale(c, user=w.mp)
    assert c.status == "sale_invoiced" and c.sale_document_ref.endswith("-2") and len(w.invoicing.calls) == 2
    assert c.installments.count() == 12  # the same schedule, the same fixed price
    w.invoicing.posted, w.invoicing.status = True, "final"
    assert services._refresh_deferred_sales(w.alm) == [DeferredSaleContract.objects.get(pk=c.pk)]


def test_e13_final_settlement_recovery_recognises_the_profit_through_invoicing(w):
    from payroll.test_final_settlement import go_live, leaver, settle, terminate
    n1 = leaver(w)
    c = services.approve_deferred_sale(bai_ajel(w, n1, first="2027-03", undertaking=True), user=w.mp,
                                       sale_date=date(2027, 2, 20))
    t = terminate(w, n1)
    go_live(w)
    fs = services.approve_final_settlement(settle(w, t), user=w.c)
    recovered = sum(l.amount for l in fs.lines.filter(line_type="deferred_sale_recovery"))
    assert recovered == c.receivable
    assert sum(r[1] for r in w.invoicing.recognised) == recovered
    assert {r[2] for r in w.invoicing.recognised} == {fs.journal_entry.date} and {r[4] for r in w.invoicing.recognised} == {w.c.pk}
