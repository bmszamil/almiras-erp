"""Payroll review fixes of 2026-10-20, second round (payroll §10.9 items 11 to 15; invoicing R128 as amended), with
payroll's fake invoicing port (the real port: invoicing/test_deferred_sale_review2.py). Anonymised fixtures; the
payroll clock is fixed; nothing external is called.

Finding 1: the rebate credit note is asked for only inside payroll's ``rebating`` marker. Finding 2: a rejected credit
note restores the instalments, makes the rebate ``rejected`` and alerts. Finding 3: WHT base per payee. Finding 4: the
WHT parts backfill (payroll.0018) and the return's refusal without parts. Item 6: a rebate approved while a run or a
final settlement is calculated is caught by the approval fingerprint."""
import importlib
from datetime import date
from types import SimpleNamespace

import pytest
from django.core.exceptions import PermissionDenied
from django.apps import apps as django_apps
from django.db import connection, models

from payroll import invoicing_port, services
from payroll.conftest import D, amount, approved, calculated, rate, saudi, slip
from payroll.models import (DeferredSaleContract, DeferredSaleInstallment, DeferredSaleRebate, FinalSettlement,
                            PayrollAlert, PayrollError, PayrollPayment)
from payroll.test_final_settlement import go_live, leaver, settle, terminate
from payroll.test_review_fixes_2026_10_20 import bilingual, category, remuneration, remuneration_settlement
from payroll.test_slice2_bai_ajel import bai_ajel

backfill = importlib.import_module("payroll.migrations.0018_review_fixes_rebate_wht").backfill_wht_parts


def sold(w, emp=None, **kw):
    c = services.approve_deferred_sale(bai_ajel(w, emp or saudi(w), **kw), user=w.mp, sale_date=date(2027, 3, 20))
    services._refresh_deferred_sales(w.alm)
    return DeferredSaleContract.objects.get(pk=c.pk)


def rebate(w, c, amount="60.00"):
    return services.prepare_deferred_sale_rebate(c, user=w.p, amount=amount, reason_ar="سداد مبكر", request_ref="REQ-1")


def planned(c):
    return [(i.period, i.amount) for i in DeferredSaleInstallment.objects.filter(contract=c, status="planned")
            .order_by("period", "pk")]


# --- finding 1: only inside payroll's approval of that rebate ---------------------------------------------------------

def test_f1_the_port_is_called_only_inside_the_rebate_marker_of_that_rebate_by_its_approver(w):
    c = sold(w)
    r = rebate(w, c)
    ref = f"payroll.deferredsalerebate:{r.pk}"
    assert invoicing_port.rebate_problems(c, source_ref=ref, amount=r.amount, approver=w.mp)  # no marker
    with invoicing_port.rebating(r, w.mp):
        assert invoicing_port.rebate_problems(c, source_ref=ref, amount=r.amount, approver=w.mp) == []
        assert invoicing_port.rebate_problems(c, source_ref=ref, amount=D("61.00"), approver=w.mp)  # other amount
        assert invoicing_port.rebate_problems(c, source_ref="manual-1", amount=r.amount, approver=w.mp)
        assert invoicing_port.rebate_problems(c, source_ref=ref, amount=r.amount, approver=w.c)  # not its approver
        other = sold(w, first="2027-05")
        assert invoicing_port.rebate_problems(other, source_ref=ref, amount=r.amount, approver=w.mp)  # other contract
    with invoicing_port.rebating(r, w.p):  # the preparer never approves (four eyes)
        assert invoicing_port.rebate_problems(c, source_ref=ref, amount=r.amount, approver=w.p)
    with pytest.raises(PayrollError) as exc:  # the fake port mirrors the real one: no marker, no credit note
        w.invoicing.create_bai_ajel_rebate(c, user=w.mp, source_ref=ref, amount=r.amount, reason_ar="x")
    assert exc.value.code == "rebate_not_from_payroll" and not w.invoicing.rebates
    r = services.approve_deferred_sale_rebate(r, user=w.mp)  # payroll's approval sets the marker
    assert r.status == "approved" and w.invoicing.rebates == [(c.pk, ref, D("60.00"), w.mp.pk)]
    assert r.installment_cuts == [{"period": "2028-03", "amount": "69.00"}] and r.credit_note_status == "issued"


# --- finding 2: a rejected credit note restores the instalments and alerts --------------------------------------------

