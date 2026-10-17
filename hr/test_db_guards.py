"""hr R7, R14, R15, R24/R25, R29, R40, R41: PostgreSQL triggers refuse writes that bypass the ORM guards."""
from datetime import date

import pytest
from django.db import DatabaseError, connection, transaction

from hr import services
from hr.conftest import contract, employee, hired
from hr.models import StatutoryParameter

pg = pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL triggers (hr migration 0003)")


def _refused(sql, params):
    with pytest.raises(DatabaseError):
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(sql, params)


@pg
def test_approved_contract_and_components_are_immutable(w):
    c = contract(w, employee(w))
    _refused("UPDATE hr_employmentcontract SET job_title_en = 'X' WHERE id = %s", [c.pk])
    _refused("UPDATE hr_employmentcontract SET status = 'draft' WHERE id = %s", [c.pk])
    _refused("DELETE FROM hr_employmentcontract WHERE id = %s", [c.pk])
    _refused("UPDATE hr_contractcomponent SET monthly_amount = 1 WHERE contract_id = %s", [c.pk])
    _refused("DELETE FROM hr_contractcomponent WHERE contract_id = %s", [c.pk])
    with connection.cursor() as cur:  # the one allowed change: ended_on (termination)
        cur.execute("UPDATE hr_employmentcontract SET ended_on = '2027-06-30' WHERE id = %s", [c.pk])
    # R61: a set Qiwa reference never changes on an approved version.
    _refused("UPDATE hr_employmentcontract SET qiwa_contract_ref = 'OTHER' WHERE id = %s", [c.pk])


@pg
def test_approved_contract_gets_its_qiwa_reference_once(w):
    c = contract(w, employee(w), qiwa_contract_ref="")
    _refused("UPDATE hr_employmentcontract SET qiwa_contract_ref = 'Q-1', job_title_en = 'X' WHERE id = %s", [c.pk])
    with connection.cursor() as cur:
        cur.execute("UPDATE hr_employmentcontract SET qiwa_contract_ref = 'Q-1' WHERE id = %s", [c.pk])
    _refused("UPDATE hr_employmentcontract SET qiwa_contract_ref = '' WHERE id = %s", [c.pk])
    _refused("UPDATE hr_employmentcontract SET qiwa_contract_ref = 'Q-2' WHERE id = %s", [c.pk])


@pg
def test_append_only_tables(w):
    emp = hired(w, hire_date=date(2025, 1, 1))
    services.accrue_leave(2027, 1)
    _refused("UPDATE hr_leaveledger SET days = 9 WHERE employee_id = %s", [emp.pk])
    _refused("DELETE FROM hr_leaveledger WHERE employee_id = %s", [emp.pk])
    t = services.record_termination(emp, user=w.h, kind="employer_termination", last_working_day=date(2027, 6, 30),
                                    notice_given_on=date(2027, 5, 1), reason_ar="سبب")
    t = services.approve_termination(services.submit_termination(t, user=w.h), user=w.c)
    _refused("UPDATE hr_eosbcalculation SET award = 1 WHERE termination_id = %s", [t.pk])
    _refused("DELETE FROM hr_eosbcalculation WHERE termination_id = %s", [t.pk])
    _refused("UPDATE hr_termination SET last_working_day = '2027-07-31' WHERE id = %s", [t.pk])
    _refused("DELETE FROM hr_employee WHERE id = %s", [emp.pk])


@pg
def test_used_parameter_and_events(w):
    hired(w)
    used = StatutoryParameter.objects.filter(used_at__isnull=False).first()
    _refused("UPDATE hr_statutoryparameter SET value = '{}' WHERE id = %s", [used.pk])
    _refused("DELETE FROM hr_statutoryparameter WHERE id = %s", [used.pk])
    with connection.cursor() as cur:  # verification stays possible
        cur.execute("UPDATE hr_statutoryparameter SET status = 'verified', verified_by_id = %s, verified_at = now() "
                    "WHERE id = %s", [w.mp.pk, used.pk])
    emp = hired(w)
    a = services.create_assignment(emp, user=w.h, kind="secondment", receiving_entity=w.sub, role_title_en="A",
                                   role_title_ar="أ", start_date=date(2027, 2, 1))
    a = services.approve_assignment(services.submit_assignment(a, user=w.h), user=w.c)
    _, ev = services.end_assignment(a, date(2027, 3, 1), user=w.h, reason_ar="انتهاء")
    _refused("UPDATE hr_assignmentevent SET end_date = '2027-04-01' WHERE id = %s", [ev.pk])
    _refused("DELETE FROM hr_assignmentevent WHERE id = %s", [ev.pk])
    services.deliver_assignment_events()
    _refused("UPDATE hr_assignmentevent SET attempts = 9 WHERE id = %s", [ev.pk])  # delivered: final


@pg
def test_r7_parameter_trigger_refuses_unlocking_and_self_verification(w):
    """Review probe PG1: clearing used_at, editing after it, verifier = enterer, status back, verified insert."""
    row = services.param("SA", "eosb.award_table", date(2027, 1, 1))
    with connection.cursor() as cur:
        cur.execute("UPDATE hr_statutoryparameter SET used_at = now() WHERE id = %s", [row.pk])
    _refused("UPDATE hr_statutoryparameter SET value = '{}'::jsonb WHERE id = %s", [row.pk])
    _refused("UPDATE hr_statutoryparameter SET used_at = NULL WHERE id = %s", [row.pk])
    _refused("UPDATE hr_statutoryparameter SET used_at = now() - interval '1 day' WHERE id = %s", [row.pk])
    _refused("UPDATE hr_statutoryparameter SET status = 'verified' WHERE id = %s", [row.pk])  # CHECK: no verifier
    entered = services.add_statutory_parameter(user=w.h, jurisdiction="SA", code="test.pg", value={"x": "1"},
                                               valid_from=date(2027, 1, 1), source_url="https://example.test",
                                               source_reference="t")
    _refused("UPDATE hr_statutoryparameter SET status = 'verified', verified_by_id = entered_by_id, verified_at = now() "
             "WHERE id = %s", [entered.pk])
    services.verify_statutory_parameter(entered, user=w.mp)
    _refused("UPDATE hr_statutoryparameter SET source_reference = 'x' WHERE id = %s", [entered.pk])
    _refused("UPDATE hr_statutoryparameter SET verified_by_id = %s WHERE id = %s", [w.c.pk, entered.pk])
    _refused("UPDATE hr_statutoryparameter SET status = 'unverified', verified_by_id = NULL, verified_at = NULL "
             "WHERE id = %s", [entered.pk])
    _refused("INSERT INTO hr_statutoryparameter (jurisdiction, code, value, valid_from, source_url, source_reference, "
             "status, verified_by_id, verified_at, created_at) VALUES ('SA', 'test.ins', '{}'::jsonb, '2027-01-01', "
             "'https://x.test', 't', 'verified', %s, now(), now())", [w.mp.pk])
    with connection.cursor() as cur:  # forward: verified -> superseded stays possible
        cur.execute("UPDATE hr_statutoryparameter SET status = 'superseded' WHERE id = %s", [entered.pk])
    _refused("UPDATE hr_statutoryparameter SET status = 'verified' WHERE id = %s", [entered.pk])
