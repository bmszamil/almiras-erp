"""hr R27, R35 to R41, R52 with the compliance corrections of 2026-10-17 (Art. 85 bands with 5 years inclusive at
1/3; resignation vs Art. 75 notice; mutual agreement consent; closure, activity ended, bankruptcy; Art. 79 bis
resignation dates and withdrawal) (AT-19 to AT-22, AT-27)."""
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction

import pytest

from core import access
from hr import services
from hr.conftest import employee, hired
from hr.models import (Assignment, AssignmentEvent, EosbCalculation, FinalSettlementInput, HrError, LeaveLedger,
                       LeaveRequest)

D = Decimal
AS_OF = date(2027, 5, 31)


def _record(w, emp, kind="employer_termination", **fields):
    fields.setdefault("reason_ar", "سبب الإنهاء")
    if kind != "resignation":
        fields.setdefault("last_working_day", date(2027, 6, 30))
    if kind in ("employer_termination", "employee_notice_art75"):
        fields.setdefault("notice_given_on", fields["last_working_day"] - timedelta(days=60))
    return services.record_termination(emp, user=w.h, kind=kind, **fields)


def _approve(w, emp, kind="employer_termination", approver=None, **fields):
    t = services.submit_termination(_record(w, emp, kind, **fields), user=w.h)
    return services.approve_termination(t, user=approver or w.c)


def _years_ago(days):
    return AS_OF - timedelta(days=days - 1)


# --- R36 / AT-19 ----------------------------------------------------------------------------------------------

def test_at19_reason_and_approval_segregation(w):
    emp = hired(w, hire_date=date(2020, 1, 1))
    with pytest.raises(HrError):
        _record(w, emp, "employee_notice_art75", reason_ar="")
    t = services.submit_termination(_record(w, emp, "employee_notice_art75"), user=w.h)
    with pytest.raises(access.AccessDenied):
        services.approve_termination(t, user=w.h)
    assert services.approve_termination(t, user=w.c).status == "approved"
    own = hired(w, hire_date=date(2020, 1, 1), linked_user=w.c, contract_kwargs={"approver": w.mp})
    t2 = services.submit_termination(_record(w, own, "employee_notice_art75"), user=w.h)
    with pytest.raises(access.AccessDenied) as exc:
        services.approve_termination(t2, user=w.c)
    assert exc.value.code == "self_approval"


def test_r37_notice_shortfall_is_a_warning(w):
    emp = hired(w, hire_date=date(2020, 1, 1))
    t = _record(w, emp, notice_given_on=date(2027, 6, 10))
    assert t.notice_shortfall_days == 40 and "Art. 75" in t.warnings[0]


def test_r37_kind_must_fit_the_contract(w):
    saudi_open = hired(w)
    with pytest.raises(HrError):
        _record(w, saudi_open, "resignation", submitted_on=date(2027, 5, 25))
    fixed = hired(w, saudi=False)
    with pytest.raises(HrError):
        _record(w, fixed, "employee_notice_art75")
    with pytest.raises(HrError):
        _record(w, fixed, "mutual_agreement")
    assert _record(w, fixed, "mutual_agreement", written_consent_ref="CONSENT-1").pk
    with pytest.raises(HrError):
        _record(w, hired(w), "end_of_remuneration")


def test_r37_warnings_art82_sick_leave(w):
    emp = hired(w, hire_date=date(2020, 1, 1))
    req = services.request_leave(emp, "SICK", user=w.h, start_date=date(2027, 6, 25), end_date=date(2027, 7, 5),
                                 document_ref="MED-1")
    services.approve_leave(req, user=w.h2)
    t = _record(w, emp)
    assert any("Art. 82" in x for x in t.warnings)


def test_art54_probation_termination_only_during_probation(w):
    emp = hired(w, hire_date=date(2027, 4, 1))  # probation 90 days
    with pytest.raises(HrError):
        _record(w, emp, "probation_termination", last_working_day=date(2027, 8, 1))
    t = _approve(w, emp, "probation_termination", last_working_day=date(2027, 6, 15))
    assert EosbCalculation.objects.get(termination=t).award == 0


# --- R39 / AT-21: EOSB figures with the unverified seed (Art. 84, 85 as corrected) ------------------------

