"""Run lifecycle, entity separation, approval and posting (R1 to R13, R36, R39, R50; AT-1 to AT-9, AT-23, AT-24,
AT-33, AT-36, AT-64)."""
from datetime import date

import pytest
from django.core.exceptions import PermissionDenied
from django.http import Http404

from core import access
from core.access.models import GrantUse
from core.access.testing import member
from hr import services as hr
from hr.conftest import make_entity
from ledger import services as ledger
from ledger.models import FiscalPeriod, JournalEntry
from ledger.services_setup import create_monthly_periods
from payroll import services
from payroll.conftest import D, amount, approved, calculated, expat, run_for, saudi, slip, submitted
from payroll.models import PayrollError, PayrollRun, Payslip


def test_at1_runs_are_entity_scoped_and_another_entitys_run_is_404(w):
    saudi(w)
    s2 = saudi(w, w.sub)
    alm = approved(w)
    sub = services.calculate_run(services.create_run(w.sub, user=w.ps, period="2027-03"), user=w.ps)
    assert list(services.runs(w.p)) == [alm]
    with pytest.raises(Http404):
        services.get_run(w.p, sub.pk)
    assert not alm.payslips.filter(employee=s2).exists()  # R1: only the entity's employees
    assert sub.payslips.filter(employee=s2).exists()
    with pytest.raises(Http404):
        services.get_payslip(w.p, sub.payslips.get().pk)


def test_at1_off_cycle_run_refuses_another_entitys_employee(w):
    s2 = saudi(w, w.sub)
    with pytest.raises(PayrollError) as exc:
        services.create_run(w.alm, user=w.p, period="2027-03", kind="off_cycle", reason_ar="سبب", employees=[s2])
    assert exc.value.code == "entity_mismatch"


def test_at2_new_entity_runs_as_data_egypt_and_unactivated_refused(w):
    new = make_entity("NEW", "full", w.alm)
    create_monthly_periods(new, 2027)
    from core.access.testing import make_user
    pn, cn, hn = make_user("pn"), make_user("cn"), make_user("hn")
    member(pn, new, "payroll_officer")
    member(cn, new, "approver")
    member(hn, new, "hr_officer")
    services.setup_entity(new)
    services.configure_settings(new, user=cn, enabled=True)
    dept = hr.create_department(new, user=hn, code="ADM", name_en="Admin", name_ar="الإدارة")
    grade = hr.create_grade(new, user=hn, code="G1", name_en="G1", name_ar="د1", overtime_eligible=True)
    from hr.conftest import contract_fields
    emp = hr.create_employee(new, user=hn, full_name_en="New Person", full_name_ar="شخص جديد", nationality="IN",
                             id_type="iqama", id_number="2999999901", date_of_birth=date(1990, 1, 1), gender="male",
                             hire_date=date(2027, 1, 1), employment_basis="ksa_employee")
    c = hr.create_contract(emp, user=hn, components={"BASIC": "4000.00"},
                           **contract_fields(w, emp, department=dept, grade=grade))
    hr.approve_contract(hr.submit_contract(c, user=hn), user=cn)
    run = services.calculate_run(services.create_run(new, user=pn, period="2027-03"), user=pn)
    assert run.payslips.get().gross == D("4000.00")  # no code change for a new entity (R3)
    with pytest.raises(PayrollError) as exc:
        services.create_run(w.egy, user=w.pe, period="2027-03")
    assert exc.value.code == "jurisdiction_not_configured" and " / " in exc.value.messages[0]
    uae = make_entity("UAE", "full", w.alm, country="AE", currency="AED")
    pu, cu = make_user("pu"), make_user("cu")
    member(pu, uae, "payroll_officer")
    member(cu, uae, "approver")
    services.configure_settings(uae, user=cu, enabled=True)
    with pytest.raises(PayrollError) as exc:  # AT-61: China / UAE not activated (hr R68)
        services.create_run(uae, user=pu, period="2027-03")
    assert exc.value.code == "jurisdiction_not_activated"


def test_r3_disabled_payroll_refuses_runs(w):
    services.configure_settings(w.sub, user=w.c2, enabled=False)
    with pytest.raises(PayrollError) as exc:
        services.create_run(w.sub, user=w.ps, period="2027-03")
    assert exc.value.code == "payroll_disabled"


def test_at3_float_amounts_are_refused(w):
    n1 = expat(w)
    with pytest.raises(PayrollError) as exc:
        services.create_input(n1, user=w.p, kind="one_off_earning", period="2027-03", amount=100.5, reason_ar="مكافأة")
    assert exc.value.code == "float_rejected"


