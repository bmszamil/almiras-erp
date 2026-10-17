"""hr R15 to R21, R53, R56 with the compliance corrections of 2026-10-17 (probation 180 in all cases, non-Saudi
open-ended refused, Art. 55 warning, Qiwa reference) (AT-9, AT-10, AT-11, AT-27, AT-28, AT-31)."""
from datetime import date

import pytest

from core import access
from core.access.testing import make_user
from hr import services
from hr.conftest import contract, contract_fields, employee, hired
from hr.models import ContractComponent, EmploymentContract, HrError


def _submit(w, emp, components=None, **fields):
    return contract(w, emp, components, approve=False, **fields)


# --- R15 / AT-9 -------------------------------------------------------------------------------------------

def test_at9_segregation_and_immutability(w):
    emp = employee(w)
    v1 = _submit(w, emp)
    with pytest.raises(access.AccessDenied):
        services.approve_contract(v1, user=w.h)
    v1 = services.approve_contract(v1, user=w.c)
    assert v1.status == "approved" and v1.approved_by == w.c and v1.parameter_ids
    with pytest.raises(HrError):
        services.update_contract(v1, user=w.h, job_title_en="Changed")
    v1.job_title_en = "Changed"
    with pytest.raises(HrError):
        v1.save()
    with pytest.raises(HrError):
        ContractComponent.objects.filter(contract=v1).update(monthly_amount="1.00")
    with pytest.raises(HrError):
        EmploymentContract.objects.filter(pk=v1.pk).update(status="draft")
    own = employee(w, linked_user=w.c)
    c = _submit(w, own)
    with pytest.raises(access.AccessDenied) as exc:
        services.approve_contract(c, user=w.c)
    assert exc.value.code == "self_approval"
    assert services.approve_contract(c, user=w.mp).status == "approved"  # managing_partner fallback (OQ-25)


def test_r15_creator_with_approver_role_cannot_approve(w):
    from core.access.testing import member

    member(w.c, w.alm, "hr_officer")
    emp = employee(w)
    c = services.create_contract(emp, user=w.c, components={"BASIC": "5000.00"}, **contract_fields(w, emp))
    services.submit_contract(c, user=w.c)
    with pytest.raises(access.AccessDenied) as exc:
        services.approve_contract(c, user=w.c)
    assert exc.value.code == "self_approval"


def test_r15_reject_and_withdraw(w):
    emp = employee(w)
    c = _submit(w, emp)
    with pytest.raises(HrError):
        services.reject_contract(c, user=w.c, reason_ar="")
    assert services.reject_contract(c, user=w.c, reason_ar="غير مكتمل").status == "rejected"
    d = services.create_contract(emp, user=w.h, components={"BASIC": "5000.00"}, **contract_fields(w, emp))
    assert d.version_no == 2 and services.withdraw_contract(d, user=w.h).status == "withdrawn"


# --- R16 to R21 / AT-10 (as corrected by the Labour Law review) -------------------------------------------

@pytest.mark.parametrize("fields, components, words", [
    ({"contract_type": "fixed", "end_date": None}, None, "end date"),
    ({"probation_periods": [181]}, None, "180"),
    ({"probation_periods": [90, 91]}, None, "180"),
    ({}, {"HOUSING": "1000.00"}, "BASIC"),
    ({"weekly_hours": "50"}, None, "hours"),
    ({"daily_hours": "9"}, None, "hours"),
    ({"currency": "USD"}, None, "functional currency"),
    ({"annual_leave_days": 15}, None, "21 or 30"),
    ({"annual_leave_days": 25}, None, "21 or 30"),
    ({"annual_leave_days": 31}, None, "21 or 30"),
    ({"notice_days_employer": 30}, None, "Notice"),
    ({"department": None}, None, "department"),
])
def test_at10_contract_checks_refuse(w, fields, components, words):
    emp = employee(w)
    with pytest.raises(HrError) as exc:
        _submit(w, emp, components, **fields)
    assert words in " ".join(exc.value.messages)


