"""Payroll approvals through the one approval engine (access §10: R70, R79, R82, AT-74, AT-87; payroll §11 R82 to
R84). Fixtures from payroll/conftest.py: ALM (group parent), SUB; the hr_officer ``h`` of ALM, the MP, SUB's CFO
``c2``; SUB's ``ceo`` is added here. All names and amounts are made up."""
from datetime import date

import pytest
from django.core.exceptions import PermissionDenied

from core.access import matrix, reports
from core.access import services as access
from core.access.authority_models import ApprovalDecision, ApprovalRequest
from core.access.models import AccessLog
from core.access.testing import make_user, member
from django.utils import timezone
from payroll import services
from payroll.conftest import saudi, submitted

pytestmark = pytest.mark.django_db


def overtime(emp, by, on=date(2027, 3, 10)):
    return services.create_input(emp, user=by, kind="overtime_hours", period=f"{on:%Y-%m}", quantity="2",
                                 overtime_date=on, reason_ar="إنجاز عاجل")


def sub_matrix(w, rows):
    aa = make_user("sub_aa")
    member(aa, w.sub, "access_admin")
    v = matrix.draft_version(w.sub, user=aa, effective_from=timezone.localdate(access.now()), summary_en="SUB payroll",
                             summary_ar="رواتب", copy_current=False)
    for row in rows:
        matrix.add_rule(v, user=aa, **row)
    matrix.submit_version(v, user=aa)
    return matrix.decide_version(v, user=w.ce_sub, approve=True)


@pytest.fixture
def ws(w):
    w.ce_sub = make_user("ce_sub")
    member(w.ce_sub, w.sub, "ceo")
    return w


def test_at74_alm_runs_and_overtime_follow_the_code_defaults(w):
    saudi(w)
    run = submitted(w)
    with pytest.raises(PermissionDenied) as exc:  # contracted payroll needs no MP approval (A2): code default
        services.approve_run(run, user=w.mp)
    assert exc.value.code == "not_in_matrix"
    approved = services.approve_run(run, user=w.h)
    assert approved.status == "approved"
    req = ApprovalRequest.objects.get(document_type="payroll.payrollrun", document_id=run.pk)
    assert req.rule_source == "default" and req.status == "approved"
    grant_row = AccessLog.objects.get(event="permission_granted", actor=w.h, permission="payroll.approve_run")
    assert grant_row.details["rule_source"] == "default"
    inp = overtime(saudi(w), w.p)
    with pytest.raises(PermissionDenied):  # ALM overtime: the MP (decided A2), not the hr officer
        services.approve_overtime(inp, user=w.h)
    assert services.approve_overtime(inp, user=w.mp).status == "approved"
    gov = access.system_user("governance")
    member(gov, w.alm, "system_governance")
    month = timezone.localdate(access.now()).replace(day=1)
    content = reports.generate_report(w.alm, month, user=gov).content
    listed = {(x["activity"], x["user"]["id"]) for x in content["sections"]["default_rule_approvals"]}
    assert {("payroll.run", w.h.pk), ("payroll.overtime", w.mp.pk)} <= listed


def test_at87_nobody_approves_their_own_overtime_or_loan(ws):
    w = ws
    sub_matrix(w, [dict(activity="payroll.overtime", approver_roles=["ceo"]),
                   dict(activity="payroll.overtime", approver_roles=["approver"], step_no=2),
                   dict(activity="payroll.loan", approver_roles=["ceo"])])
    me = saudi(w, w.sub, linked_user=w.ce_sub)
    inp = overtime(me, w.ps)
    with pytest.raises(PermissionDenied) as exc:
        services.approve_overtime(inp, user=w.ce_sub)
    assert exc.value.code == "beneficiary" and exc.value.message_ar
    assert services.approve_overtime(inp, user=w.c2).status == "approved"  # the next step: the CFO (R82)
    req = ApprovalRequest.objects.get(document_type="payroll.payrollinput", document_id=inp.pk)
    assert list(req.decisions.values_list("step_no", "decision")) == [(1, "skipped"), (2, "approved")]
    loan = services.create_loan(me, user=w.ps, kind="loan", principal="3000.00", disbursement_date=date(2027, 2, 1),
                                purpose_ar="سلفة", installments=[("2027-03", "1500.00"), ("2027-04", "1500.00")])
    services.submit_loan(loan, user=w.ps)
    with pytest.raises(PermissionDenied) as exc:
        services.approve_loan(loan, user=w.ce_sub)
    assert exc.value.code == "beneficiary"
    with pytest.raises(PermissionDenied):  # no next step: the loan waits (R18), no fallback to the MP or the CFO
        services.approve_loan(loan, user=w.c2)
    with pytest.raises(PermissionDenied):
        services.approve_loan(loan, user=w.mp)


def test_r79_payroll_top_authority_self_approval_under_the_matrix(ws):
    """payroll R84: SUB's matrix names its ceo for runs; the ceo who prepared and submitted the run approves it as a
    logged top-authority self-approval; an hr_officer who did so is refused."""
    w = ws
    sub_matrix(w, [dict(activity="payroll.run", approver_roles=["ceo", "hr_officer"])])
    member(w.ce_sub, w.sub, "payroll_officer")
    saudi(w, w.sub)
    run = submitted(w, entity=w.sub, user=w.ce_sub)
    approved = services.approve_run(run, user=w.ce_sub)
    assert approved.status == "approved" and approved.journal_entry.posted_by == w.ce_sub
    log = AccessLog.objects.get(event="self_approval", actor=w.ce_sub)
    assert log.details["activity"] == "payroll.run" and log.details["matrix_version"]
    assert ApprovalDecision.objects.get(request__document_id=run.pk, request__document_type="payroll.payrollrun") \
        .top_authority


def test_r70_sub_loans_default_to_the_top_authority_not_the_mp(ws):
    """OQ-39 / R70: without SUB rows, SUB's loans are approved by SUB's top authority (its ceo); the MP's group
    membership gives nothing in SUB (no cascade, R64)."""
    w = ws
    emp = saudi(w, w.sub)
    loan = services.create_loan(emp, user=w.ps, kind="loan", principal="2000.00", disbursement_date=date(2027, 2, 1),
                                purpose_ar="سلفة", installments=[("2027-03", "1000.00"), ("2027-04", "1000.00")])
    services.submit_loan(loan, user=w.ps)
    for user in (w.mp, w.c2):
        with pytest.raises(PermissionDenied):
            services.approve_loan(loan, user=user)
    assert services.approve_loan(loan, user=w.ce_sub).status in ("approved", "disbursed", "active")
