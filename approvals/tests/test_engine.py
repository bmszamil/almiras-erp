"""Approval engine: acceptance tests T3-T18, T20, T21, T23, T24 and rules R5-R18, R21, D10, D11."""
import datetime
from decimal import Decimal

import pytest
from django.core import mail
from django.db import IntegrityError, transaction
from django.utils import timezone

from access import services as access
from access.errors import AccessDenied, BilingualError, Conflict, NotFound, RuleError
from access.models import AuditLog, Role
from approvals import notify, services
from approvals.models import (
    ApprovableType, ApprovalAction, ApprovalPolicy, ApprovalRequest, ApprovalStep, Delegation, Notification,
)
from approvals.services import PostingFailed

pytestmark = pytest.mark.django_db
D = Decimal
S = ApprovalRequest.Status


def submit(doc, user):
    with django_capture():
        return services.submit(doc, user)


class django_capture:
    """Run on_commit callbacks (emails) immediately, like a real commit would."""

    def __enter__(self):
        from django.test import TestCase

        self.cm = TestCase.captureOnCommitCallbacks(execute=True)
        return self.cm.__enter__()

    def __exit__(self, *exc):
        return self.cm.__exit__(*exc)


def approve(request, user, **kw):
    request.refresh_from_db()
    with django_capture():
        return services.act(request, user, "approve", document_version=request.document_version, **kw)


# ---------------------------------------------------------------- policy resolution and initial data


def test_r6_initial_policy_per_spending_type_mp_one_step(payment_type):
    for t in ApprovableType.objects.filter(is_spending=True):
        p = ApprovalPolicy.objects.get(document_type=t, entity__isnull=True)
        assert p.amount_min is None and p.amount_max is None
        steps = list(p.steps.all())
        assert len(steps) == 1 and steps[0].approver_role.code == "managing_partner"


def test_r6_journal_entry_policy_loaded_by_migration():
    p = ApprovalPolicy.objects.get(document_type__code="ledger.journal_entry", entity__isnull=True)
    assert p.steps.get().approver_role.code == "managing_partner"


def test_t3_submit_payment_pending_for_mp_with_in_app_and_email_in_his_language(alm, mp, accountant, make_payment):
    mp.language = "ar"
    mp.save()
    pay = make_payment(accountant, alm, "1000")
    r = submit(pay, accountant)
    assert r.status == S.PENDING and r.amount == D("1000.00") and r.currency == "SAR"
    assert r.policy.steps.get(sequence=1).approver_role.code == "managing_partner"
    pay.refresh_from_db()
    assert pay.status == "pending_approval"
    n = Notification.objects.get(user=mp, kind="approval_pending")
    assert n.channels_sent["in_app"]["status"] == "sent" and n.channels_sent["email"]["status"] == "sent"
    assert len(mail.outbox) == 1 and mail.outbox[0].subject == n.title_ar and mail.outbox[0].to == [mp.email]
    assert AuditLog.objects.filter(action="approval.submit", object_id=str(r.pk)).exists()


def test_t4_mp_approves_logs_action_and_audit_and_locks_document(alm, mp, accountant, make_payment):
    pay = make_payment(accountant, alm)
    r = approve(submit(pay, accountant), mp)
    assert r.status == S.APPROVED and r.decided_at
    a = r.actions.get(action="approve")
    assert a.actor == mp and a.on_behalf_of is None and not a.self_approval_override
    row = AuditLog.objects.get(action="approval.approve", object_id=str(r.pk))
    assert row.before["request"]["status"] == "pending" and row.after["request"]["status"] == "approved"
    pay.refresh_from_db()
    assert pay.status == "executed"


def test_t5_edit_amount_of_approved_document_refused_bilingual(alm, mp, accountant, make_payment):
    from testdocs.models import Payment

    pay = make_payment(accountant, alm)
    approve(submit(pay, accountant), mp)
    pay.refresh_from_db()
    pay.amount = D("2")
    with pytest.raises(RuleError) as e:
        pay.save()
    assert e.value.message_en and e.value.message_ar
    assert Payment.objects.get(pk=pay.pk).amount == D("1000.00")


