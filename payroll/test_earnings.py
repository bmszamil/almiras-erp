"""Earnings, inputs and deductions (R9, R14 to R18, R44, R45, R52, R54; AT-8, AT-10 to AT-12, AT-27, AT-28,
AT-35, AT-37, AT-38)."""
from datetime import date, datetime

import pytest
from django.core.exceptions import PermissionDenied

from core.access.testing import make_user, member
from hr import services as hr
from payroll import services
from payroll.conftest import D, amount, approved, calculated, expat, saudi, slip
from payroll.models import LoanInstallment, PayrollError


def overtime(w, emp, hours, on, by=None, **kw):
    return services.create_input(emp, user=by or w.p, kind="overtime_hours", period=f"{on:%Y-%m}", quantity=hours,
                                 overtime_date=on, reason_ar="إنجاز عاجل", **kw)


def test_at10_overtime_pay_is_hourly_wage_plus_half_hourly_basic(w):
    s1 = saudi(w)
    services.approve_overtime(overtime(w, s1, "10", date(2027, 3, 10)), user=w.mp)
    run = calculated(w)
    assert amount(run, s1, "overtime") == D("770.83")  # 10 x (13,500/30/8 + 0.5 x 10,000/30/8)


def test_at28_overtime_eligibility_reason_and_route(w):
    m1 = saudi(w, contract_kwargs={"grade": w.mgmt})
    with pytest.raises(PayrollError):
        overtime(w, m1, "2", date(2027, 3, 10))  # grade not eligible (P11)
    s1 = saudi(w)
    with pytest.raises(PayrollError):
        services.create_input(s1, user=w.p, kind="overtime_hours", period="2027-03", quantity="2",
                              overtime_date=date(2027, 3, 10), reason_ar="")
    inp = overtime(w, s1, "2", date(2027, 3, 10))
    with pytest.raises(PermissionDenied):
        services.approve_overtime(inp, user=w.c)  # not the route's approver
    with pytest.raises(PermissionDenied):
        services.approve_overtime(inp, user=w.p)  # its creator
    assert services.approve_overtime(inp, user=w.mp).status == "approved"  # ALM route: managing_partner


def test_at28_sub_overtime_by_the_department_head_only(w):
    dh_user, other_head_user = make_user("dh"), make_user("oh")
    head = saudi(w, w.sub, linked_user=dh_user)
    member(dh_user, w.sub, "department_head")
    member(other_head_user, w.sub, "department_head")
    hr.set_department_head(w.sub_dept, head, user=w.hs)
    s2 = saudi(w, w.sub)
    inp = services.create_input(s2, user=w.ps, kind="overtime_hours", period="2027-03", quantity="2",
                                overtime_date=date(2027, 3, 10), reason_ar="إنجاز")
    with pytest.raises(PayrollError) as exc:  # a head of another department
        services.approve_overtime(inp, user=other_head_user)
    assert exc.value.code == "not_department_head"
    assert services.approve_overtime(inp, user=dh_user).status == "approved"
    own = services.create_input(head, user=w.ps, kind="overtime_hours", period="2027-03", quantity="2",
                                overtime_date=date(2027, 3, 11), reason_ar="إنجاز")
    with pytest.raises(PermissionDenied):
        services.approve_overtime(own, user=dh_user)  # own pay; goes to the MP (hr OQ-30 default)


def test_r45_unapproved_overtime_is_left_out(w):
    s1 = saudi(w)
    overtime(w, s1, "5", date(2027, 3, 10))
    run = calculated(w)
    assert amount(run, s1, "overtime") == 0


def test_at35_overtime_caps(w):
    s1 = saudi(w)
    first = services.create_input(s1, user=w.p, kind="overtime_hours", period="2027-01", quantity="715",
                                  overtime_date=date(2027, 1, 15), reason_ar="مشروع", worker_consent_ref="")
    services.approve_overtime(first, user=w.mp)
    with pytest.raises(PayrollError) as exc:
        overtime(w, s1, "10", date(2027, 3, 10))
    assert "720" in exc.value.messages[0]
    assert overtime(w, s1, "10", date(2027, 3, 10), worker_consent_ref="CONSENT-1")
    s9 = saudi(w)
    with pytest.raises(PayrollError):
        overtime(w, s9, "11", date(2027, 3, 14), art106_exceptional=True)
    for day in range(14, 20):  # Sunday 14 .. Friday 19 March 2027: 6 x 10 hours
        overtime(w, s9, "10", date(2027, 3, day), art106_exceptional=True, reference=f"d{day}")
    with pytest.raises(PayrollError):
        overtime(w, s9, "1", date(2027, 3, 20), art106_exceptional=True)