def test_at10_probation_up_to_180_and_split_periods_accepted(w):
    for periods in ([120], [180], [90, 90]):
        emp = employee(w)
        assert contract(w, emp, probation_periods=periods).status == "approved"


def test_at10_float_amount_refused(w):
    emp = employee(w)
    with pytest.raises(HrError) as exc:
        services.create_contract(emp, user=w.h, components={"BASIC": 5000.5}, **contract_fields(w, emp))
    assert exc.value.code == "float_rejected"


def test_r16_non_saudi_open_ended_refused(w):
    emp = employee(w, saudi=False)
    with pytest.raises(HrError) as exc:
        _submit(w, emp, contract_type="unlimited", end_date=None)
    assert "Art. 37" in exc.value.messages[0]
    assert contract(w, emp).contract_type == "fixed"


def test_r16_saudi_fixed_term_four_years_warns_open_ended(w):
    emp = employee(w)
    c = contract(w, emp, contract_type="fixed", end_date=date(2031, 6, 30))
    assert c.warnings and "Art. 55" in c.warnings[0]
    short = contract(w, employee(w), contract_type="fixed", end_date=date(2028, 12, 31))
    assert short.warnings == []


def test_r17_one_probation_per_employer(w):
    emp = hired(w, id_number="1000000777", hire_date=date(2025, 1, 1))
    t = services.record_termination(emp, user=w.h, kind="employer_termination", last_working_day=date(2027, 3, 31),
                                    notice_given_on=date(2027, 1, 1), reason_ar="سبب")
    services.approve_termination(services.submit_termination(t, user=w.h), user=w.c)
    again = employee(w, id_number="1000000777", hire_date=date(2027, 5, 1))
    with pytest.raises(HrError) as exc:
        _submit(w, again)
    assert "Art. 54" in exc.value.messages[0]
    assert contract(w, again, probation_periods=[]).status == "approved"
    assert contract(w, employee(w, id_number="1000000778", hire_date=date(2027, 5, 1)),
                    job_title_en="Driver", job_title_ar="سائق").status == "approved"


def test_r17_amendment_cannot_add_probation(w):
    emp = hired(w)
    with pytest.raises(HrError):
        contract(w, emp, approve=False, effective_date=date(2027, 4, 1), probation_periods=[60])


def test_at39_qiwa_reference_is_required_for_activation_not_approval(w):
    w.clock.set(date(2027, 1, 1))
    emp = employee(w)  # hire and start 2027-01-01 = today
    c = services.approve_contract(_submit(w, emp, qiwa_contract_ref=""), user=w.c)
    assert c.status == "approved" and any("Regs Art. 18" in x for x in c.warnings)
    emp.refresh_from_db()
    assert emp.status == "draft"
    with pytest.raises(HrError) as exc:
        services.activate_employee(emp, user=w.h)
    assert exc.value.code == "qiwa_required"
    assert "Regs Art. 18" in exc.value.messages[0] and "قوى" in exc.value.messages[0]
    assert services.refresh_statuses(today=date(2027, 1, 1)) == 0  # the daily job refuses it as well
    services.record_qiwa_reference(c, "QIWA-2027-0001", user=w.h)
    emp.refresh_from_db()
    assert emp.status == "active"  # the started version now has its reference
    c.refresh_from_db()
    assert c.qiwa_contract_ref == "QIWA-2027-0001" and c.status == "approved"
    with pytest.raises(HrError):  # filled in once only
        services.record_qiwa_reference(c, "QIWA-OTHER", user=w.h)


def test_r61_activate_employee_after_reference(w):
    w.clock.set(date(2027, 1, 1))
    emp = employee(w)
    c = services.approve_contract(_submit(w, emp, qiwa_contract_ref=""), user=w.c)
    with pytest.raises(access.AccessDenied):
        services.record_qiwa_reference(c, "QIWA-1", user=w.p)
    services.record_qiwa_reference(c, "QIWA-1", user=w.h)
    emp.refresh_from_db()
    assert emp.status == "active"
    with pytest.raises(HrError) as exc:
        services.activate_employee(emp, user=w.h)
    assert exc.value.code == "employee_status"