def test_t6_mp_self_approval_logged_and_reported(alm, mp, make_payment):
    pay = make_payment(mp, alm, "750")
    r = submit(pay, mp)
    # MP is told this will be a self-approval; nobody else is eligible, so no 'no_eligible' alert.
    assert Notification.objects.filter(user=mp, kind="approval_pending").exists()
    r = approve(r, mp)
    assert r.status == S.APPROVED
    assert r.actions.get(action="approve").self_approval_override is True
    row = AuditLog.objects.get(action="approval.self_approval_override", object_id=str(r.pk))
    assert row.after["self_approval_override"] is True
    now = timezone.localtime()
    auditor_like = mp  # MP holds audit.view
    report = services.self_approval_report(auditor_like, now.year, now.month)
    assert [x["request_id"] for x in report] == [r.pk] and report[0]["amount"] == "750.00"


def test_t6_self_approval_report_visible_to_chairman_and_auditor(alm, mp, chairman, make_user, make_payment):
    auditor = make_user("ext-auditor", (alm, "auditor"))
    r = approve(submit(make_payment(mp, alm), mp), mp)
    now = timezone.localtime()
    for viewer in (chairman, auditor):
        assert [x["request_id"] for x in services.self_approval_report(viewer, now.year, now.month)] == [r.pk]
    outsider = make_user("acc2", (alm, "accountant"))
    assert services.self_approval_report(outsider, now.year, now.month) == []


def test_t6_d10_no_self_approval_when_another_eligible_approver_exists(alm, mp, make_user, make_payment):
    make_user("mp2", (alm, "managing_partner"))
    r = submit(make_payment(mp, alm), mp)
    with pytest.raises(AccessDenied):
        approve(r, mp)


def test_t6_r8_non_mp_creator_cannot_self_approve(alm, mp, make_user, make_payment):
    # Chairman is the only approver in a tier and also the creator; no override for him.
    chair = make_user("chair2", (alm, "chairman"), (alm, "accountant"))
    p = services.create_policy(
        mp, document_type=ApprovableType.objects.get(code="payments.payment"), entity=alm,
        name_en="Chair", name_ar="رئيس", currency="SAR", priority=5,
        steps=[{"sequence": 1, "approver_role": Role.objects.get(code="chairman")}],
    )
    r = submit(make_payment(chair, alm), chair)
    assert r.policy == p
    with pytest.raises(AccessDenied):
        approve(r, chair)
    assert Notification.objects.filter(alert_code="no_eligible_approver").exists()


def test_r8_submitter_cannot_approve(alm, mp, make_user, make_payment):
    other_mp = make_user("mp2", (alm, "managing_partner"))
    r = submit(make_payment(mp, alm), mp)
    approve(r, other_mp)  # the other MP can
    r2 = submit(make_payment(other_mp, alm), other_mp)
    with pytest.raises(AccessDenied):
        approve(r2, other_mp)


def test_r9_no_eligible_approver_alerts_mp(alm, mp, make_user, make_payment):
    # Payment created by accountant, but the only MP is deactivated: nobody can approve.
    acc = make_user("acc", (alm, "accountant"))
    mp.is_active = False
    mp.save()
    r = submit(make_payment(acc, alm), acc)
    assert r.status == S.PENDING
    assert Notification.objects.filter(alert_code="no_eligible_approver", user=acc).exists()


def test_t7_delegate_approves_within_limits_on_behalf_of_mp(alm, mp, accountant, make_user, make_payment, clock):
    delegate = make_user("deputy", (alm, "viewer"))
    tz = timezone.get_current_timezone()
    services.create_delegation(
        mp, delegator=mp, delegate=delegate, entity=alm,
        document_types=[ApprovableType.objects.get(code="payments.payment")], amount_max=D("50000"),
        starts_at=datetime.datetime(2026, 11, 1, tzinfo=tz), ends_at=datetime.datetime(2026, 11, 11, tzinfo=tz),
    )
    clock.set(datetime.datetime(2026, 11, 5, 10, tzinfo=tz))
    r = approve(submit(make_payment(accountant, alm, "20000"), accountant), delegate)
    assert r.status == S.APPROVED
    a = r.actions.get(action="approve")
    assert a.actor == delegate and a.on_behalf_of == mp
    assert AuditLog.objects.get(action="approval.approve", object_id=str(r.pk)).on_behalf_of == mp


@pytest.mark.parametrize("amount,day", [("60000", 5), ("20000", 11)])
def test_t8_delegate_refused_over_limit_or_after_end(alm, mp, accountant, make_user, make_payment, clock, amount, day):
    delegate = make_user("deputy", (alm, "viewer"))
    tz = timezone.get_current_timezone()
    services.create_delegation(
        mp, delegator=mp, delegate=delegate, entity=alm, amount_max=D("50000"),
        document_types=[ApprovableType.objects.get(code="payments.payment")],
        starts_at=datetime.datetime(2026, 11, 1, tzinfo=tz), ends_at=datetime.datetime(2026, 11, 11, tzinfo=tz),
    )
    clock.set(datetime.datetime(2026, 11, day, 10, tzinfo=tz))
    r = submit(make_payment(accountant, alm, amount), accountant)
    with pytest.raises(AccessDenied):
        approve(r, delegate)