def _hired_for(years, extra_days=0):
    """The hire date that gives ``years`` completed Gregorian anniversaries plus ``extra_days`` on AS_OF."""
    last_anniversary = AS_OF + timedelta(days=1 - extra_days)
    return last_anniversary.replace(year=last_anniversary.year - years)


@pytest.mark.parametrize("years, extra, kind, expected", [
    (4, 0, "resignation", "5333.33"),
    (4, 0, "employer_termination", "16000.00"),
    (12, 0, "resignation", "76000.00"),       # 5 × 4,000 + 7 × 8,000
    (1, 0, "resignation", "0.00"),
    (4, 0, "art80_dismissal", "0.00"),
    (7, 219, "end_of_fixed_term", "40800.00"),  # 7.6 years: 5 × 4,000 + 2.6 × 8,000
    # Finding 5: "exactly 5 years" is the fifth anniversary (2022-06-01 to 2027-05-31, a leap day inside): 1/3.
    (5, 0, "resignation", "6666.67"),
    (5, 1, "resignation", "13347.95"),        # 5 years + 1 day: 2/3 × (20,000.00 + 8,000.00 × 1/365)
    (10, 0, "resignation", "60000.00"),       # 10 years: full award
    (2, 0, "resignation", "2666.67"),         # 2 years: 1/3
    (4, 0, "employee_notice_art75", "5333.33"),
    (4, 0, "mutual_agreement", "16000.00"),
    (4, 0, "closure", "16000.00"),
    (4, 0, "activity_ended", "16000.00"),
    (4, 0, "bankruptcy", "16000.00"),
    (4, 0, "death", "16000.00"),
    (4, 0, "probation_termination", "0.00"),
])
def test_at21_eosb_figures(w, years, extra, kind, expected):
    hire = _hired_for(years, extra)
    emp = hired(w, hire_date=hire)  # BASIC 6,000 + HOUSING 1,500 + TRANSPORT 500 = 8,000
    r = services.calculate_eosb(emp, AS_OF, kind, user=w.p)
    assert r.wage_basis == D("8000.00") and r.service_days == (AS_OF - hire).days + 1
    assert r.service_years == years + Fraction(extra, 365)
    assert r.award == D(expected), (r.full_award, r.fraction)
    assert not EosbCalculation.objects.exists()  # pure: nothing written


def test_at21_exact_fraction_value(w):
    emp = hired(w, hire_date=_hired_for(5, 1))
    r = services.calculate_eosb(emp, AS_OF, "resignation", user=w.h)
    assert r.fraction == Fraction(2, 3) and r.service_years == Fraction(1826, 365)


@pytest.mark.parametrize("hire, as_of, years, fraction", [
    # Review probe P1: 2022-06-01 to 2027-05-31 is 1,826 days (29 Feb 2024 inside) but exactly 5 years.
    (date(2022, 6, 1), date(2027, 5, 31), Fraction(5), "1/3"),
    # P1b: one day short of the second anniversary: under 2 years, no award.
    (date(2025, 6, 2), date(2027, 5, 31), 1 + Fraction(364, 365), "0"),
    # P1c: two days short of 10 years: still the 2/3 band.
    (date(2017, 6, 3), date(2027, 5, 31), 9 + Fraction(363, 365), "2/3"),
    (date(2017, 6, 1), date(2027, 5, 31), Fraction(10), "1"),
])
def test_r39_gregorian_thresholds_are_completed_anniversaries(w, hire, as_of, years, fraction):
    w.clock.set(date(2027, 6, 1))
    emp = hired(w, hire_date=hire)
    r = services.calculate_eosb(emp, as_of, "employee_notice_art75", user=w.h)
    assert r.service_years == years and str(r.fraction) == fraction


def test_r39_unpaid_days_beyond_twenty_are_excluded(w):
    emp = hired(w, hire_date=_years_ago(4 * 365))  # 1,460 days to AS_OF
    req = services.request_leave(emp, "UNPAID", user=w.h, start_date=date(2027, 3, 1), end_date=date(2027, 3, 30),
                                 reason_ar="ظرف")
    services.approve_leave(req, user=w.h2)
    r = services.calculate_eosb(emp, AS_OF, "employer_termination", user=w.h)
    assert r.excluded_days == 10 and r.service_days == 4 * 365