def test_r61_activation_waits_for_the_start_date(w):
    emp = employee(w, hire_date=date(2027, 9, 1))
    contract(w, emp)
    with pytest.raises(HrError) as exc:
        services.activate_employee(emp, user=w.h)
    assert exc.value.code == "not_started"


def test_r61_remuneration_staff_need_no_qiwa_reference(w):
    w.clock.set(date(2027, 1, 1))
    emp = employee(w, basis="remuneration")
    contract(w, emp)
    emp.refresh_from_db()
    assert emp.status == "active"


def test_r21_annual_leave_below_statutory_after_five_years(w):
    emp = hired(w, hire_date=date(2020, 1, 1))
    with pytest.raises(HrError) as exc:
        contract(w, emp, approve=False, effective_date=date(2027, 3, 1))  # 21 days, 7 years' service
    assert "statutory 30" in exc.value.messages[0]
    assert contract(w, emp, effective_date=date(2027, 3, 1), annual_leave_days=30).status == "approved"


# --- R20 / AT-11 ------------------------------------------------------------------------------------------

def test_at11_terms_on_and_identity(w):
    emp = employee(w, hire_date=date(2027, 1, 1))
    v1 = contract(w, emp, {"BASIC": "10000.00"})
    v2 = contract(w, emp, {"BASIC": "11000.00"}, effective_date=date(2027, 3, 15))
    assert v2.kind == "amendment" and v2.version_no == 2
    basic = lambda c: c.components.get(component__code="BASIC").monthly_amount  # noqa: E731
    assert services.terms_on(emp, date(2027, 3, 14), user=w.p) == v1 and basic(v1) == 10000
    assert services.terms_on(emp, date(2027, 3, 15), user=w.p) == v2 and basic(v2) == 11000
    d = services.create_contract(emp, user=w.h, effective_date=date(2027, 5, 1))
    with pytest.raises(HrError) as exc:
        services.update_contract(d, user=w.h, entity=w.sub)
    assert exc.value.code == "contract_identity"
    with pytest.raises(HrError):
        services.update_contract(d, user=w.h, employee=hired(w))
    with pytest.raises(HrError):
        contract(w, emp, approve=False, effective_date=date(2026, 12, 1))  # before the start


# --- R52, R53 / AT-27, AT-28 --------------------------------------------------------------------------------

def test_at27_remuneration_contract(w):
    r1 = employee(w, basis="remuneration")
    c = contract(w, r1, notice_days_employer=0, notice_days_employee=0, weekly_hours="70", daily_hours="12")
    assert c.status == "approved" and c.currency_id == "USD" and c.warnings == []
    r1.refresh_from_db()
    assert r1.status == "active" and services.document_flag(r1, user=w.h) == ("ok", [])
    with pytest.raises(HrError):
        contract(w, employee(w, basis="remuneration"), {"REMUNERATION": "5000.00", "HOUSING": "100.00"}, approve=False)
    with pytest.raises(HrError) as exc:
        contract(w, employee(w, basis="remuneration"), {"REMUNERATION": "5000.005"}, approve=False)
    assert "USD" in exc.value.messages[0] and "2 decimals" in exc.value.messages[0]
    kwd = contract(w, employee(w, basis="remuneration"), {"REMUNERATION": "1250.125"}, currency="KWD")
    assert kwd.components.get().monthly_amount == __import__("decimal").Decimal("1250.125")
    with pytest.raises(HrError):
        contract(w, employee(w, basis="remuneration"), approve=False, probation_periods=[30])
    with pytest.raises(HrError):
        contract(w, employee(w, basis="remuneration"), approve=False, annual_leave_days=21)