def test_at11_at38_sick_leave_tiers_and_holiday_at_full_pay(w):
    n1 = expat(w)
    req = hr.request_leave(n1, "SICK", user=w.h, start_date=date(2027, 2, 1), end_date=date(2027, 3, 7),
                           document_ref="MED-1")
    hr.approve_leave(req, user=w.h2)
    run = calculated(w)
    assert amount(run, n1, "sick_deduction") == D("333.33")  # 5 x 8,000/30 x 25%
    n2 = expat(w)
    req = hr.request_leave(n2, "SICK", user=w.h, start_date=date(2027, 2, 1), end_date=date(2027, 3, 12),
                           document_ref="MED-2")
    hr.approve_leave(req, user=w.h2)
    run = services.calculate_run(run, user=w.p)
    # 3 to 8 March at 75% (6 days); 9 to 12 March are Eid al-Fitr: 100% (Regs Art. 24).
    assert amount(run, n2, "sick_deduction") == D("400.00")


def test_at12_loan_installment_capped_at_10_percent_and_replanned(w):
    n1 = expat(w)
    loan = services.create_loan(n1, user=w.p, kind="loan", principal="2000.00", disbursement_date=date(2027, 2, 1),
                                purpose_ar="قرض", installments=[("2027-03", "1000.00"), ("2027-04", "1000.00")])
    services.approve_loan(services.submit_loan(loan, user=w.p), user=w.mp)
    run = approved(w)
    assert amount(run, n1, "loan_installment") == D("800.00")
    assert any("reduced to 800.00" in x for x in slip(run, n1).warnings)
    carried = LoanInstallment.objects.get(loan=loan, carried_from__isnull=False)
    assert carried.amount == D("200.00") and carried.period == "2027-05"


def test_at12_negative_net_is_refused(w):
    n1 = expat(w)
    inp = services.create_input(n1, user=w.p, kind="one_off_deduction", period="2027-03", amount="9000.00",
                                deduction_basis="statutory_other", reason_ar="استقطاع")
    services.approve_input(inp, user=w.p2)
    run = calculated(w)
    assert slip(run, n1).refused and "Art. 93" in slip(run, n1).refusal


def test_at37_deduction_bases_and_limits(w):
    n1 = expat(w)
    base = dict(kind="one_off_deduction", period="2027-03", reason_ar="استقطاع")
    with pytest.raises(PayrollError):
        services.create_input(n1, user=w.p, amount="500.00", deduction_basis="written_consent", **base)
    assert services.create_input(n1, user=w.p, amount="500.00", deduction_basis="written_consent",
                                 consent_ref="SIGNED-1", **base)
    with pytest.raises(PayrollError) as exc:
        services.create_input(n1, user=w.p, amount="1500.00", deduction_basis="damage_art91", reference="d", **base)
    assert "1333.33" in exc.value.messages[0]
    with pytest.raises(PayrollError) as exc:
        services.create_input(n1, user=w.p, amount="2500.00", deduction_basis="court_order", reference="c", **base)
    assert "2000.00" in exc.value.messages[0]
    with pytest.raises(PayrollError):
        services.create_input(n1, user=w.p, kind="disciplinary_fine", period="2027-03", amount="100.00",
                              reason_ar="جزاء")
    with pytest.raises(PayrollError) as exc:  # Art. 109(2): no cash instead of leave during service
        services.create_input(n1, user=w.p, kind="leave_encashment", period="2027-03", amount="100.00",
                              reason_ar="بدل")
    assert "109" in exc.value.messages[0]


def test_at37_fixed_deductions_above_half_the_wage_refuse_the_payslip(w):
    n1 = expat(w)
    inp = services.create_input(n1, user=w.p, kind="one_off_deduction", period="2027-03", amount="4500.00",
                                deduction_basis="written_consent", consent_ref="SIGNED-2", reason_ar="استقطاع")
    services.approve_input(inp, user=w.p2)
    run = calculated(w)
    assert slip(run, n1).refused and "4000.00" in slip(run, n1).refusal


