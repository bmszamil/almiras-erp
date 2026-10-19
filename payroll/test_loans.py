"""Loans and bai' ajel (R26, R27, R46 to R49; E4, E10/E28, E11; AT-17, AT-29 to AT-31, AT-57, AT-66)."""
from datetime import date

import pytest
from django.core.exceptions import PermissionDenied

from payroll import services
from payroll.conftest import D, amount, approved, calculated, saudi, slip
from payroll.invoicing_port import NotBuiltInvoicingPort
from payroll.models import DeferredSaleInstallment, EmployeeLoan, PayrollError


def loan(w, emp, principal="3000.00", installments=(("2027-03", "1000.00"), ("2027-04", "1000.00"),
                                                     ("2027-05", "1000.00")), kind="loan", **kw):
    return services.create_loan(emp, user=w.p, kind=kind, principal=principal, disbursement_date=date(2027, 2, 1),
                                purpose_ar="قرض حسن", installments=list(installments), **kw)


def test_at17_schedule_sum_no_fee_disbursement_and_no_skip(w):
    s1 = saudi(w)
    with pytest.raises(PayrollError) as exc:
        loan(w, s1, installments=[("2027-03", "1100.00"), ("2027-04", "1100.00"), ("2027-05", "1100.00")])
    assert exc.value.code == "schedule_sum"
    with pytest.raises(PayrollError) as exc:
        loan(w, s1, admin_fee="50.00")
    assert exc.value.code == "sharia_no_charge" and "/" in exc.value.messages[0]
    sub_emp = saudi(w, w.sub)
    with pytest.raises(PayrollError):  # every entity, also without Sharia enforcement
        services.create_loan(sub_emp, user=w.ps, kind="loan", principal="100.00", disbursement_date=date(2027, 2, 1),
                             purpose_ar="قرض", installments=[("2027-03", "100.00")], interest_rate="1")
    l = services.submit_loan(loan(w, s1), user=w.p)
    with pytest.raises(PermissionDenied):
        services.approve_loan(l, user=w.c)  # the CFO does not approve loans (P12)
    l = services.approve_loan(l, user=w.mp)
    lines = {x.account.code: (x.debit, x.credit) for x in l.journal_entry.lines.select_related("account")}
    assert lines == {"1230": (D("3000.00"), D("0")), "1100": (D("0"), D("3000.00"))} and l.interest_free
    with pytest.raises(PayrollError):
        services.skip_installment(l.installments.first(), user=w.p)


def test_at17_reschedule_is_a_new_approved_version_with_the_same_total(w):
    s1 = saudi(w)
    l = services.approve_loan(services.submit_loan(loan(w, s1), user=w.p), user=w.mp)
    with pytest.raises(PayrollError):
        services.propose_schedule(l, user=w.p, installments=[("2027-04", "3000.01")])
    services.propose_schedule(l, user=w.p, installments=[("2027-04", "1500.00"), ("2027-06", "1500.00")])
    l = services.approve_schedule(l, user=w.mp)
    assert l.schedule_version == 2
    run = calculated(w)
    assert amount(run, s1, "loan_installment") == 0  # March skipped through the new schedule


def test_at29_cap_three_basic_salaries_advances_included(w):
    s1 = saudi(w)
    big = services.submit_loan(loan(w, s1, principal="30000.01", installments=[("2027-03", "30000.01")]), user=w.p)
    with pytest.raises(PayrollError) as exc:
        services.approve_loan(big, user=w.mp)
    assert "30000.00" in exc.value.messages[0] and "bai' ajel" in exc.value.messages[0]
    ok = services.approve_loan(services.submit_loan(loan(w, s1, principal="30000.00",
                                                         installments=[("2027-06", "30000.00")]), user=w.p), user=w.mp)
    assert ok.cap_amount == D("30000.00") and ok.cap_basic_at_approval == D("10000.00")
    adv = services.submit_loan(loan(w, s1, kind="salary_advance", principal="1000.00",
                                    installments=[("2027-04", "1000.00")]), user=w.p)
    with pytest.raises(PayrollError):
        services.approve_loan(adv, user=w.mp)