def test_r16_delegate_needs_view_access_in_entity(alm, sub, mp, make_user):
    outsider = make_user("outsider", (sub, "viewer"))
    now = timezone.now()
    with pytest.raises(RuleError):
        services.create_delegation(mp, delegator=mp, delegate=outsider, entity=alm,
                                   starts_at=now, ends_at=now + datetime.timedelta(days=1))


def test_t9_delegate_who_created_payment_cannot_approve_it(alm, mp, make_user, make_payment):
    deputy = make_user("deputy", (alm, "accountant"))
    now = timezone.now()
    services.create_delegation(mp, delegator=mp, delegate=deputy, entity=alm,
                               starts_at=now - datetime.timedelta(hours=1), ends_at=now + datetime.timedelta(days=3))
    r = submit(make_payment(deputy, alm), deputy)
    with pytest.raises(AccessDenied):
        approve(r, deputy)
    with pytest.raises(AccessDenied):
        approve(r, deputy, on_behalf_of=mp)


def test_t10_new_tier_routes_to_mp_then_chairman_without_code(alm, mp, chairman, accountant, make_payment):
    services.create_policy(
        mp, document_type=ApprovableType.objects.get(code="payments.payment"), entity=None,
        # Same priority as the open-ended base policy would overlap (T20), so the tier uses priority 10.
        amount_min=D("500000"), currency="SAR", priority=10, name_en="Large payments", name_ar="دفعات كبيرة",
        steps=[{"sequence": 1, "approver_role": Role.objects.get(code="managing_partner")},
               {"sequence": 2, "approver_role": Role.objects.get(code="chairman")}],
    )
    r = submit(make_payment(accountant, alm, "600000"), accountant)
    assert r.policy.name_en == "Large payments" and r.policy.steps.count() == 2
    r = approve(r, mp)
    assert r.status == S.PENDING and r.current_sequence == 2
    assert Notification.objects.filter(user=chairman, kind="approval_pending").exists()
    with pytest.raises(AccessDenied):
        approve(r, mp)  # one person counts once
    r = approve(r, chairman)
    assert r.status == S.APPROVED
    small = submit(make_payment(accountant, alm, "1000"), accountant)
    assert small.policy.steps.count() == 1  # narrower group-wide range wins only where it matches


def test_r5_entity_specific_policy_beats_group_wide(alm, sub, mp, make_user, make_payment):
    services.create_policy(
        mp, document_type=ApprovableType.objects.get(code="payments.payment"), entity=alm, currency="SAR",
        priority=0, name_en="ALM only", name_ar="المراس فقط",
        steps=[{"sequence": 1, "approver_role": Role.objects.get(code="chairman")}],
    )
    acc = make_user("acc", (alm, "accountant"))
    assert submit(make_payment(acc, alm), acc).policy.name_en == "ALM only"


def test_r5_narrowest_range_then_priority(alm, mp, make_payment, make_user):
    t = ApprovableType.objects.get(code="payments.payment")
    chair = Role.objects.get(code="chairman")
    services.create_policy(mp, document_type=t, entity=alm, amount_min=D("0"), amount_max=D("100000"), currency="SAR",
                           priority=0, name_en="wide", name_ar="w", steps=[{"approver_role": chair}])
    services.create_policy(mp, document_type=t, entity=alm, amount_min=D("0"), amount_max=D("10000"), currency="SAR",
                           priority=1, name_en="narrow", name_ar="n", steps=[{"approver_role": chair}])
    services.create_policy(mp, document_type=t, entity=alm, amount_min=D("0"), amount_max=D("10000"), currency="SAR",
                           priority=9, name_en="narrow-high", name_ar="nh", steps=[{"approver_role": chair}])
    acc = make_user("acc", (alm, "accountant"))
    assert submit(make_payment(acc, alm, "5000"), acc).policy.name_en == "narrow-high"
    assert submit(make_payment(acc, alm, "50000"), acc).policy.name_en == "wide"