def test_f2_a_final_credit_note_is_recorded_and_never_read_again(w):
    c = sold(w)
    r = services.approve_deferred_sale_rebate(rebate(w, c, amount="120.00"), user=w.mp)  # gross 138.00
    assert planned(c)[-1] == ("2028-02", D("115.00")) and len(planned(c)) == 11  # 126.50 + 11.50 cut
    assert r.installment_cuts == [{"period": "2028-03", "amount": "126.50"}, {"period": "2028-02", "amount": "11.50"}]
    w.invoicing.rebate_status = "final"
    services._refresh_deferred_sales(w.alm)
    assert DeferredSaleRebate.objects.get(pk=r.pk).credit_note_status == "final"
    w.invoicing.rebate_status = "rejected"  # final is final: never read again
    services._refresh_deferred_sales(w.alm)
    assert DeferredSaleRebate.objects.get(pk=r.pk).status == "approved" and len(planned(c)) == 11
    assert not PayrollAlert.objects.filter(kind="bai_ajel_rebate").exists()


def test_f2_rejection_restores_alerts_payroll_and_the_approver_then_reapproval_cuts_again(w):
    c = sold(w)
    before = planned(c)
    r = services.approve_deferred_sale_rebate(rebate(w, c, amount="120.00"), user=w.mp)
    assert sum(a for _, a in planned(c)) == sum(a for _, a in before) - D("138.00")
    w.invoicing.rebate_status = "rejected"
    assert [x.pk for x in services._refresh_rebates(w.alm)] == [r.pk]
    r = DeferredSaleRebate.objects.get(pk=r.pk)
    assert (r.status, r.credit_note_status) == ("rejected", "rejected")
    assert planned(c) == before  # nothing was waived: every cut is back in its own period
    alerts = PayrollAlert.objects.filter(kind="bai_ajel_rebate")
    assert {a.recipient for a in alerts} >= {w.p, w.mp} and r.credit_note_ref in alerts.first().text_en
    assert r.credit_note_ref in alerts.first().text_ar and c.number in alerts.first().text_en
    with pytest.raises(PayrollError) as exc:  # the rejected rebate is still open
        rebate(w, c)
    assert exc.value.code == "rebate_open"
    with pytest.raises(PermissionDenied):  # four eyes: never the preparer
        services.approve_deferred_sale_rebate(r, user=w.p)
    w.invoicing.rebate_status = "issued"
    r = services.approve_deferred_sale_rebate(r, user=w.mp)  # four eyes again: a new credit note, cuts again
    assert r.status == "approved" and len(w.invoicing.rebates) == 2
    assert sum(a for _, a in planned(c)) == sum(a for _, a in before) - D("138.00")
    w.invoicing.rebate_status = "rejected"
    services._refresh_deferred_sales(w.alm)
    r = services.discard_deferred_sale_rebate(DeferredSaleRebate.objects.get(pk=r.pk), user=w.p)
    assert r.status == "discarded" and planned(c) == before


def test_f2_a_rejection_after_a_cut_period_was_collected_moves_the_amount_after_the_last_instalment(w):
    c = sold(w, cost="200.00", profit="100.00", term=2)  # receivable 345.00: 172.50 x 2 (April, May)
    approved(w, "2027-03")
    r = services.approve_deferred_sale_rebate(rebate(w, c, amount="100.00"), user=w.mp)  # gross 115.00 off May
    assert planned(c) == [("2027-04", D("172.50")), ("2027-05", D("57.50"))]
    services.approve_run(services.submit_run(calculated(w, "2027-04"), user=w.p), user=w.h)
    services.approve_run(services.submit_run(calculated(w, "2027-05"), user=w.p), user=w.h)
    assert planned(c) == []
    w.invoicing.rebate_status = "rejected"
    services._refresh_deferred_sales(w.alm)
    assert planned(c) == [("2027-06", D("115.00"))]  # May was collected: the 115.00 still owed comes next
    assert DeferredSaleRebate.objects.get(pk=r.pk).status == "rejected"


# --- item 6: a rebate approved while a run or a settlement is calculated ----------------------------------------------