def test_at28_functional_currency_for_ksa_and_local(w):
    with pytest.raises(HrError):
        _submit(w, employee(w), currency="USD")
    local = employee(w, w.egy, basis="local_entity_employee")
    dept = services.create_department(w.egy, user=w.he, code="OPS", name_en="Ops", name_ar="العمليات")
    grade = services.create_grade(w.egy, user=w.he, code="G1", name_en="G1", name_ar="د1", overtime_eligible=False)
    fields = contract_fields(w, local, department=dept, grade=grade)
    usd = services.create_contract(local, user=w.he, components={"BASIC": "100.00"}, **{**fields, "currency": "USD"})
    with pytest.raises(HrError) as exc:
        services.submit_contract(usd, user=w.he)
    assert "EGP" in exc.value.messages[0] and "عملة" in exc.value.messages[0]
    egp = services.create_contract(local, user=w.he, components={"BASIC": "9000.00"},
                                   **{**fields, "currency": "EGP", "qiwa_contract_ref": ""})
    assert services.submit_contract(egp, user=w.he).status == "submitted"


def test_contract_for_terminated_employee_refused(w):
    emp = hired(w, hire_date=date(2025, 1, 1))
    t = services.record_termination(emp, user=w.h, kind="employer_termination", last_working_day=date(2027, 5, 1),
                                    notice_given_on=date(2027, 3, 1), reason_ar="سبب")
    services.approve_termination(services.submit_termination(t, user=w.h), user=w.c)
    with pytest.raises(HrError):
        services.create_contract(emp, user=w.h)
    assert make_user("x").pk


# --- R17 / AT-33: rehire and probation ----------------------------------------------------------------------

def _left(w, id_number):
    emp = hired(w, id_number=id_number, hire_date=date(2026, 6, 1))
    t = services.record_termination(emp, user=w.h, kind="employer_termination", last_working_day=date(2027, 3, 31),
                                    notice_given_on=date(2027, 1, 1), reason_ar="سبب")
    services.approve_termination(services.submit_termination(t, user=w.h), user=w.c)
    return emp


def test_at33_rehire_same_job_within_six_months_gets_no_probation(w):
    _left(w, "1000000901")
    again = employee(w, id_number="1000000901", hire_date=date(2027, 6, 1))
    with pytest.raises(HrError) as exc:
        _submit(w, again, probation_agreement_ref="AGR-1")
    assert "Art. 54" in exc.value.messages[0]


def test_at33_rehire_after_six_months_needs_the_written_agreement(w):
    _left(w, "1000000902")
    w.clock.set(date(2027, 11, 1))
    again = employee(w, id_number="1000000902", hire_date=date(2027, 11, 1))
    with pytest.raises(HrError) as exc:
        _submit(w, again)
    assert "written agreement" in exc.value.messages[0]
    c = contract(w, again, probation_agreement_ref="Written agreement 2027/11")
    assert c.status == "approved" and c.probation_days == 90


def test_at33_rehire_in_another_job_needs_the_written_agreement(w):
    _left(w, "1000000903")
    again = employee(w, id_number="1000000903", hire_date=date(2027, 6, 1))
    with pytest.raises(HrError):
        _submit(w, again, job_title_en="Driver", job_title_ar="سائق")
    assert contract(w, again, job_title_en="Driver", job_title_ar="سائق",
                    probation_agreement_ref="AGR-2").status == "approved"


def test_at33_probation_end_skips_founding_day_and_sick_days(w):
    emp = hired(w, hire_date=date(2027, 2, 1))  # probation 90 days
    c = emp.contracts.get(status="approved")
    # Founding Day 2027-02-22 and Eid al-Fitr 1448 (2027-03-09 to 03-12, seeded from the Umm al-Qura table) skipped.
    assert services.probation_end_date(c, user=w.h) == date(2027, 5, 6)
    req = services.request_leave(emp, "SICK", user=w.h, start_date=date(2027, 3, 20), end_date=date(2027, 3, 22),
                                 document_ref="MED-1")
    services.approve_leave(req, user=w.h2)
    assert services.probation_end_date(c, user=w.h) == date(2027, 5, 9)  # and the 3 sick days


# --- R16(b), R63 / AT-10 (Art. 55) and R19 (Art. 99) ---------------------------------------------------------