def test_r26_charges_linked_to_loans_are_refused(w):
    n1 = expat(w)
    with pytest.raises(PayrollError) as exc:
        services.create_input(n1, user=w.p, kind="one_off_deduction", period="2027-03", amount="50.00",
                              reference="admin fee", reason_ar="رسوم", deduction_basis="written_consent",
                              consent_ref="x")
    assert exc.value.code == "sharia_no_charge"


def test_at27_cut_off_in_country_27th_out_of_country_6th(w, pclock):
    s1 = saudi(w)
    on27 = services.create_input(s1, user=w.p, kind="bonus", period="2027-03", amount="100.00", reason_ar="أ",
                                 reference="a")
    on28 = services.create_input(s1, user=w.p, kind="bonus", period="2027-03", amount="200.00", reason_ar="ب",
                                 reference="b")
    pclock.set(datetime(2027, 3, 27, 18, 0))
    assert services.approve_input(on27, user=w.p2).payroll_period == "2027-03"
    pclock.set(datetime(2027, 3, 28, 9, 0))
    assert services.approve_input(on28, user=w.p2).payroll_period == "2027-04"
    s = services._settings_of(w.alm)
    from payroll.models import PayrollGroup
    assert services._payroll_period_for("2027-03", date(2027, 4, 6), PayrollGroup.OUT_OF_COUNTRY, s) == "2027-03"
    assert services._payroll_period_for("2027-03", date(2027, 4, 7), PayrollGroup.OUT_OF_COUNTRY, s) == "2027-04"
    run = calculated(w)
    assert amount(run, s1, "bonus") == D("100.00") and run.entry_date == date(2027, 3, 31)
    services.create_run(w.alm, user=w.p, period="2027-03", payroll_group="out_of_country")
    with pytest.raises(PayrollError):
        services.create_run(w.alm, user=w.p, period="2027-03", payroll_group="out_of_country")


def test_at8_retro_difference_paid_in_the_next_run(w, clock):
    s1 = saudi(w)
    approved(w)
    from payroll.conftest import contract
    clock.set(date(2027, 4, 10))
    contract(w, s1, {"BASIC": "11000.00", "HOUSING": "2500.00", "TRANSPORT": "1000.00"}, effective_date=date(2027, 3, 15), start_date=date(2027, 1, 1), qiwa_contract_ref="QIWA-AMEND-1")
    run = calculated(w, "2027-04")
    # 1,000.00 x 16/30: 15 to 30 March on the 30-day basis (day 31 carries no pay, so a month is never more than 30
    # days; AT-8's 17/30 would pay 31/30 of March across the two versions; noted in the spec as built).
    assert amount(run, s1, "retro") == D("533.33")
    assert amount(run, s1, "earning", component__code="BASIC") == D("11000.00")
    # The GOSI wage of March is the wage registered at the start of March (R20(g) seed rule): no retro share.
    assert not slip(run, s1).lines.filter(line_type="employee_gosi", origin_period="2027-03").exists()


def test_r58_retro_wage_from_the_first_of_a_past_month_carries_its_gosi_shares(w, clock):
    s1 = saudi(w)
    approved(w)
    from payroll.conftest import contract
    clock.set(date(2027, 4, 10))
    contract(w, s1, {"BASIC": "11000.00", "HOUSING": "2500.00", "TRANSPORT": "1000.00"},
             effective_date=date(2027, 3, 1), start_date=date(2027, 1, 1), qiwa_contract_ref="QIWA-AMEND-3")
    run = calculated(w, "2027-04")
    assert amount(run, s1, "retro") == D("1000.00")
    retro = slip(run, s1).lines.filter(origin_period="2027-03")
    assert sum(l.amount for l in retro.filter(line_type="employee_gosi")) == D("97.50")  # 9% + 0.75% of 1,000.00
    assert sum(l.amount for l in retro.filter(line_type="employer_gosi")) == D("117.50")  # 9% + 0.75% + 2%


def test_r14_retro_decrease_is_only_warned(w, clock):
    s1 = saudi(w)
    approved(w)
    from payroll.conftest import contract
    clock.set(date(2027, 4, 10))
    contract(w, s1, {"BASIC": "9000.00", "HOUSING": "2500.00", "TRANSPORT": "1000.00"}, effective_date=date(2027, 3, 1), start_date=date(2027, 1, 1), qiwa_contract_ref="QIWA-AMEND-2")
    run = calculated(w, "2027-04")
    assert amount(run, s1, "retro") == 0 and any("Art. 92" in x for x in slip(run, s1).warnings)