def test_r27_loan_settles_when_fully_deducted(w):
    s1 = saudi(w)
    l = services.approve_loan(services.submit_loan(loan(w, s1, principal="900.00",
                                                        installments=[("2027-03", "900.00")]), user=w.p), user=w.mp)
    run = approved(w)
    assert slip(run, s1).loan_balance == D("0.00")
    assert EmployeeLoan.objects.get(pk=l.pk).status == "settled"


def sale(w, emp, cost="30000.00", profit="3600.00", term=24, consent="SIGNED-BAJ-1", **kw):
    c = services.create_deferred_sale(emp, user=w.p, asset_description_ar="سيارة", cost=cost, profit_amount=profit,
                                      term_months=term, first_period="2027-03", consent_ref=consent, **kw)
    return services.approve_deferred_sale_terms(services.submit_deferred_sale_terms(c, user=w.p), user=w.mp)


def test_at30_at57_bai_ajel_sale_through_invoicing_and_installments(w):
    s1 = saudi(w)
    c = sale(w, s1)
    with pytest.raises(PermissionDenied):
        services.approve_deferred_sale(c, user=w.p)
    c = services.approve_deferred_sale(c, user=w.mp)
    assert c.status == "sold" and c.vat_amount == D("5040.00") and c.receivable == D("38640.00")
    assert c.number == "ALM-BAJ-2027-000001" and w.invoicing.calls
    inst = DeferredSaleInstallment.objects.filter(contract=c)
    assert inst.count() == 24 and sum(i.amount for i in inst) == D("38640.00")
    run = approved(w)
    assert amount(run, s1, "deferred_sale_installment") == D("1610.00")  # within 50%, no 10% cap (P19(k))
    assert dict((l.account.code, l.credit) for l in run.journal_entry.lines.select_related("account")
                if l.account.code == "1240") == {"1240": D("1610.00")}


def test_at66_vat_charged_to_the_employee_in_the_installments(w):
    s1 = saudi(w)
    c = services.approve_deferred_sale(sale(w, s1, cost="100.00", profit="10.00", term=2), user=w.mp)
    assert [i.amount for i in c.installments.order_by("period")] == [D("63.25"), D("63.25")]


def test_r47_without_invoicing_the_sale_is_refused(w):
    from payroll import invoicing_port
    s1 = saudi(w)
    c = sale(w, s1)
    prev, invoicing_port._override = invoicing_port._override, NotBuiltInvoicingPort()
    try:
        with pytest.raises(PayrollError) as exc:
            services.approve_deferred_sale(c, user=w.mp)
    finally:
        invoicing_port._override = prev
    assert exc.value.code == "invoicing_not_built"


def test_at31_at41_no_increase_fields_consent_and_undertaking(w):
    s1 = saudi(w)
    with pytest.raises(PayrollError) as exc:
        services.create_deferred_sale(s1, user=w.p, asset_description_ar="سيارة", cost="100.00", profit_amount="1.00",
                                      term_months=2, first_period="2027-03", late_fee="5.00")
    assert exc.value.code == "sharia_no_charge"
    with pytest.raises(PayrollError):
        services.create_deferred_sale(s1, user=w.p, asset_description_ar="سيارة", cost="100.00", profit_amount="1.00",
                                      term_months=2, first_period="2027-03", final_dues_undertaking=True)
    c = sale(w, s1, consent="")
    with pytest.raises(PayrollError) as exc:
        services.approve_deferred_sale(c, user=w.mp)
    assert exc.value.code == "consent_required"
    c.profit_amount = D("9999")
    with pytest.raises(PayrollError):
        c.save()  # never edited outside the services


def test_hr_port_reports_loan_and_bai_ajel_balances(w):
    from payroll.hr_port import PayrollHrPort
    s1 = saudi(w)
    services.approve_loan(services.submit_loan(loan(w, s1), user=w.p), user=w.mp)
    services.approve_deferred_sale(sale(w, s1, final_dues_undertaking=True), user=w.mp)
    b = PayrollHrPort().outstanding_balances(s1, date(2027, 3, 31))
    assert b.loans == D("3000.00") and b.bai_ajel == D("38640.00") and b.bai_ajel_undertaking