def test_at21_snapshot_immutable_recalc_supersedes_then_refused(w):
    emp = hired(w, hire_date=_hired_for(4))
    t = _approve(w, emp, last_working_day=AS_OF, notice_given_on=AS_OF - timedelta(days=60))
    snap = EosbCalculation.objects.get(termination=t)
    assert snap.award == D("16000.00") and snap.fraction_text == "1" and snap.parameter_ids
    snap.award = D("1")
    with pytest.raises(HrError):
        snap.save()
    with pytest.raises(HrError):
        EosbCalculation.objects.filter(pk=snap.pk).update(award=D("1"))
    new = services.recalculate_eosb(t, user=w.h)
    assert new.supersedes == snap and new.award == D("16000.00")
    assert FinalSettlementInput.objects.filter(termination=t, status="draft").count() == 1
    w.payroll.approved.add(t.pk)
    with pytest.raises(HrError) as exc:
        services.recalculate_eosb(t, user=w.h)
    assert exc.value.code == "settlement_approved"


# --- R27, R35, R38 / AT-20 ----------------------------------------------------------------------------------

def _seconded_with_leave(w, opening="10"):
    """Hired 2025-01-01, approved opening balance as of 2026-12-31, no accrual written since (the job has not run)."""
    emp = employee(w, hire_date=date(2025, 1, 1))
    adj = services.record_opening_leave_balance(emp, "ANNUAL", D(opening), date(2026, 12, 31), user=w.h)
    from hr.conftest import contract

    contract(w, emp)
    services.approve_leave_adjustment(adj, user=w.c)
    emp.refresh_from_db()
    a = services.create_assignment(emp, user=w.h, kind="secondment", receiving_entity=w.sub, role_title_en="Analyst",
                                   role_title_ar="محلل", start_date=date(2027, 2, 1))
    a = services.approve_assignment(services.submit_assignment(a, user=w.h), user=w.c)
    future = services.request_leave(emp, "ANNUAL", user=w.h, start_date=date(2027, 7, 5), end_date=date(2027, 7, 7))
    services.approve_leave(future, user=w.h2)
    return emp, a, future


def test_at20_termination_approval_does_everything_in_one_transaction(w):
    emp, a, future = _seconded_with_leave(w)
    assert services.leave_balance(emp, "ANNUAL", user=w.h) == 7
    t = _approve(w, emp, last_working_day=date(2027, 6, 30), notice_given_on=date(2027, 5, 1))
    emp.refresh_from_db()
    assert emp.status == "on_notice"
    assert emp.contracts.get(status="approved").ended_on == date(2027, 6, 30)
    a.refresh_from_db()
    assert a.status == "ended" and a.end_date == date(2027, 6, 30)
    assert AssignmentEvent.objects.get(assignment=a).end_date == date(2027, 6, 30)
    assert LeaveRequest.objects.get(pk=future.pk).status == "cancelled"
    # Finding 2: January to June 2027 (6 x 1.75) are accrued up to the last working day before the balance is taken.
    assert services.leave_balance(emp, "ANNUAL", user=w.h) == D("20.50")
    snap = EosbCalculation.objects.get(termination=t)
    si = FinalSettlementInput.objects.get(termination=t)
    assert si.eosb_calculation == snap and si.leave_balance_days == D("20.50")
    assert si.daily_wage == D("266.67") and si.leave_encashment == D("5466.67") and si.includes_statutory_lines
    assert w.payroll.drafts == [si.pk]
    services.refresh_statuses(today=date(2027, 7, 1))
    emp.refresh_from_db()
    assert emp.status == "terminated"


def test_r38_a_payroll_failure_rolls_everything_back(w):
    emp, a, future = _seconded_with_leave(w)

    def boom(si, user):
        raise RuntimeError("payroll down")

    w.payroll.create_draft = boom
    t = services.submit_termination(_record(w, emp), user=w.h)
    with pytest.raises(RuntimeError):
        services.approve_termination(t, user=w.c)
    emp.refresh_from_db()
    a.refresh_from_db()
    assert emp.status == "active" and a.status == "active" and not AssignmentEvent.objects.exists()
    assert LeaveRequest.objects.get(pk=future.pk).status == "approved" and not EosbCalculation.objects.exists()