def test_at10_saudi_fourth_fixed_term_refused(w):
    emp = employee(w)
    c = contract(w, emp, contract_type="fixed", end_date=date(2028, 1, 1), renewal_count=3)
    assert any("Art. 55" in x for x in c.warnings)  # the third renewal reaches the limit
    with pytest.raises(HrError) as exc:
        _submit(w, emp, contract_type="fixed", effective_date=date(2028, 1, 2), end_date=date(2029, 1, 1),
                renewal_count=4)
    assert "Art. 55" in exc.value.messages[0] and "المادة 55" in exc.value.messages[0]
    with pytest.raises(HrError):  # also when the count is not raised: the previous term reached the limit
        _submit(w, emp, contract_type="fixed", effective_date=date(2028, 1, 2), end_date=date(2029, 1, 1))
    assert contract(w, emp, contract_type="unlimited", end_date=None, effective_date=date(2028, 1, 2)).status == "approved"


def test_r16_salary_amendment_inside_the_same_fixed_term_is_not_a_renewal(w):
    emp = employee(w)
    contract(w, emp, contract_type="fixed", end_date=date(2031, 6, 30))  # reaches 4 years: warned
    c = contract(w, emp, {"BASIC": "6500.00"}, contract_type="fixed", end_date=date(2031, 6, 30),
                 effective_date=date(2027, 4, 1))
    assert c.status == "approved"


def test_r19_nine_hour_days_only_with_an_art99_reference(w):
    emp = employee(w)
    with pytest.raises(HrError) as exc:
        _submit(w, emp, daily_hours="9", weekly_hours="45")
    assert "Art. 98, 99" in exc.value.messages[0]
    c = contract(w, emp, daily_hours="9", weekly_hours="45", extended_hours_ref="Ministerial decision 1/99 (test)")
    assert c.status == "approved"
    with pytest.raises(HrError):
        _submit(w, employee(w), daily_hours="10", weekly_hours="45", extended_hours_ref="Ministerial decision (test)")


# --- Nits 13 to 15 ---------------------------------------------------------------------------------------------

def test_nit13_contract_notice_minimum_follows_paid_monthly(w):
    with pytest.raises(HrError):  # monthly paid: 60 days for the employer
        _submit(w, employee(w), notice_days_employer=30, notice_days_employee=30)
    weekly = _submit(w, employee(w), paid_monthly=False, notice_days_employer=30, notice_days_employee=30)
    assert weekly.status == "submitted"  # not monthly paid: 30 days either side (Art. 75)
    with pytest.raises(HrError):
        _submit(w, employee(w), paid_monthly=False, notice_days_employer=30, notice_days_employee=29)


def test_nit14_zero_decimal_currency_for_remuneration(w):
    jpy = contract(w, employee(w, basis="remuneration"), {"REMUNERATION": "600000"}, currency="JPY")
    assert jpy.status == "approved"
    with pytest.raises(HrError) as exc:
        contract(w, employee(w, basis="remuneration"), {"REMUNERATION": "600000.5"}, currency="JPY", approve=False)
    assert "JPY" in exc.value.messages[0]


def test_nit15_an_amendment_needs_its_own_qiwa_reference(w):
    emp = hired(w)
    v1 = emp.contracts.get(status="approved")
    v2 = services.create_contract(emp, user=w.h, effective_date=date(2027, 3, 1))
    assert v2.qiwa_contract_ref == ""  # not copied from v1
    services.update_contract(v2, user=w.h, qiwa_contract_ref=v1.qiwa_contract_ref)
    with pytest.raises(HrError) as exc:
        services.submit_contract(v2, user=w.h)
    assert "Qiwa" in exc.value.messages[0] and "قوى" in exc.value.messages[0]
    services.update_contract(v2, user=w.h, qiwa_contract_ref="")
    v2 = services.approve_contract(services.submit_contract(v2, user=w.h), user=w.c)
    assert any("Regs Art. 18" in x for x in v2.warnings)
    with pytest.raises(HrError):
        services.record_qiwa_reference(v2, v1.qiwa_contract_ref, user=w.h)
    assert services.record_qiwa_reference(v2, "QIWA-AMEND-2", user=w.h).qiwa_contract_ref == "QIWA-AMEND-2"