def test_r7_used_policy_cannot_be_edited_only_retired(alm, mp, accountant, make_payment):
    r = submit(make_payment(accountant, alm), accountant)
    p = r.policy
    p.amount_max = D("5")
    with pytest.raises(RuleError):
        p.save()
    p.refresh_from_db()
    with pytest.raises(RuleError):
        p.steps.first().delete()
    services.update_policy(mp, p, is_active=False)
    assert ApprovalPolicy.objects.get(pk=p.pk).is_active is False


def test_r7_policy_management_needs_permission(alm, accountant):
    with pytest.raises(AccessDenied):
        services.create_policy(accountant, document_type=ApprovableType.objects.get(code="ledger.journal_entry"),
                               entity=alm, currency="SAR", priority=1, name_en="x", name_ar="x",
                               steps=[{"approver_role": Role.objects.get(code="chairman")}])


def test_t11_escalation_after_48h_to_chairman(alm, mp, chairman, accountant, make_payment, clock):
    t = ApprovableType.objects.get(code="payments.payment")
    services.create_policy(
        mp, document_type=t, entity=alm, currency="SAR", priority=0, name_en="esc", name_ar="تصعيد",
        steps=[{"sequence": 1, "approver_role": Role.objects.get(code="managing_partner"), "escalate_after_hours": 48,
                "escalate_to_role": Role.objects.get(code="chairman")}],
    )
    r = submit(make_payment(accountant, alm), accountant)
    clock.advance(hours=47)
    assert services.run_escalations(at=clock.now) == (0, 0)
    clock.advance(hours=2)
    assert services.run_escalations(at=clock.now) == (1, 0)
    assert r.actions.filter(action="escalate").count() == 1
    assert Notification.objects.filter(user=chairman, kind="escalated").exists()
    assert AuditLog.objects.filter(action="approval.escalate", object_id=str(r.pk)).exists()
    assert services.run_escalations(at=clock.now) == (0, 0)  # escalates once
    r = approve(r, chairman)  # escalation target may now decide
    assert r.status == S.APPROVED


def test_r15_expiry_when_policy_has_timeout(alm, mp, accountant, make_payment, clock):
    t = ApprovableType.objects.get(code="payments.payment")
    services.create_policy(mp, document_type=t, entity=alm, currency="SAR", priority=0, name_en="exp", name_ar="exp",
                           expire_after_hours=72, steps=[{"approver_role": Role.objects.get(code="managing_partner")}])
    pay = make_payment(accountant, alm)
    r = submit(pay, accountant)
    clock.advance(hours=73)
    assert services.run_escalations(at=clock.now) == (0, 1)
    r.refresh_from_db()
    pay.refresh_from_db()
    assert r.status == S.EXPIRED and pay.status == "draft"


def test_t12_document_changed_after_submission_409_and_returned(alm, mp, accountant, make_payment):
    from testdocs.models import Payment

    pay = make_payment(accountant, alm)
    r = submit(pay, accountant)
    Payment.objects.filter(pk=pay.pk).update(amount=D("999999"))  # change bypassing the model guard
    with pytest.raises(Conflict) as e:
        approve(r, mp)
    assert e.value.code == "document_changed"
    r.refresh_from_db()
    assert r.status == S.RETURNED
    assert Payment.objects.get(pk=pay.pk).status == "draft"


def test_r11_pending_document_is_locked_too(alm, accountant, make_payment):
    pay = make_payment(accountant, alm)
    submit(pay, accountant)
    pay.refresh_from_db()
    pay.counterparty = "Someone else"
    with pytest.raises(RuleError):
        pay.save()


def test_t13_reject_without_comment_422_status_unchanged(alm, mp, accountant, make_payment):
    r = submit(make_payment(accountant, alm), accountant)
    with pytest.raises(RuleError) as e:
        services.act(r, mp, "reject", comment="  ", document_version=r.document_version)
    assert e.value.status == 422
    r.refresh_from_db()
    assert r.status == S.PENDING


def test_r17_return_without_comment_refused(alm, mp, accountant, make_payment):
    r = submit(make_payment(accountant, alm), accountant)
    with pytest.raises(RuleError):
        services.act(r, mp, "return", document_version=r.document_version)


def test_r14_reject_rejects_whole_request(alm, mp, accountant, make_payment):
    pay = make_payment(accountant, alm)
    r = services.act(submit(pay, accountant), mp, "reject", comment="No budget", document_version=submit_version(pay))
    assert r.status == S.REJECTED
    pay.refresh_from_db()
    assert pay.status == "rejected"


