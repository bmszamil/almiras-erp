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


def test_regs_art18_qiwa_reference_required_for_ksa(w):
    emp = employee(w)
    c = _submit(w, emp, qiwa_contract_ref="")
    with pytest.raises(HrError) as exc:
        services.approve_contract(c, user=w.c)
    assert "Qiwa" in exc.value.messages[0]


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
    egp = services.create_contract(local, user=w.he, components={"BASIC": "9000.00"}, **{**fields, "currency": "EGP"})
    assert services.submit_contract(egp, user=w.he).status == "submitted"


def test_contract_for_terminated_employee_refused(w):
    emp = hired(w, hire_date=date(2025, 1, 1))
    t = services.record_termination(emp, user=w.h, kind="employer_termination", last_working_day=date(2027, 5, 1),
                                    notice_given_on=date(2027, 3, 1), reason_ar="سبب")
    services.approve_termination(services.submit_termination(t, user=w.h), user=w.c)
    with pytest.raises(HrError):
        services.create_contract(emp, user=w.h)
    assert make_user("x").pk
