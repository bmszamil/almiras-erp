"""REST API /api/v1/: T1, T2, T16, T19 (API part), T22, R1, R17, R22, error format."""
import datetime
import uuid

import pytest
from django.utils import timezone

from approvals import services
from approvals.models import ApprovalAction, ApprovalRequest

pytestmark = pytest.mark.django_db


def post_action(client, r, action, body=None, key=None):
    return client.post(f"/api/v1/approvals/requests/{r.pk}/{action}", body or {}, format="json",
                       HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()))


def test_t1_accountant_lists_only_alm_requests_and_gets_404_for_other_entity(alm, sub, make_user, make_payment, api):
    acc = make_user("acc", (alm, "accountant"))
    sub_acc = make_user("subacc", (sub, "accountant"))
    mine = services.submit(make_payment(acc, alm), acc)
    other = services.submit(make_payment(sub_acc, sub), sub_acc)
    c = api(acc)
    r = c.get("/api/v1/approvals/requests")
    assert r.status_code == 200
    assert [x["id"] for x in r.json()["results"]] == [mine.pk]
    assert c.get(f"/api/v1/approvals/requests/{other.pk}").status_code == 404
    assert post_action(c, other, "cancel").status_code == 404


def test_t2_submit_via_api_refused_403_for_viewer(alm, sub, make_user, make_payment, api):
    u = make_user("u", (alm, "viewer"), (sub, "accountant"))
    pay = make_payment(u, alm)
    r = api(u).post("/api/v1/approvals/requests", {"document_type": "payments.payment", "object_id": pay.pk}, format="json")
    assert r.status_code == 403
    assert r.json()["code"] == "permission_denied" and r.json()["message_ar"]


def test_submit_and_approve_via_api_with_decimal_strings(alm, mp, accountant, make_payment, api):
    pay = make_payment(accountant, alm, "1234.50")
    r = api(accountant).post("/api/v1/approvals/requests", {"document_type": "payments.payment", "object_id": pay.pk}, format="json")
    assert r.status_code == 201
    body = r.json()
    assert body["amount"] == "1234.50" and body["currency"] == "SAR"  # R22
    assert body["summary"]["en"] and body["summary"]["ar"]
    assert body["allowed_actions"] == ["cancel"]
    c = api(mp)
    detail = c.get(f"/api/v1/approvals/requests/{body['id']}").json()
    assert set(detail["allowed_actions"]) >= {"approve", "reject", "return"}
    inbox = c.get("/api/v1/approvals/requests?scope=mine").json()["results"]
    assert [x["id"] for x in inbox] == [body["id"]]
    resp = post_action(c, ApprovalRequest.objects.get(pk=body["id"]), "approve", {"document_version": detail["document_version"]})
    assert resp.status_code == 200 and resp.json()["status"] == "approved"


def test_t22_same_idempotency_key_same_response_one_action(alm, mp, accountant, make_payment, api):
    r = services.submit(make_payment(accountant, alm), accountant)
    c = api(mp)
    key = str(uuid.uuid4())
    first = post_action(c, r, "approve", {"document_version": r.document_version}, key=key)
    second = post_action(c, r, "approve", {"document_version": r.document_version}, key=key)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert ApprovalAction.objects.filter(request=r, action="approve").count() == 1


def test_idempotency_key_required_and_not_reusable_for_other_call(alm, mp, accountant, make_payment, api):
    r = services.submit(make_payment(accountant, alm), accountant)
    c = api(mp)
    resp = c.post(f"/api/v1/approvals/requests/{r.pk}/approve", {"document_version": r.document_version}, format="json")
    assert resp.status_code == 422 and resp.json()["code"] == "idempotency_key_required"
    key = str(uuid.uuid4())
    post_action(c, r, "reject", {"document_version": "stale", "comment": "x"}, key=key)
    resp = post_action(c, r, "approve", {"document_version": r.document_version}, key=key)
    assert resp.status_code == 422 and resp.json()["code"] == "idempotency_key_reused"


def test_stale_version_409(alm, mp, accountant, make_payment, api):
    r = services.submit(make_payment(accountant, alm), accountant)
    resp = post_action(api(mp), r, "approve", {"document_version": "0" * 64})
    assert resp.status_code == 409 and resp.json()["code"] == "stale_version"