def test_at4_one_regular_run_off_cycle_reason_and_no_direct_status(w):
    saudi(w)
    run = approved(w)
    with pytest.raises(PayrollError) as exc:
        run_for(w)
    assert exc.value.code == "run_exists"
    with pytest.raises(PayrollError):
        run_for(w, kind="off_cycle", reason_ar="")
    draft = run_for(w, "2027-04")
    draft.status = "approved"
    with pytest.raises(PayrollError):
        draft.save()  # service-only writes
    draft._service_write = True
    with pytest.raises(PayrollError) as exc:  # draft -> approved is not a move (R7)
        draft.save()
    assert exc.value.code == "run_status"
    with pytest.raises(PayrollError):
        PayrollRun.objects.filter(pk=draft.pk).update(status="approved")


def test_at5_source_changed_after_calculation_refuses_approval_grant_unused(w):
    s1 = saudi(w)
    run = submitted(w)
    req = hr.request_leave(s1, "UNPAID", user=w.h, start_date=date(2027, 3, 15), end_date=date(2027, 3, 16))
    hr.approve_leave(req, user=w.h2)
    before = GrantUse.objects.count()
    with pytest.raises(PayrollError) as exc:
        services.approve_run(run, user=w.h)
    assert exc.value.code == "recalculate" and "أعد احتساب" in exc.value.messages[0]
    assert GrantUse.objects.count() == before and PayrollRun.objects.get(pk=run.pk).status == "submitted"
    run = services.return_run(run, user=w.h, reason_ar="إعادة احتساب")
    run = services.submit_run(services.calculate_run(run, user=w.p), user=w.p)
    run = services.approve_run(run, user=w.h)
    assert run.status == "approved" and amount(run, s1, "unpaid_deduction") == D("900.00")


def test_at6_joiner_prorated_and_unpaid_days_deducted(w):
    n1 = expat(w, hire_date=date(2027, 3, 11))
    s1 = saudi(w)
    req = hr.request_leave(s1, "UNPAID", user=w.h, start_date=date(2027, 3, 15), end_date=date(2027, 3, 16))
    hr.approve_leave(req, user=w.h2)
    run = calculated(w)
    ps = slip(run, n1)
    assert ps.paid_days == 20
    assert amount(run, n1, "earning") == D("5333.33")  # 4,000.00 + 1,000.00 + 333.33 (20/30 each)
    assert amount(run, s1, "unpaid_deduction") == D("900.00")  # 2 x 13,500.00 / 30


def test_at36_calendar_day_basis_needs_its_reference(w):
    with pytest.raises(PayrollError) as exc:
        services.configure_settings(w.alm, user=w.c, day_basis="calendar")
    assert "Art. 2" in exc.value.messages[0]
    services.configure_settings(w.alm, user=w.c, day_basis="calendar", day_basis_ref="Work regulations Art. 12")
    n1 = expat(w, hire_date=date(2027, 3, 11))
    run = calculated(w)
    assert slip(run, n1).paid_days == 21 and amount(run, n1, "earning", component__code="BASIC") == D("4064.52")


def test_at7_approval_by_hr_officer_posts_the_entry_with_number_and_no_names(w):
    s1 = saudi(w, full_name_en="Unique Testname")
    run = submitted(w)
    with pytest.raises(PermissionDenied):
        services.approve_run(run, user=w.p)  # preparer (A)
    with pytest.raises(PermissionDenied):
        services.approve_run(run, user=w.c)  # the CFO does not hold payroll.approve_run (P14)
    run = services.approve_run(run, user=w.h)
    entry = run.journal_entry
    assert entry.document_type == "payroll.payrollrun" and entry.document_id == run.pk
    assert entry.date == date(2027, 3, 31) and run.number == "ALM-PAY-2027-000001"
    for line in entry.lines.all():  # R36: no names, IDs or individual pay
        assert "Unique" not in line.description and s1.employee_code not in line.description


def test_at7_closed_period_refuses_and_close_refused_while_submitted(w):
    saudi(w)
    run = submitted(w)
    period = FiscalPeriod.objects.get(entity=w.alm, name="2027-03")
    with pytest.raises(Exception) as exc:
        ledger.close_period(period, user=w.c)
    assert "payroll run" in str(exc.value)
    period.status = FiscalPeriod.Status.CLOSED
    period.save()
    with pytest.raises(Exception):
        services.approve_run(run, user=w.h)
    assert PayrollRun.objects.get(pk=run.pk).status == "submitted" and PayrollRun.objects.get(pk=run.pk).number == ""


