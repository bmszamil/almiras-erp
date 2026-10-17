"""Review fixes (17 Oct 2026): request bulk guard (1), update_policy scope fields (4), D16 (7), D17 (8),
any posting exception (9), push-token takeover (10), compliance items."""
import datetime
import uuid
from decimal import Decimal

import pytest
from django.utils import timezone

from access import services as access
from access.errors import AccessDenied, RuleError
from access.models import AuditLog, Role
from approvals import services
from approvals.models import ApprovableType, ApprovalPolicy, ApprovalRequest, Notification, PushDevice
from approvals.services import PostingFailed

pytestmark = pytest.mark.django_db
D = Decimal


def approve(r, user, **kw):
    r.refresh_from_db()
    return services.act(r, user, "approve", document_version=r.document_version, **kw)


def delegate_from(mp, delegate, entity, **kw):
    now = timezone.now()
    return services.create_delegation(mp, delegator=mp, delegate=delegate, entity=entity,
                                      starts_at=now - datetime.timedelta(hours=1),
                                      ends_at=now + datetime.timedelta(days=5), **kw)


# ---------------------------------------------------------------- 1. requests cannot be bulk-changed


def test_requests_refuse_bulk_update_delete_create(alm, accountant, make_payment):
    r = services.submit(make_payment(accountant, alm), accountant)
    for call in (lambda: ApprovalRequest.objects.filter(pk=r.pk).update(status="approved"),
                 lambda: ApprovalRequest.objects.filter(pk=r.pk).update(summary_en="x"),
                 lambda: ApprovalRequest.objects.all().delete(),
                 lambda: ApprovalRequest.objects.bulk_create([ApprovalRequest(entity=alm)])):
        with pytest.raises(RuleError):
            call()
    assert ApprovalRequest.objects.get(pk=r.pk).status == "pending"


# ---------------------------------------------------------------- 7. D16


def test_d16_delegate_approves_mp_created_document_and_mp_cannot_self_approve(alm, mp, make_user, make_payment):
    deputy = make_user("deputy", (alm, "viewer"))
    delegate_from(mp, deputy, alm)
    r = services.submit(make_payment(mp, alm), mp)
    assert Notification.objects.filter(user=deputy, kind="approval_pending").exists()
    assert not Notification.objects.filter(user=mp, kind="approval_pending").exists()
    with pytest.raises(AccessDenied):
        approve(r, mp)
    r = approve(r, deputy)
    a = r.actions.get(action="approve")
    assert r.status == "approved" and a.actor == deputy and a.on_behalf_of == mp and not a.self_approval_override
    assert not AuditLog.objects.filter(action="approval.self_approval_override").exists()


def test_d16_delegation_outside_scope_does_not_block_self_approval(alm, mp, make_user, make_payment):
    deputy = make_user("deputy", (alm, "viewer"))
    delegate_from(mp, deputy, alm, amount_max=D("100"))  # does not cover SAR 1,000
    r = approve(services.submit(make_payment(mp, alm, "1000"), mp), mp)
    assert r.actions.get(action="approve").self_approval_override is True


def test_d16_ended_delegation_restores_self_approval(alm, mp, make_user, make_payment):
    deputy = make_user("deputy", (alm, "viewer"))
    d = delegate_from(mp, deputy, alm)
    r = services.submit(make_payment(mp, alm), mp)
    with pytest.raises(AccessDenied):
        approve(r, mp)
    services.end_delegation(mp, d)
    assert approve(r, mp).actions.get(action="approve").self_approval_override is True


def test_d16_delegate_who_created_the_document_still_cannot_approve(alm, mp, make_user, make_payment):
    deputy = make_user("deputy", (alm, "accountant"))
    delegate_from(mp, deputy, alm)
    r = services.submit(make_payment(deputy, alm), deputy)
    with pytest.raises(AccessDenied):
        approve(r, deputy)


# ---------------------------------------------------------------- 8. D17


@pytest.fixture
def integration(make_user, alm):
    return make_user("bank-feed", (alm, "agent_drafter"), user_type="service_integration")


def test_d17_integration_cannot_act_cancel_others_or_get_powerful_roles(alm, mp, accountant, integration, make_payment):
    from access.models import EntityAccess

    EntityAccess.objects.create(user=integration, entity=alm, role=Role.objects.get(code="managing_partner"),
                                valid_from=datetime.date(2026, 1, 1))
    assert not access.has_perm(integration, alm, "approvals.request.act")
    assert not access.has_perm(integration, alm, "ledger.entry.post")
    r = services.submit(make_payment(accountant, alm), accountant)
    with pytest.raises(AccessDenied):
        approve(r, integration)
    with pytest.raises(AccessDenied):
        services.cancel(r, integration)
    with pytest.raises(RuleError):
        access.grant_access(mp, integration, alm, "chairman")


def test_d17_integration_submits_own_draft_with_import_source(alm, integration, make_bill):
    r = services.submit(make_bill(integration, alm), integration)
    assert r.source == "import"


# ---------------------------------------------------------------- 9. any posting exception


