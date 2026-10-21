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


def test_r61_an_ibra_rebate_is_its_own_engine_request_on_the_contract(w):
    """access 10.5 (payroll.deferred_sale_rebate under payroll.deferred_sale): the rebate is approved with
    payroll.approve_deferred_sale on its contract, but as its own request (own rules, own decision), its preparer
    refused as its maker; the sale's request is untouched."""
    from payroll.test_review_fixes_2026_10_20b import rebate, sold

    c = sold(w)
    r = rebate(w, c)
    with pytest.raises(PermissionDenied):  # the preparer never approves it
        services.approve_deferred_sale_rebate(r, user=w.p)
    services.approve_deferred_sale_rebate(r, user=w.mp)
    reqs = {q.document_version: q for q in ApprovalRequest.objects.filter(
        document_type="payroll.deferredsalecontract", document_id=c.pk, activity__code="payroll.deferred_sale")}
    assert set(reqs) == {"sale", f"rebate:{r.pk}"}
    rebate_req = reqs[f"rebate:{r.pk}"]
    assert rebate_req.status == "approved" and rebate_req.amount == r.amount
    assert list(rebate_req.decisions.values_list("user", "decision")) == [(w.mp.pk, "approved")]
    assert list(reqs["sale"].decisions.values_list("user", flat=True)) == [w.mp.pk]


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


def test_r82_the_top_authority_never_approves_a_run_with_their_own_inputs(ws):
    """access R79(e), R82; payroll R39 (review of 92c35c2, item 5): SUB's ceo self-approves a run they prepared, but
    not one in which their own payslip has an input; the input's approver is no beneficiary."""
    w = ws
    sub_matrix(w, [dict(activity="payroll.run", approver_roles=["ceo", "hr_officer"])])
    member(w.ce_sub, w.sub, "payroll_officer")
    ps2 = make_user("sub_ps2")
    member(ps2, w.sub, "payroll_officer")
    me = saudi(w, w.sub, linked_user=w.ce_sub)
    inp = services.create_input(me, user=w.ps, kind="one_off_earning", period="2027-03", amount="100.00",
                                reason_ar="مكافأة")
    services.approve_input(inp, user=ps2)
    run = submitted(w, entity=w.sub, user=w.ce_sub)
    assert services._run_beneficiaries(run) == {w.ce_sub.pk}
    with pytest.raises(PermissionDenied) as exc:
        services.approve_run(run, user=w.ce_sub)
    assert exc.value.code == "beneficiary" and exc.value.message_ar
    assert not ApprovalDecision.objects.filter(request__document_id=run.pk,
                                               request__document_type="payroll.payrollrun").exists()


def test_r71_a_band_gap_is_refused_when_payroll_submits(ws):
    """access R61, R71 (review of 92c35c2, item 7): payroll's submit services open the engine request, so a loan
    above the last band of SUB's matrix is refused at submission and stays a draft."""
    from payroll.models import EmployeeLoan

    w = ws
    sub_matrix(w, [dict(activity="payroll.loan", approver_roles=["ceo"], amount_from="0", amount_to="1000.00")])
    emp = saudi(w, w.sub)
    small = services.create_loan(emp, user=w.ps, kind="loan", principal="800.00", disbursement_date=date(2027, 2, 1),
                                 purpose_ar="سلفة", installments=[("2027-03", "400.00"), ("2027-04", "400.00")])
    services.submit_loan(small, user=w.ps)
    req = ApprovalRequest.objects.get(document_type="payroll.employeeloan", document_id=small.pk)
    assert (req.status, req.rule_source, req.submitted_by) == ("open", "matrix", w.ps)
    big = services.create_loan(emp, user=w.ps, kind="loan", principal="3000.00", disbursement_date=date(2027, 2, 1),
                               purpose_ar="سلفة", installments=[("2027-03", "1500.00"), ("2027-04", "1500.00")])
    with pytest.raises(PermissionDenied) as exc:
        services.submit_loan(big, user=w.ps)
    assert exc.value.code == "no_authority_rule"
    assert EmployeeLoan.objects.get(pk=big.pk).status == "draft"
    assert not ApprovalRequest.objects.filter(document_type="payroll.employeeloan", document_id=big.pk).exists()


def test_r61_a_bai_ajel_closure_is_its_own_engine_activity_and_right(w):
    """access 10.5 (2026-10-21): DeferredSaleClosure approvals use payroll.approve_deferred_sale_closure, activity
    payroll.deferred_sale_closure, opened at submission; its holders are the loan write-off's (seed V18)."""
    from core.access.seed import ROLE_SEED_V18
    from payroll.test_review_fixes_2026_10_21 import _after_exit_balance, _recovery

    holders = {perm: {c for c, row in ROLE_SEED_V18.items() if perm in row[5]}
               for perm in ("payroll.approve_loan_write_off", "payroll.approve_deferred_sale_closure")}
    assert holders["payroll.approve_deferred_sale_closure"] == holders["payroll.approve_loan_write_off"]
    _, c = _after_exit_balance(w)
    x = _recovery(w, c, "30.00")
    req = ApprovalRequest.objects.get(document_type="payroll.deferredsaleclosure", document_id=x.pk)
    assert (req.activity.code, req.status, req.submitted_by) == ("payroll.deferred_sale_closure", "open", w.p)
    with pytest.raises(PermissionDenied):  # its preparer
        services.approve_deferred_sale_closure(x, user=w.p)
    done = services.approve_deferred_sale_closure(x, user=w.mp)
    assert done.status == "approved"
    grant_row = AccessLog.objects.filter(event="permission_granted", actor=w.mp,
                                         permission="payroll.approve_deferred_sale_closure").first()
    assert grant_row is not None and grant_row.details["activity"] == "payroll.deferred_sale_closure"
    assert ApprovalRequest.objects.get(pk=req.pk).status == "approved"
