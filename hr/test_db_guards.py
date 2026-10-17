"""hr R7, R14, R15, R24/R25, R29, R40, R41: PostgreSQL triggers refuse writes that bypass the ORM guards."""
from datetime import date

import pytest
from django.db import DatabaseError, connection, transaction

from hr import services
from hr.conftest import contract, employee, hired
from hr.models import Employee, FinalSettlementInput, HrError, StatutoryParameter

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


def _terminated(w, emp):
    t = services.record_termination(emp, user=w.h, kind="employer_termination", last_working_day=date(2027, 6, 20),
                                    notice_given_on=date(2027, 4, 21), reason_ar="سبب")
    return services.approve_termination(services.submit_termination(t, user=w.h), user=w.c)


def test_finding9_settlement_input_is_written_once(w):
    """Review probe P6 (ORM): the hand-off to payroll is never edited."""
    emp = hired(w, hire_date=date(2020, 1, 1))
    t = _terminated(w, emp)
    with pytest.raises(HrError):
        FinalSettlementInput.objects.filter(termination=t).update(eosb_award=1)
    si = FinalSettlementInput.objects.get(termination=t)
    si.eosb_award = 999999
    with pytest.raises(HrError):
        si.save()
    with pytest.raises(HrError):
        si.delete()
    with pytest.raises(HrError):  # an employee's status never moves backwards outside the services
        emp.refresh_from_db()
        emp.status, emp._service_write = "draft", True
        emp.save()


@pg
def test_finding9_triggers_refuse_raw_writes_to_approved_hr_records(w):
    """Review probe PG2: approved leave, employee status and basis, settlement inputs, an approved termination
    inserted directly, assignments and leave adjustments."""
    emp = hired(w, hire_date=date(2026, 1, 1))
    r = services.request_leave(emp, "UNPAID", user=w.h, start_date=date(2027, 7, 1), end_date=date(2027, 7, 5),
                               reason_ar="س")
    services.approve_leave(r, user=w.h2)
    _refused("UPDATE hr_leaverequest SET end_date = '2027-12-31', unpaid = false WHERE id = %s", [r.pk])
    _refused("UPDATE hr_leaverequest SET unpaid = false WHERE id = %s", [r.pk])
    _refused("UPDATE hr_leaverequest SET status = 'submitted' WHERE id = %s", [r.pk])
    _refused("DELETE FROM hr_leaverequest WHERE id = %s", [r.pk])
    with connection.cursor() as cur:  # the service's own moves stay possible: shortening
        cur.execute("UPDATE hr_leaverequest SET end_date = '2027-07-03', days = 3 WHERE id = %s", [r.pk])
    _refused("UPDATE hr_employee SET status = 'draft', employment_basis = 'remuneration' WHERE id = %s", [emp.pk])
    _refused("UPDATE hr_employee SET employment_basis = 'remuneration' WHERE id = %s", [emp.pk])
    _refused("UPDATE hr_employee SET entity_id = %s WHERE id = %s", [w.sub.pk, emp.pk])
    a = services.create_assignment(emp, user=w.h, kind="secondment", receiving_entity=w.sub, role_title_en="A",
                                   role_title_ar="أ", start_date=date(2027, 2, 1))
    _refused("UPDATE hr_assignment SET status = 'active', approved_by_id = %s, reference = 'X' WHERE id = %s",
             [w.c.pk, a.pk])  # not submitted
    a = services.approve_assignment(services.submit_assignment(a, user=w.h), user=w.c)
    _refused("UPDATE hr_assignment SET receiving_entity_id = %s WHERE id = %s", [w.alm.pk, a.pk])
    _refused("UPDATE hr_assignment SET start_date = '2027-01-15' WHERE id = %s", [a.pk])
    adj = services.request_leave_adjustment(emp, "ANNUAL", "2", date(2027, 3, 1), user=w.h, reason_ar="تصحيح")
    _refused("UPDATE hr_leaveadjustment SET status = 'approved', approved_by_id = created_by_id WHERE id = %s",
             [adj.pk])
    services.approve_leave_adjustment(adj, user=w.c)
    _refused("UPDATE hr_leaveadjustment SET days = 20 WHERE id = %s", [adj.pk])
    t = _terminated(w, emp)
    _refused("UPDATE hr_finalsettlementinput SET eosb_award = 999999 WHERE termination_id = %s", [t.pk])
    _refused("UPDATE hr_finalsettlementinput SET status = 'draft' WHERE termination_id = %s", [t.pk])
    _refused("DELETE FROM hr_finalsettlementinput WHERE termination_id = %s", [t.pk])
    assert Employee.objects.get(pk=emp.pk).status == "on_notice"
    _refused("UPDATE hr_employee SET status = 'draft' WHERE id = %s", [emp.pk])
    _refused(
        "INSERT INTO hr_termination (entity_id, employee_id, contract_id, kind, article_ref, last_working_day, reason_en, "
        "reason_ar, status, created_by_id, created_at, source, notice_shortfall_days, deemed_accepted, worker_initiated, "
        "warnings, protection_articles, postponement_reason_ar, postponement_reason_en, written_consent_ref, "
        "article_paragraph, hearing_ref, absence_warning_ref, compensation_ref, deadline_alerts, gosi_exclusion_reason, "
        "gosi_exclusion_ref) SELECT entity_id, employee_id, contract_id, kind, '', last_working_day, '', 'x', 'approved', "
        "created_by_id, now(), 'human', 0, false, false, '[]', '[]', '', '', '', '', '', '', '', '{}', '', '' "
        "FROM hr_termination WHERE id = %s", [t.pk])