def test_item6_a_rebate_approved_after_the_run_was_calculated_is_caught_by_the_approval_fingerprint(w):
    s1 = saudi(w)
    c = sold(w, s1, cost="1000.00", profit="1000.00", term=2)  # receivable 2,300.00: 1,150.00 x 2
    approved(w, "2027-03")
    services.approve_run(services.submit_run(calculated(w, "2027-04"), user=w.p), user=w.h)
    may = services.submit_run(calculated(w, "2027-05"), user=w.p)
    assert amount(may, s1, "deferred_sale_installment") == D("1150.00")
    services.approve_deferred_sale_rebate(rebate(w, c, amount="500.00"), user=w.mp)  # gross 575.00 off May
    with pytest.raises(PayrollError) as exc:
        services.approve_run(may, user=w.h)
    assert exc.value.code == "recalculate" and bilingual(exc.value)
    may = services.return_run(may, user=w.h, reason_ar="أعد الاحتساب")
    may = services.submit_run(services.calculate_run(may, user=w.p), user=w.p)
    assert amount(may, s1, "deferred_sale_installment") == D("575.00")
    services.approve_run(may, user=w.h)


def test_item6_a_rebate_approved_after_the_settlement_was_calculated_is_caught_by_the_approval_fingerprint(w):
    n1 = leaver(w)
    c = sold(w, n1, first="2027-03", undertaking=True)
    t = terminate(w, n1)
    go_live(w)
    fs = settle(w, t)
    recovery = sum(l.amount for l in fs.lines.filter(line_type="deferred_sale_recovery"))
    assert recovery == c.receivable
    services.approve_deferred_sale_rebate(rebate(w, c, amount="60.00"), user=w.mp)
    with pytest.raises(PayrollError) as exc:
        services.approve_final_settlement(FinalSettlement.objects.get(pk=fs.pk), user=w.c)
    assert exc.value.code == "recalculate"
    services.return_final_settlement(FinalSettlement.objects.get(pk=fs.pk), user=w.c, reason_ar="أعد الاحتساب")
    fs = settle(w, t)
    assert sum(l.amount for l in fs.lines.filter(line_type="deferred_sale_recovery")) == c.receivable - D("69.00")


# --- finding 3: the WHT base is per payee ------------------------------------------------------------------------------

def test_f3_wht_base_per_payee_from_the_payslips_a_payment_names(w):
    r1, r2 = remuneration(w), remuneration(w)
    rate("USD", "SAR", date(2027, 3, 31), "3.7500")
    rate("USD", "SAR", date(2027, 4, 8), "3.7600")
    run = approved(w, payroll_group="out_of_country")
    s1, s2, cat = slip(run, r1), slip(run, r2), category()
    assert (s1.net_txn, s2.net_txn) == (D("5000.000"), D("5000.000"))
    with pytest.raises(PayrollError) as exc:  # a part payment names its payees
        services.record_payment(w.alm, user=w.p, kind="net_salaries", run=run, payment_date=date(2027, 4, 8),
                                amount="5000.00", currency="USD", wht_category=cat)
    assert exc.value.code == "payees_required" and bilingual(exc.value)
    with pytest.raises(PayrollError) as exc:
        services.record_payment(w.alm, user=w.p, kind="net_salaries", run=run, payment_date=date(2027, 4, 8),
                                amount="5000.01", currency="USD", wht_category=cat, payslips=[s1])
    assert exc.value.code == "overpayment"
    with pytest.raises(PayrollError) as exc:
        services.record_payment(w.alm, user=w.p, kind="net_salaries", run=run, payment_date=date(2027, 4, 8),
                                amount="10.00", currency="USD", wht_category=cat, payslips=[999999])
    assert exc.value.code == "payslips_invalid"
    p1 = services.record_payment(w.alm, user=w.p, kind="net_salaries", run=run, payment_date=date(2027, 4, 8),
                                 amount="5000.00", currency="USD", wht_category=cat, payslips=[s1])
    p1 = services.approve_payment(services.submit_payment(p1, user=w.p), user=w.c)
    # r1's whole gross is paid by this payment: 5% of 5,000.00 (pro rata to the run it would have been half)
    assert p1.payslip_parts == [{"payslip_id": s1.pk, "amount": "5000.00"}]
    assert p1.wht_amount == D("250.000") and p1.wht_base_sar == D("18800.00") and p1.wht_amount_sar == D("940.00")
    assert [part["employee_code"] for part in p1.wht_parts] == [r1.employee_code]
    p2 = services.record_payment(w.alm, user=w.p, kind="net_salaries", run=run, payment_date=date(2027, 4, 8),
                                 amount="5000.00", currency="USD", wht_category=cat)
    p2 = services.approve_payment(services.submit_payment(p2, user=w.p), user=w.c)  # the rest: r2 only
    assert [part["payslip_id"] for part in p2.payslip_parts] == [s2.pk]
    assert [part["employee_code"] for part in p2.wht_parts] == [r2.employee_code] and p2.wht_amount == D("250.000")


