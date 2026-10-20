"""hr R68 (H20(d), payroll R4: "activate only Egypt for now") and R69 (H20(b): a Saudi remuneration person needs the
GOSI employment relationship) as refusals in hr itself (payroll slice 2; hr AT-47, AT-52)."""
from datetime import date

import pytest
from django.test import override_settings

from core.access.testing import make_user, member
from hr import services
from hr.conftest import contract, employee, make_entity
from hr.models import HrError, StatutoryParameter


def _local_setup(w, entity, user):
    dept = services.create_department(entity, user=user, code="OPS", name_en="Operations", name_ar="العمليات")
    grade = services.create_grade(entity, user=user, code="G1", name_en="Staff", name_ar="موظف", overtime_eligible=False)
    return dict(department=dept, grade=grade, contract_type="fixed", end_date=date(2028, 12, 31),
                notice_days_employer=90, notice_days_employee=90, qiwa_contract_ref="")


def _people(entity):
    h, c = make_user(f"h-{entity.code}"), make_user(f"c-{entity.code}")
    member(h, entity, "hr_officer")
    member(c, entity, "approver")
    return h, c


def _local_contract(w, entity, h, c, **fields):
    emp = employee(w, entity, by=h, basis="local_entity_employee")
    if not hasattr(w, "local_setup"):
        w.local_setup = {}
    if entity.pk not in w.local_setup:
        w.local_setup[entity.pk] = _local_setup(w, entity, h)
    values = dict(w.local_setup[entity.pk])
    values.update(fields)
    return contract(w, emp, {"BASIC": "9000.00"}, drafter=h, approver=c, **values)


def test_r68_egypt_is_activated_and_checked_with_its_own_parameters(w):
    c = _local_contract(w, w.egy, w.he, w.ce)
    assert c.status == "approved"
    assert {StatutoryParameter.objects.get(pk=pk).jurisdiction for pk in c.parameter_ids} == {"EG"}  # never SA (R51)


def test_r68_egypt_local_checks_probation_hours_leave_and_notice(w):
    for fields, text in (({"probation_periods": [91]}, "Probation"), ({"daily_hours": "9"}, "Working hours"),
                         ({"annual_leave_days": 15}, "Annual leave"), ({"notice_days_employee": 60}, "Notice")):
        with pytest.raises(HrError) as exc:
            _local_contract(w, w.egy, w.he, w.ce, **fields)
        assert exc.value.code == "contract_invalid" and any(text in m for m in exc.value.messages), fields


@pytest.mark.parametrize("country", ["CN", "AE"])
def test_r68_at52_china_and_uae_are_not_activated_whatever_parameters_exist(w, country):
    other = make_entity(f"X{country}", "full", w.alm, country=country, currency="SAR")
    for code, value in (("probation.max_days", {"days": "90"}), ("hours.max_daily", {"hours": "8"}),
                        ("hours.max_weekly", {"hours": "48"}), ("leave.annual.days", {"from_year_2": "21"}),
                        ("termination.notice_days", {"days": "30"})):
        row = services.add_statutory_parameter(user=w.h, jurisdiction=country, code=code, value=value,
                                               valid_from=date(2020, 1, 1), source_url="https://example.test",
                                               source_reference="test row")
        services.verify_statutory_parameter(row, user=w.mp)
    h, c = _people(other)
    with pytest.raises(HrError) as exc:
        _local_contract(w, other, h, c, notice_days_employer=30, notice_days_employee=30)
    assert exc.value.code == "jurisdiction_not_activated"
    assert "not activated" in exc.value.messages[0] and "غير مفعلة" in exc.value.messages[0]
    local = employee(w, other, by=h, basis="local_entity_employee")  # master data is kept
    with pytest.raises(HrError) as exc:
        services.calculate_eosb(local, date(2027, 6, 1), "employer_termination", user=h)
    assert exc.value.code == "jurisdiction_not_activated"


@override_settings(HR_REQUIRE_VERIFIED_PARAMETERS=True)
def test_r68_activation_row_must_be_verified_when_verified_rows_are_required(w):
    with pytest.raises(HrError) as exc:
        _local_contract(w, w.egy, w.he, w.ce)
    assert exc.value.code == "parameter_unverified" and "jurisdiction.active" in exc.value.messages[0]


def test_r68_an_inactive_row_switches_a_country_off_as_data(w):
    row = StatutoryParameter.objects.get(jurisdiction="EG", code="jurisdiction.active")
    services.add_statutory_parameter(user=w.h, jurisdiction="EG", code="jurisdiction.active", value={"active": False},
                                     valid_from=date(2027, 1, 1), source_url=row.source_url,
                                     source_reference="test: switched off")
    with pytest.raises(HrError) as exc:
        _local_contract(w, w.egy, w.he, w.ce)
    assert exc.value.code == "jurisdiction_not_activated"


def test_r69_at47_saudi_remuneration_contract_needs_the_gosi_relationship(w):
    r2 = employee(w, basis="remuneration", nationality="SA", id_type="national_id", id_number="1000000999",
                  country_of_residence="AE")
    with pytest.raises(HrError) as exc:
        contract(w, r2)
    assert exc.value.code == "contract_invalid" and any("R69" in m for m in exc.value.messages)
    services.update_employee(r2, user=w.h, gosi_employment_relationship="subordinate",
                             gosi_relationship_ref="AGREEMENT-1")
    assert contract(w, r2).status == "approved"
    b1 = employee(w, basis="remuneration", nationality="SA", id_type="national_id", id_number="1000000998",
                  country_of_residence="AE", gosi_employment_relationship="non_subordinate",
                  gosi_relationship_ref="BOARD-RES-1")
    assert contract(w, b1).status == "approved"
    r1 = employee(w, basis="remuneration")  # non-Saudi: no relationship needed
    assert contract(w, r1).status == "approved"
