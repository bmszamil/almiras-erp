"""Payroll slice 2 (§10.8 item 8) with the real invoicing port (invoicing.payroll_port, invoicing R112, E28) and
invoicing's fake ZATCA: the sale invoice posts on reporting, the contract becomes sold, a run collects the first
installment and its profit share moves from 1241 to 4610 (E12); reversing the run gives it back. Nothing external is
called; the Riyadh clock is fixed by the invoicing fixture."""
from datetime import date

from invoicing.conftest import zatca  # noqa: F401  (invoicing's fake ZATCA port, a fixture)
from invoicing.models import DeferredProfitRelease
from invoicing.test_deferred_sale import bw, invoicing, sale_doc  # noqa: F401  (the real port and its world)
from payroll import services
from payroll.conftest import D, amount, approved, calculated, saudi
from payroll.models import DeferredSaleContract, DeferredSaleInstallment
from payroll.test_slice2_bai_ajel import bai_ajel


def test_real_port_sale_posted_on_reporting_then_runs_collect_and_profit_moves_to_4610(bw, django_capture_on_commit_callbacks):
    w = bw
    s1 = saudi(w)
    c = bai_ajel(w, s1, cost="1200.00", profit="120.00", term=12)
    with django_capture_on_commit_callbacks(execute=True):
        c = services.approve_deferred_sale(c, user=w.mp)
    assert c.status == "sale_invoiced" and c.vat_amount == D("198.00") and c.receivable == D("1518.00")
    w.zatca.report(sale_doc(c))
    approved(w, "2027-03")
    apr = calculated(w, "2027-04")
    assert DeferredSaleContract.objects.get(pk=c.pk).status == "sold"
    assert amount(apr, s1, "deferred_sale_installment") == D("126.50")  # 1,518.00 / 12
    apr = services.approve_run(services.submit_run(apr, user=w.p), user=w.h)
    release = DeferredProfitRelease.objects.get(deferred_sale__source_id=c.pk, status="posted")
    assert release.collected_amount == D("126.50") and release.profit_amount == D("10.00")  # 120 x 126.50 / 1,518
    lines = {l.account.code: l.debit - l.credit for l in release.journal_entry.lines.select_related("account")}
    assert lines == {"1241": D("10.00"), "4610": D("-10.00")} and release.journal_entry.date == date(2027, 4, 30)
    services.reverse_run(apr, user=w.c, reason="wrong month")
    assert DeferredProfitRelease.objects.get(pk=release.pk).status == "reversed"
    assert DeferredSaleInstallment.objects.filter(contract=c, status="planned").count() == 12