def test_r27_not_started_assignment_is_cancelled_at_termination(w):
    emp = hired(w)
    a = services.create_assignment(emp, user=w.h, kind="secondment", receiving_entity=w.sub, role_title_en="A",
                                   role_title_ar="أ", start_date=date(2027, 8, 1))
    a = services.approve_assignment(services.submit_assignment(a, user=w.h), user=w.c)
    _approve(w, emp)
    a.refresh_from_db()
    assert a.status == "cancelled" and AssignmentEvent.objects.get(assignment=a).kind == "cancelled"


# --- R41 / AT-22 (as corrected by Art. 79 bis) -------------------------------------------------------------

def test_at22_withdraw_before_last_day_restores_then_refused_after_settlement(w):
    emp, a, _ = _seconded_with_leave(w)
    t = _approve(w, emp)
    t, warnings = services.withdraw_termination(t, user=w.c, reason_ar="تراجع")
    emp.refresh_from_db()
    a.refresh_from_db()
    assert t.status == "withdrawn" and emp.status == "active"
    assert emp.contracts.get(status="approved").ended_on is None
    assert a.status == "active" and a.end_date is None and warnings  # billing must reopen it (OQ-16)
    assert FinalSettlementInput.objects.get(termination=t).status == "discarded" and w.payroll.discarded == [t.pk]
    other = hired(w)
    t2 = _approve(w, other)
    w.payroll.approved.add(t2.pk)
    with pytest.raises(HrError):
        services.withdraw_termination(t2, user=w.mp, reason_ar="تراجع")
    with pytest.raises(access.AccessDenied):  # the termination's own creator cannot withdraw it
        services.withdraw_termination(t2, user=w.h, reason_ar="تراجع")


def test_art79bis_resignation_dates_are_derived(w):
    emp = hired(w, saudi=False)
    with pytest.raises(HrError) as exc:
        _record(w, emp, "resignation", submitted_on=date(2027, 5, 25), last_working_day=date(2027, 6, 30))
    assert exc.value.code == "lwd_derived"
    t = _record(w, emp, "resignation", submitted_on=date(2027, 5, 25))
    assert t.last_working_day == date(2027, 6, 24)  # deemed accepted on day 30
    t = services.approve_termination(services.submit_termination(t, user=w.h), user=w.c)
    assert t.accepted_on == date(2027, 6, 1) and t.last_working_day == date(2027, 6, 1)  # accepted today
    late = hired(w, saudi=False)
    t = _approve(w, late, "resignation", submitted_on=date(2027, 4, 1))
    assert t.accepted_on == date(2027, 5, 1) and t.last_working_day == date(2027, 5, 1)
    late.refresh_from_db()
    assert late.status == "terminated"


def test_art79bis_postponement(w):
    emp = hired(w, saudi=False)
    with pytest.raises(HrError):
        _record(w, emp, "resignation", submitted_on=date(2027, 5, 25), postponed_until=date(2027, 8, 1))
    with pytest.raises(HrError):
        _record(w, emp, "resignation", submitted_on=date(2027, 5, 25), postponed_until=date(2027, 8, 24),
                postponement_reason_ar="تسليم العمل")
    t = _approve(w, emp, "resignation", submitted_on=date(2027, 5, 25), postponed_until=date(2027, 8, 10),
                 postponement_reason_ar="تسليم العمل")
    assert t.last_working_day == date(2027, 8, 10)


def test_art79bis_withdrawal_within_seven_days_and_never_after_acceptance(w):
    emp = hired(w, saudi=False)
    t = _record(w, emp, "resignation", submitted_on=date(2027, 5, 28))
    t, _ = services.withdraw_termination(t, user=w.h, reason_ar="تراجع الموظف")
    assert t.status == "withdrawn"
    t = _record(w, emp, "resignation", submitted_on=date(2027, 5, 20))
    with pytest.raises(HrError) as exc:
        services.withdraw_termination(t, user=w.h, reason_ar="تراجع الموظف")
    assert exc.value.code == "withdrawal_late"
    accepted = hired(w, saudi=False)
    ta = _approve(w, accepted, "resignation", submitted_on=date(2027, 5, 30), postponed_until=date(2027, 8, 1),
                  postponement_reason_ar="تسليم")
    with pytest.raises(HrError) as exc:
        services.withdraw_termination(ta, user=w.mp, reason_ar="تراجع")
    assert exc.value.code == "withdrawal_after_acceptance"


# --- R52 / AT-27: remuneration staff --------------------------------------------------------------------