@pg
def test_finding9_approvals_without_or_by_their_own_maker_are_refused(w):
    emp = employee(w)
    c = contract(w, emp, approve=False)
    _refused("UPDATE hr_employmentcontract SET status = 'approved', approved_by_id = created_by_id WHERE id = %s", [c.pk])
    _refused("UPDATE hr_employmentcontract SET status = 'approved', approved_by_id = NULL WHERE id = %s", [c.pk])
    c = services.approve_contract(c, user=w.c)
    emp.refresh_from_db()
    t = services.record_termination(emp, user=w.h, kind="employer_termination", last_working_day=date(2027, 6, 20),
                                    notice_given_on=date(2027, 4, 21), reason_ar="سبب")
    _refused("UPDATE hr_termination SET status = 'approved', approved_by_id = %s WHERE id = %s", [w.c.pk, t.pk])  # draft
    t = services.submit_termination(t, user=w.h)
    _refused("UPDATE hr_termination SET status = 'approved' WHERE id = %s", [t.pk])  # no approver
    _refused("UPDATE hr_termination SET status = 'approved', approved_by_id = submitted_by_id WHERE id = %s", [t.pk])
    t = services.approve_termination(t, user=w.c)
    _refused("UPDATE hr_termination SET gosi_exclusion_ref = 'X', reason_ar = 'y' WHERE id = %s", [t.pk])


@pg
def test_finding10_superseded_events_are_final(w, settings):
    settings.HR_ASSIGNMENT_EVENT_MAX_ATTEMPTS = 1
    emp = hired(w)
    a = services.create_assignment(emp, user=w.h, kind="secondment", receiving_entity=w.sub, role_title_en="A",
                                   role_title_ar="أ", start_date=date(2027, 2, 1))
    a = services.approve_assignment(services.submit_assignment(a, user=w.h), user=w.c)
    w.billing.link(a.reference)
    w.billing.fail_times = 1
    _, ev = services.end_assignment(a, date(2027, 3, 1), user=w.h, reason_ar="انتهاء")
    _refused("UPDATE hr_assignmentevent SET delivery_status = 'superseded' WHERE id = %s", [ev.pk])  # pending
    services.deliver_assignment_events()
    services.supersede_assignment_event(ev, user=w.h, reason_ar="يدوي")
    _refused("UPDATE hr_assignmentevent SET delivery_status = 'pending' WHERE id = %s", [ev.pk])
    _refused("UPDATE hr_assignmenteventaction SET reason_ar = 'x' WHERE event_id = %s", [ev.pk])