def test_at9_reversal_mirrors_and_restores_installments(w):
    n1 = expat(w)
    loan = services.create_loan(n1, user=w.p, kind="loan", principal="1000.00", disbursement_date=date(2027, 2, 1),
                                purpose_ar="قرض", installments=[("2027-03", "500.00"), ("2027-04", "500.00")])
    services.approve_loan(services.submit_loan(loan, user=w.p), user=w.mp)
    run = approved(w)
    assert loan.installments.get(period="2027-03").status == "deducted"
    with pytest.raises(PermissionDenied):
        services.reverse_run(run, user=w.p, reason="error")
    run = services.reverse_run(run, user=w.c, reason="Wrong inputs")
    assert run.status == "reversed" and run.reversal_entry.reversal_of_id == run.journal_entry_id
    assert loan.installments.get(period="2027-03").status == "planned"
    from payroll.models import EosbProvisionEntry
    assert sum(e.amount for e in EosbProvisionEntry.objects.filter(employee=n1)) == 0
    again = approved(w)  # a new regular run for the period after the reversal (R13)
    assert again.status == "approved"


def test_at9_reversal_refused_after_an_approved_payment(w):
    saudi(w)
    run = approved(w)
    p = services.record_payment(w.alm, user=w.p, kind="net_salaries", run=run, payment_date=date(2027, 3, 31),
                                amount=run.payslips.get().net)
    services.approve_payment(services.submit_payment(p, user=w.p), user=w.c)
    with pytest.raises(PayrollError) as exc:
        services.reverse_run(PayrollRun.objects.get(pk=run.pk), user=w.c, reason="late")
    assert exc.value.code in ("run_paid", "run_status")


def test_r13_registered_run_entry_is_not_reversed_with_a_generic_grant(w):
    saudi(w)
    run = approved(w)
    g = access.require(w.a, w.alm, "ledger.reverse_entry", document=run.journal_entry)
    with pytest.raises(PermissionDenied):
        ledger.reverse_entry(run.journal_entry, g)


def test_at33_at64_only_the_hr_officer_without_own_inputs_approves(w):
    s1 = saudi(w)
    inp = services.create_input(s1, user=w.h2, kind="one_off_earning", period="2027-03", amount="100.00",
                                reason_ar="مكافأة")
    services.approve_input(inp, user=w.p2)
    run = submitted(w)
    with pytest.raises(PermissionDenied):
        services.approve_run(run, user=w.h2)  # entered an input of the run (R50 default)
    with pytest.raises(PermissionDenied) as exc:
        services.approve_run(run, user=w.mp)  # P20(a): no MP approval of contracted payroll
    assert exc.value.code == "not_in_matrix"
    assert services.approve_run(run, user=w.h).status == "approved"


def test_at24_approver_with_own_inputs_is_refused(w):
    h_emp = saudi(w, linked_user=w.h)
    inp = services.create_input(h_emp, user=w.p, kind="one_off_earning", period="2027-03", amount="100.00",
                                reason_ar="مكافأة")
    services.approve_input(inp, user=w.p2)
    run = submitted(w)
    with pytest.raises(PermissionDenied):
        services.approve_run(run, user=w.h)
    assert services.approve_run(run, user=w.h2).status == "approved"


def test_at23_agents_and_system_users_hold_no_payroll_right(w):
    s1 = saudi(w)
    with pytest.raises(PermissionDenied):
        services.create_input(s1, user=w.agent, kind="bonus", period="2027-03", amount="10.00", reason_ar="x")
    system = access.system_user("billing")
    with pytest.raises(PermissionDenied):
        services.create_run(w.alm, user=system, period="2027-03")


def test_r56_run_not_submitted_while_payslips_are_refused(w):
    s7 = saudi(w, cohort="")
    run = calculated(w)
    assert slip(run, s7).refused and "cohort" in slip(run, s7).refusal
    with pytest.raises(PayrollError) as exc:
        services.submit_run(run, user=w.p)
    assert exc.value.code == "payslips_refused"


def test_r10_terminated_in_period_is_left_to_the_final_settlement(w):
    n1 = expat(w)
    t = hr.record_termination(n1, user=w.h, kind="employer_termination", last_working_day=date(2027, 3, 20),
                              notice_given_on=date(2027, 1, 19), reason_ar="إنهاء")
    hr.approve_termination(hr.submit_termination(t, user=w.h), user=w.c)
    run = calculated(w)
    assert not Payslip.objects.filter(run=run, employee=n1).exists()
    assert any("final settlement" in x for x in run.warnings)