def test_at27_remuneration_no_eosb_end_of_remuneration_only(w):
    r1 = hired(w, basis="remuneration")
    with pytest.raises(HrError) as exc:
        services.calculate_eosb(r1, AS_OF, "employer_termination", user=w.h)
    assert exc.value.code == "remuneration_no_eosb"
    with pytest.raises(HrError):
        _record(w, r1, "resignation", submitted_on=date(2027, 5, 25), last_working_day=None)
    with pytest.raises(HrError):
        _record(w, r1, "employer_termination")
    t = _approve(w, r1, "end_of_remuneration")
    assert t.status == "approved" and not EosbCalculation.objects.filter(termination=t).exists()
    si = FinalSettlementInput.objects.get(termination=t)
    assert not si.includes_statutory_lines and si.eosb_award == 0 and si.leave_balance_days == 0
    assert si.currency_id == "USD"


def test_r51_local_eosb_refused_until_parameters(w):
    local = employee(w, w.egy, basis="local_entity_employee")
    with pytest.raises(HrError) as exc:
        services.calculate_eosb(local, AS_OF, "employer_termination", user=w.he)
    assert exc.value.code == "jurisdiction_not_configured" and "EG" in exc.value.messages[0]


# --- Finding 2 (review probe P2): the partial last month accrues before the settlement balance is taken -------

def test_r35_partial_last_month_accrues_up_to_the_last_working_day(w):
    emp = hired(w, hire_date=date(2027, 1, 1))
    for month in (1, 2, 3, 4, 5):
        services.accrue_leave(2027, month)
    w.clock.set(date(2027, 6, 1))
    t = _approve(w, emp, last_working_day=date(2027, 6, 20), notice_given_on=date(2027, 4, 21))
    row = LeaveLedger.objects.get(employee=emp, period="2027-06")
    assert row.date == date(2027, 6, 20) and row.days == D("1.17") and row.source_id == t.pk  # 21 x 20 / (12 x 30)
    si = FinalSettlementInput.objects.get(termination=t)
    assert si.leave_balance_days == D("9.92")  # 5 x 1.75 + 1.17
    services.accrue_leave(2027, 6)  # the month-end job does not accrue June again
    assert LeaveLedger.objects.filter(employee=emp, period="2027-06").count() == 1
    assert services.leave_balance(emp, "ANNUAL", user=w.h) == D("9.92")


def test_r35_a_month_the_job_accrued_in_full_is_corrected_to_the_last_working_day(w):
    emp = hired(w, hire_date=date(2027, 1, 1))
    for month in (1, 2, 3, 4, 5, 6):
        services.accrue_leave(2027, month)
    w.clock.set(date(2027, 7, 2))
    t = _approve(w, emp, last_working_day=date(2027, 6, 20), notice_given_on=date(2027, 4, 21))
    assert FinalSettlementInput.objects.get(termination=t).leave_balance_days == D("9.92")
    rows = list(LeaveLedger.objects.filter(employee=emp, period="2027-06").order_by("id").values_list("days", flat=True))
    assert rows == [D("1.75"), D("-0.58")]


def test_r40_recalculation_accrues_up_to_the_last_day_too(w):
    emp = hired(w, hire_date=date(2027, 1, 1))
    t = _approve(w, emp, last_working_day=date(2027, 6, 20), notice_given_on=date(2027, 4, 21))
    first = FinalSettlementInput.objects.get(termination=t)
    assert first.leave_balance_days == D("9.92")  # Jan to May in full, June to the 20th
    services.recalculate_eosb(t, user=w.h)
    latest = FinalSettlementInput.objects.get(termination=t, status="draft")
    assert latest.leave_balance_days == D("9.92") and LeaveLedger.objects.filter(employee=emp).count() == 6


def test_r41_withdrawal_reverses_the_partial_month_accrual(w):
    emp = hired(w, hire_date=date(2027, 1, 1))
    t = _approve(w, emp, last_working_day=date(2027, 6, 20), notice_given_on=date(2027, 4, 21))
    services.withdraw_termination(t, user=w.c, reason_ar="تراجع")
    assert services.leave_balance(emp, "ANNUAL", user=w.h) == D("8.75")  # the June part is reversed
    services.accrue_leave(2027, 6)  # the job accrues June in full again
    assert services.leave_balance(emp, "ANNUAL", user=w.h) == D("10.50")