def submit_version(doc):
    return ApprovalRequest.objects.filter(object_id=str(doc.pk)).latest("id").document_version


def test_t14_return_with_comment_back_to_draft_resubmission_new_request(alm, mp, accountant, make_payment):
    pay = make_payment(accountant, alm)
    r = submit(pay, accountant)
    r = services.act(r, mp, "return", comment="Attach the invoice", document_version=r.document_version)
    assert r.status == S.RETURNED
    pay.refresh_from_db()
    assert pay.status == "draft"
    r2 = submit(pay, accountant)
    assert r2.pk != r.pk and r2.status == S.PENDING
    assert Notification.objects.filter(user=accountant, kind="approval_returned").exists()


def test_r14_min_approvals_needs_distinct_users(alm, mp, make_user, accountant, make_payment):
    mp2 = make_user("mp2", (alm, "managing_partner"))
    services.create_policy(mp, document_type=ApprovableType.objects.get(code="payments.payment"), entity=alm,
                           currency="SAR", priority=0, name_en="two", name_ar="اثنان",
                           steps=[{"approver_role": Role.objects.get(code="managing_partner"), "min_approvals": 2}])
    r = submit(make_payment(accountant, alm), accountant)
    r = approve(r, mp)
    assert r.status == S.PENDING
    with pytest.raises(AccessDenied):
        approve(r, mp)
    assert approve(r, mp2).status == S.APPROVED


def test_r10_final_states_cannot_change(alm, mp, accountant, make_payment):
    r = approve(submit(make_payment(accountant, alm), accountant), mp)
    with pytest.raises(Conflict):
        services.act(r, mp, "reject", comment="late", document_version=r.document_version)
    r.status = S.PENDING
    with pytest.raises(RuleError):
        r.save()
    with pytest.raises(RuleError):
        ApprovalRequest.objects.filter(pk=r.pk).update(status="weird")
    from django.db import connection

    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cur:
        cur.execute("UPDATE approvals_approvalrequest SET status = 'weird' WHERE id = %s", [r.pk])


def test_one_pending_request_per_document(alm, accountant, make_payment):
    pay = make_payment(accountant, alm)
    submit(pay, accountant)
    from testdocs.models import Payment

    Payment.objects.filter(pk=pay.pk).update(status="draft")
    with pytest.raises(Conflict):
        submit(Payment.objects.get(pk=pay.pk), accountant)


def test_r13_only_submitter_or_cancel_any_can_cancel_and_only_pending(alm, mp, accountant, make_user, make_payment):
    other = make_user("acc2", (alm, "accountant"))
    pay = make_payment(accountant, alm)
    r = submit(pay, accountant)
    with pytest.raises(AccessDenied):
        services.cancel(r, other)
    r = services.cancel(r, accountant)
    assert r.status == S.CANCELLED
    pay.refresh_from_db()
    assert pay.status == "draft"
    with pytest.raises(Conflict):
        services.cancel(r, accountant)
    r2 = submit(pay, accountant)
    assert services.cancel(r2, mp).status == S.CANCELLED  # MP holds cancel_any


def test_actions_are_append_only(alm, accountant, make_payment):
    r = submit(make_payment(accountant, alm), accountant)
    a = r.actions.get()
    a.comment = "x"
    with pytest.raises(BilingualError):
        a.save()
    with pytest.raises(BilingualError):
        a.delete()


def test_t15_agent_drafts_and_submits_bill_with_agent_source(alm, mp, agent, make_bill):
    bill = make_bill(agent, alm)
    r = submit(bill, agent)
    assert bill.source == "agent:invoice-reader" and r.source == "agent:invoice-reader"
    assert AuditLog.objects.get(action="approval.submit", object_id=str(r.pk)).source == "agent:invoice-reader"


def test_t16_agent_cannot_approve_regardless_of_roles(alm, mp, agent, accountant, make_payment):
    from access.models import EntityAccess

    # Force an MP grant past the service guard to prove the hard check in act().
    EntityAccess.objects.create(user=agent, entity=alm, role=Role.objects.get(code="managing_partner"),
                                valid_from=datetime.date(2026, 1, 1))
    r = submit(make_payment(accountant, alm), accountant)
    with pytest.raises(AccessDenied) as e:
        approve(r, agent)
    assert e.value.status == 403
    with pytest.raises(AccessDenied):
        services.cancel(r, agent)


def test_r18_agent_cannot_create_delegation(alm, agent, mp):
    now = timezone.now()
    with pytest.raises(AccessDenied):
        services.create_delegation(agent, delegator=agent, delegate=mp, starts_at=now, ends_at=now + datetime.timedelta(days=1))