def test_f3_a_part_payment_named_for_two_payees_is_split_by_what_is_open_on_each(w):
    r1, r2 = remuneration(w), remuneration(w)
    rate("USD", "SAR", date(2027, 3, 31), "3.7500")
    rate("USD", "SAR", date(2027, 4, 8), "3.7500")
    run = approved(w, payroll_group="out_of_country")
    p = services.record_payment(w.alm, user=w.p, kind="net_salaries", run=run, payment_date=date(2027, 4, 8),
                                amount="1000.01", currency="USD", wht_category=category(),
                                payslips=[slip(run, r1), slip(run, r2)])
    p = services.approve_payment(services.submit_payment(p, user=w.p), user=w.c)
    parts = sorted(D(x["amount"]) for x in p.payslip_parts)
    assert sum(parts) == D("1000.01") and parts == [D("500.00"), D("500.01")]  # the parts add up to the payment


# --- finding 4: WHT parts of payments approved before payroll.0017 -----------------------------------------------------

def _wipe_parts(p):
    """Test data: a payment approved before payroll.0017 had no per-payee parts (approved rows are locked on
    PostgreSQL, so the lock is lifted for this one test update, as the migration itself does)."""
    with connection.cursor() as cursor:
        if connection.vendor == "postgresql":
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            cursor.execute("ALTER TABLE payroll_payrollpayment DISABLE TRIGGER payroll_payment_lock")
        models.QuerySet.update(PayrollPayment.objects.filter(pk=p.pk), wht_parts=[])
        if connection.vendor == "postgresql":
            cursor.execute("ALTER TABLE payroll_payrollpayment ENABLE TRIGGER payroll_payment_lock")


def _editor():
    def execute(sql):
        with connection.cursor() as cursor:
            cursor.execute(sql)
    return SimpleNamespace(connection=connection, execute=execute)


def test_f4_return_refused_without_parts_then_backfilled_from_what_was_booked(w):
    from invoicing import services as inv
    from invoicing.models import InvoicingError

    r1, fs = remuneration_settlement(w)
    p = services.record_payment(w.alm, user=w.p, kind="final_settlement", final_settlement=fs,
                                payment_date=date(2027, 3, 20), amount="5000.00", wht_category=category())
    p = services.approve_payment(services.submit_payment(p, user=w.p), user=w.c)
    _wipe_parts(p)
    with pytest.raises((PayrollError, InvoicingError)) as exc:
        inv.wht_return_data(w.alm, date(2027, 3, 1), user=w.a)
    assert getattr(exc.value, "code", "") == "wht_parts_missing" or "wht_parts_missing" in str(exc.value.messages)
    backfill(django_apps, _editor())
    p.refresh_from_db()
    assert p.wht_parts == [{"employee_id": r1.pk, "employee_code": r1.employee_code, "country": "EG",
                            "base_sar": "6000.00", "wht_sar": "300.00", "backfilled": True}]
    data = inv.wht_return_data(w.alm, date(2027, 3, 1), user=w.a)
    rows = [r for r in data["rows"] if r.get("source") == "payroll"]
    assert [(r["vendor"], r["wht_sar"], r["base_sar"]) for r in rows] == [(r1.employee_code, D("300.00"),
                                                                           D("6000.00"))]
    assert data["difference"] == 0
    backfill(django_apps, _editor())  # idempotent: filled rows are left alone
    p.refresh_from_db()
    assert len(p.wht_parts) == 1


def test_f4_a_run_payment_is_backfilled_over_its_remuneration_payslips_by_net(w):
    r1, r2 = remuneration(w), remuneration(w)
    rate("USD", "SAR", date(2027, 3, 31), "3.7500")
    rate("USD", "SAR", date(2027, 4, 8), "3.7500")
    run = approved(w, payroll_group="out_of_country")
    p = services.record_payment(w.alm, user=w.p, kind="net_salaries", run=run, payment_date=date(2027, 4, 8),
                                amount="10000.00", currency="USD", wht_category=category())
    p = services.approve_payment(services.submit_payment(p, user=w.p), user=w.c)
    booked = (p.wht_base_sar, p.wht_amount_sar)
    _wipe_parts(p)
    backfill(django_apps, _editor())
    p.refresh_from_db()
    assert {x["employee_code"] for x in p.wht_parts} == {r1.employee_code, r2.employee_code}
    assert (sum(D(x["base_sar"]) for x in p.wht_parts), sum(D(x["wht_sar"]) for x in p.wht_parts)) == booked