def test_t12_api_returns_409_when_document_changed(alm, mp, accountant, make_payment, api):
    from testdocs.models import Payment

    pay = make_payment(accountant, alm)
    r = services.submit(pay, accountant)
    Payment.objects.filter(pk=pay.pk).update(counterparty="Changed Co")
    resp = post_action(api(mp), r, "approve", {"document_version": r.document_version})
    assert resp.status_code == 409 and resp.json()["code"] == "document_changed"
    r.refresh_from_db()
    assert r.status == "returned"


def test_t13_reject_without_comment_422_via_api(alm, mp, accountant, make_payment, api):
    r = services.submit(make_payment(accountant, alm), accountant)
    resp = post_action(api(mp), r, "reject", {"document_version": r.document_version})
    assert resp.status_code == 422 and resp.json()["code"] == "comment_required"
    r.refresh_from_db()
    assert r.status == "pending"


def test_t16_agent_approve_via_api_403(alm, agent, accountant, make_payment, api):
    r = services.submit(make_payment(accountant, alm), accountant)
    resp = post_action(api(agent), r, "approve", {"document_version": r.document_version})
    assert resp.status_code in (403, 404)
    r.refresh_from_db()
    assert r.status == "pending"


def test_d20_agent_cannot_submit_via_api_human_submits_and_agent_cannot_approve(alm, agent, accountant, make_bill,
                                                                                  api):
    bill = make_bill(agent, alm)
    c = api(agent)
    resp = c.post("/api/v1/approvals/requests", {"document_type": "payables.bill", "object_id": bill.pk}, format="json")
    assert resp.status_code == 403 and resp.json()["code"] == "service_user_cannot_submit"
    assert resp.json()["message_ar"] and not ApprovalRequest.objects.exists()
    resp = api(accountant).post("/api/v1/approvals/requests", {"document_type": "payables.bill", "object_id": bill.pk},
                                format="json")
    assert resp.status_code == 201 and resp.json()["source"] == "agent:invoice-reader"
    r = ApprovalRequest.objects.get(pk=resp.json()["id"])
    assert r.submitted_by == accountant and r.document_created_by == agent
    assert post_action(c, r, "approve", {"document_version": r.document_version}).status_code in (403, 404)
    assert ApprovalRequest.objects.get(pk=r.pk).status == "pending"


def test_t19_language_persists_and_messages_bilingual(alm, make_user, api):
    u = make_user("u", (alm, "viewer"))
    c = api(u)
    assert c.patch("/api/v1/me", {"language": "ar"}, format="json").json()["language"] == "ar"
    c2 = api(u)  # new sign-in
    me = c2.get("/api/v1/me").json()
    assert me["language"] == "ar" and me["name"]["ar"]
    assert me["entities"][0]["name"] == {"en": "Almiras", "ar": "المراس"}
    assert c2.patch("/api/v1/me", {"language": "fr"}, format="json").status_code == 422


def test_me_lists_only_entities_with_access(alm, sub, make_user, api):
    u = make_user("u", (sub, "accountant"))
    me = api(u).get("/api/v1/me").json()
    assert [e["code"] for e in me["entities"]] == ["LZM"]
    assert api(u).patch("/api/v1/me", {"default_entity": alm.pk}, format="json").status_code == 403


def test_filters_type_status_amount(alm, accountant, make_payment, make_bill, api):
    services.submit(make_payment(accountant, alm, "100"), accountant)
    services.submit(make_payment(accountant, alm, "9000"), accountant)
    services.submit(make_bill(accountant, alm, "500"), accountant)
    c = api(accountant)
    assert len(c.get("/api/v1/approvals/requests?type=payments.payment").json()["results"]) == 2
    assert len(c.get("/api/v1/approvals/requests?amount_min=400&amount_max=600").json()["results"]) == 1
    assert len(c.get("/api/v1/approvals/requests?status=approved").json()["results"]) == 0
    assert len(c.get("/api/v1/approvals/requests?scope=submitted&entity=ALM").json()["results"]) == 3


def test_summary_counts_and_totals(alm, mp, accountant, make_payment, api):
    services.submit(make_payment(accountant, alm, "100"), accountant)
    services.submit(make_payment(accountant, alm, "250.25"), accountant)
    s = api(mp).get("/api/v1/approvals/summary?entity=ALM").json()
    assert s["pending_for_me"] == 2 and s["totals"] == {"SAR": "350.25"}


def test_cancel_via_api(alm, accountant, make_payment, api):
    r = services.submit(make_payment(accountant, alm), accountant)
    resp = post_action(api(accountant), r, "cancel", {"comment": "duplicate"})
    assert resp.status_code == 200 and resp.json()["status"] == "cancelled"