def test_t2_viewer_in_alm_cannot_submit_alm_draft(alm, sub, make_user, make_payment):
    u = make_user("u", (alm, "viewer"), (sub, "accountant"))
    with pytest.raises(AccessDenied) as e:
        submit(make_payment(u, alm), u)
    assert e.value.status == 403


def test_t20_overlapping_policy_same_entity_priority_refused(alm, mp, payment_type):
    t = ApprovableType.objects.get(code="payments.payment")
    chair = Role.objects.get(code="chairman")
    services.create_policy(mp, document_type=t, entity=alm, amount_min=D("0"), amount_max=D("1000"), currency="SAR",
                           priority=1, name_en="a", name_ar="a", steps=[{"approver_role": chair}])
    with pytest.raises(RuleError) as e:
        services.create_policy(mp, document_type=t, entity=alm, amount_min=D("500"), amount_max=None, currency="SAR",
                               priority=1, name_en="b", name_ar="b", steps=[{"approver_role": chair}])
    assert e.value.code == "policy_overlap"
    # adjacent range (max exclusive) is fine
    services.create_policy(mp, document_type=t, entity=alm, amount_min=D("1000"), currency="SAR",
                           priority=1, name_en="c", name_ar="c", steps=[{"approver_role": chair}])


def test_t21_no_policy_bilingual_error(alm, mp, accountant, make_payment):
    ApprovalPolicy.objects.filter(document_type__code="payments.payment").update(is_active=False)
    with pytest.raises(RuleError) as e:
        submit(make_payment(accountant, alm), accountant)
    assert e.value.code == "no_policy" and e.value.message_ar and e.value.message_en
    assert not ApprovalRequest.objects.exists()


def test_t23_email_down_request_saved_in_app_created_retry_logged(alm, mp, accountant, make_payment, monkeypatch):
    def broken(*a, **k):
        raise ConnectionRefusedError("SMTP down")

    monkeypatch.setattr(notify, "send_mail", broken)
    r = submit(make_payment(accountant, alm), accountant)
    assert ApprovalRequest.objects.get(pk=r.pk).status == S.PENDING
    n = Notification.objects.get(user=mp, kind="approval_pending")
    assert n.channels_sent["email"]["status"] == "failed" and "SMTP down" in n.channels_sent["email"]["error"]
    monkeypatch.undo()
    assert notify.retry_failed_emails() == 1
    n.refresh_from_db()
    assert n.channels_sent["email"]["status"] == "sent" and n.channels_sent["email"]["attempts"] == 2


def test_r21_push_is_hook_only_and_no_email_without_address(alm, mp, make_user, make_payment):
    from approvals.models import PushDevice

    acc = make_user("noemail", (alm, "accountant"), email="")
    PushDevice.objects.create(user=mp, platform="ios", token="t1")
    r = submit(make_payment(acc, alm), acc)
    n = Notification.objects.get(user=mp, kind="approval_pending")
    assert n.channels_sent["push"]["status"] == "hook_inactive"
    services.cancel(r, acc)
    assert not Notification.objects.filter(user=acc, kind="approval_pending").exists()


def test_t24_new_subsidiary_uses_group_wide_policy(alm, mp, make_user, make_bill):
    from core.models import Entity

    new = Entity.objects.create(code="NEW", name_en="NewCo", name_ar="شركة جديدة", parent=alm)
    acc = make_user("newacc", (new, "accountant"))
    new_mp = make_user("newmp", (new, "managing_partner"))
    r = submit(make_bill(acc, new), acc)
    assert r.policy.entity is None and r.entity == new
    assert approve(r, new_mp).status == S.APPROVED


def test_r1_visible_requests_filtered_by_entity_and_own_only(alm, sub, mp, make_user, make_payment):
    acc = make_user("acc", (alm, "accountant"))
    sub_acc = make_user("subacc", (sub, "accountant"))
    faisal = make_user("faisal", (alm, "pa_drafter"))
    r1 = submit(make_payment(acc, alm), acc)
    r2 = submit(make_payment(sub_acc, sub), sub_acc)
    r3 = submit(make_payment(faisal, alm), faisal)
    assert set(services.visible_requests(acc)) == {r1, r3}
    assert set(services.visible_requests(faisal)) == {r3}  # pa_drafter sees own only
    with pytest.raises(NotFound):
        services.get_visible(acc, r2.pk)