def test_unexpected_posting_exception_rolls_back_and_alerts(alm, mp, accountant, make_payment, monkeypatch):
    from testdocs.approval import PaymentApproval

    def boom(self, doc, request, actor):
        raise RuntimeError("bank connector crashed: secret detail")

    monkeypatch.setattr(PaymentApproval, "on_approved", boom)
    pay = make_payment(accountant, alm)
    r = services.submit(pay, accountant)
    with pytest.raises(PostingFailed) as exc:
        approve(r, mp)
    assert "RuntimeError" in exc.value.message_en and "secret detail" not in exc.value.message_en
    r.refresh_from_db()
    pay.refresh_from_db()
    assert r.status == "pending" and pay.status == "pending_approval"
    assert AuditLog.objects.filter(action="approval.posting_failed", object_id=str(r.pk)).exists()
    assert Notification.objects.filter(alert_code="posting_failed", user=mp).exists()


# ---------------------------------------------------------------- 4. policy scope fields


def test_update_policy_scope_fields_fixed(alm, sub, mp, make_user, payment_type):
    make_user("mp-sub", (sub, "managing_partner"))
    p = services.create_policy(mp, document_type=ApprovableType.objects.get(code="payments.payment"), entity=alm,
                               currency="SAR", priority=3, name_en="ALM", name_ar="المراس",
                               steps=[{"approver_role": Role.objects.get(code="chairman")}])
    for field, value in [("entity", sub), ("currency", "USD"),
                         ("document_type", ApprovableType.objects.get(code="payables.bill"))]:
        with pytest.raises(RuleError) as e:
            services.update_policy(mp, p, **{field: value})
        assert e.value.code == "policy_scope_fixed"
    services.update_policy(mp, p, name_en="ALM renamed", entity=alm)  # unchanged scope value is fine
    assert ApprovalPolicy.objects.get(pk=p.pk).name_en == "ALM renamed"


def test_api_patch_policy_cannot_move_it_to_another_entity(alm, sub, mp, api, payment_type):
    p = services.create_policy(mp, document_type=ApprovableType.objects.get(code="payments.payment"), entity=alm,
                               currency="SAR", priority=3, name_en="ALM", name_ar="المراس",
                               steps=[{"approver_role": Role.objects.get(code="chairman")}])
    resp = api(mp).patch(f"/api/v1/approvals/policies/{p.pk}", {"entity": sub.pk}, format="json")
    assert resp.status_code == 422 and resp.json()["code"] == "policy_scope_fixed"
    assert ApprovalPolicy.objects.get(pk=p.pk).entity == alm


# ---------------------------------------------------------------- 10. push device takeover


def test_push_token_of_another_user_cannot_be_taken_over(alm, mp, accountant, api):
    assert api(mp).post("/api/v1/devices", {"platform": "ios", "token": "tok-1"}, format="json").status_code == 201
    resp = api(accountant).post("/api/v1/devices", {"platform": "android", "token": "tok-1"}, format="json")
    assert resp.status_code == 409 and resp.json()["code"] == "device_token_taken"
    d = PushDevice.objects.get(token="tok-1")
    assert d.user == mp and d.platform == "ios"
    api(accountant).delete("/api/v1/devices", {"token": "tok-1"}, format="json")
    assert PushDevice.objects.get(token="tok-1").is_active is True


# ---------------------------------------------------------------- compliance: audit security fields, access review


def test_audit_ip_and_user_agent_only_with_view_security(alm, mp, chairman, accountant, make_payment, api):
    services.submit(make_payment(accountant, alm), accountant, ctx=access.Context(channel="api", ip="10.1.2.3", user_agent="UA"))
    mp_rows = api(mp).get("/api/v1/audit?action=approval.submit").json()
    chair_rows = api(chairman).get("/api/v1/audit").json()
    submit_mp = [r for r in mp_rows if r["action"] == "approval.submit"][0]
    submit_chair = [r for r in chair_rows if r["action"] == "approval.submit"][0]
    assert submit_mp["ip"] == "10.1.2.3" and submit_mp["user_agent"] == "UA"
    assert "ip" not in submit_chair and "user_agent" not in submit_chair


def test_access_review_report_lists_roles_last_login_and_2fa(alm, sub, mp, make_user, api):
    make_user("sub-only", (sub, "accountant"))
    faisal = make_user("faisal", (alm, "pa_drafter"))
    access.enroll_totp(None, faisal)
    rows = access.access_review_report(mp)
    by_user = {(r["username"], r["role"]): r for r in rows}
    assert ("faisal", "pa_drafter") in by_user and by_user[("faisal", "pa_drafter")]["two_factor"] is True
    assert not any(r["entity"] == "LZM" for r in rows)  # MP has no LZM access (D12)
    assert {"last_login", "active", "granted_by", "valid_to"} <= set(rows[0])
    assert AuditLog.objects.filter(action="access.review_report", actor=mp).exists()
    assert api(mp).get("/api/v1/access/review").status_code == 200
    assert api(faisal).get("/api/v1/access/review").status_code == 403