def test_delegations_api_create_list_end(alm, mp, make_user, api):
    d = make_user("deputy", (alm, "viewer"))
    c = api(mp)
    now = timezone.now()
    resp = c.post("/api/v1/approvals/delegations", {
        "delegate": d.pk, "entity": alm.pk, "document_types": ["ledger.journal_entry"], "amount_max": "50000.00",
        "starts_at": now.isoformat(), "ends_at": (now + datetime.timedelta(days=5)).isoformat(),
    }, format="json")
    assert resp.status_code == 201 and resp.json()["amount_max"] == "50000.00"
    assert len(c.get("/api/v1/approvals/delegations").json()) == 1
    assert len(api(d).get("/api/v1/approvals/delegations").json()) == 1
    resp = c.patch(f"/api/v1/approvals/delegations/{resp.json()['id']}", {"end": True}, format="json")
    assert resp.status_code == 200


def test_delegation_for_someone_else_needs_manage_any(alm, chairman, mp, make_user, api):
    d = make_user("deputy", (alm, "viewer"))
    now = timezone.now()
    resp = api(chairman).post("/api/v1/approvals/delegations", {
        "delegator": mp.pk, "delegate": d.pk, "entity": alm.pk,
        "starts_at": now.isoformat(), "ends_at": (now + datetime.timedelta(days=1)).isoformat(),
    }, format="json")
    assert resp.status_code == 403


def test_policies_api_needs_policy_manage(alm, mp, accountant, api):
    assert api(accountant).get("/api/v1/approvals/policies").json() == []
    body = {"entity": alm.pk, "document_type": "ledger.journal_entry", "amount_min": "100000.00", "currency": "SAR",
            "priority": 5, "name_en": "Big entries", "name_ar": "قيود كبيرة", "valid_from": "2026-01-01",
            "steps": [{"sequence": 1, "approver_role": _role("chairman"), "min_approvals": 1}]}
    assert api(accountant).post("/api/v1/approvals/policies", body, format="json").status_code == 403
    resp = api(mp).post("/api/v1/approvals/policies", body, format="json")
    assert resp.status_code == 201 and resp.json()["amount_min"] == "100000.00"


def _role(code):
    from access.models import Role

    return Role.objects.get(code=code).pk


def test_notifications_list_and_read_own_only(alm, mp, accountant, make_payment, api):
    services.submit(make_payment(accountant, alm), accountant)
    c = api(mp)
    items = c.get("/api/v1/notifications?unread=1").json()
    assert len(items) == 1 and items[0]["title"]["ar"]
    assert c.post(f"/api/v1/notifications/{items[0]['id']}/read").status_code == 200
    assert c.get("/api/v1/notifications?unread=1").json() == []
    assert api(accountant).post(f"/api/v1/notifications/{items[0]['id']}/read").status_code == 404


def test_audit_api_needs_audit_view_and_is_entity_filtered(alm, sub, mp, accountant, make_user, make_payment, api):
    r = services.submit(make_payment(accountant, alm), accountant)
    sub_acc = make_user("subacc", (sub, "accountant"))
    services.submit(make_payment(sub_acc, sub), sub_acc)
    assert api(accountant).get("/api/v1/audit").status_code == 403
    rows = api(mp).get(f"/api/v1/audit?object_type=approvals.approvalrequest&object_id={r.pk}").json()
    assert rows and all(x["entity"] == "ALM" for x in rows)
    all_rows = api(mp).get("/api/v1/audit").json()
    assert not any(x["entity"] == "LZM" for x in all_rows)  # MP has no access to LZM (D12)


def test_self_approval_report_api(alm, mp, make_payment, api):
    r = services.submit(make_payment(mp, alm), mp)
    services.act(r, mp, "approve", document_version=r.document_version)
    now = timezone.localtime()
    rows = api(mp).get(f"/api/v1/approvals/self-approval-report?year={now.year}&month={now.month}").json()
    assert [x["request_id"] for x in rows] == [r.pk]


def test_devices_hook_accepts_registration(alm, mp, api):
    c = api(mp)
    assert c.post("/api/v1/devices", {"platform": "android", "token": "abc"}, format="json").status_code == 201
    assert c.delete("/api/v1/devices", {"token": "abc"}, format="json").status_code == 204


def test_unauthenticated_api_refused(db):
    from rest_framework.test import APIClient

    resp = APIClient().get("/api/v1/approvals/requests")
    assert resp.status_code in (401, 403)
    assert set(resp.json()) == {"code", "message_en", "message_ar"}
