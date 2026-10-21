"""Web slice 2 tests that commit for real (``transaction=True``): the refusal row on the separate access-log
connection (WT-6, access R27) and MFA state under PostgreSQL's ``ATOMIC_REQUESTS`` (security review 2026-10-21,
item 1). Kept apart because a module-wide ``usefixtures`` would build ``w`` before ``seeded_db`` reseeds the flushed
database; here the fixtures are requested in order."""
import pytest
from django.db import connection

from core.access.authority_models import ApprovalRequest
from core.access.models import AccessLog
from payroll.test_pg_review_2026_10_20c import seeded_db  # noqa: F401  (a fixture: reseeds after the flush)
from web import views
from web.conftest import draft_invoice, next_code, person
from web.models import MfaDevice


def request_of(document):
    return ApprovalRequest.objects.get(document_type=document._meta.label_lower, document_id=document.pk,
                                       status="open")


def approve(client, entity, req, code=None, version=None):
    data = {"version": req.document_version if version is None else version}
    if code is not None:
        data["code"] = code
    return client.post(f"/e/{entity}/inbox/{req.pk}/approve/", data)


def status_of(document):
    document.refresh_from_db()
    return document.status


def failures(user):
    return AccessLog.objects.filter(event="mfa_verify_failed", actor=user)


@pytest.mark.django_db(transaction=True, databases=["default", "access_log"])
def test_wt6_the_refused_self_approval_is_logged_even_though_nothing_changes(seeded_db, w, periods_now, signed_in):
    """The refusal row is written on the separate access-log connection, so it survives the rollback (access R27)."""
    maker = person("maker", (w.alm, "accountant"), (w.alm, "approver"))
    own = draft_invoice(w.alm, maker, w.customer, price="410.00", submit=True)
    client = signed_in(maker)
    assert approve(client, "ALM", request_of(own), code=next_code(maker)).status_code == 403
    denied = AccessLog.objects.filter(event="permission_denied", actor=maker).last()
    assert denied.details["reason"] == "self_approval" and status_of(own) == "submitted"



@pytest.mark.skipif(connection.vendor != "postgresql", reason="ATOMIC_REQUESTS is set in the PostgreSQL settings")
@pytest.mark.django_db(transaction=True, databases=["default", "access_log"])
def test_item1_postgresql_failures_and_used_steps_are_committed_despite_errors(seeded_db, w, periods_now, signed_in, mfa_clock,
                                                                          monkeypatch):
    assert connection.settings_dict["ATOMIC_REQUESTS"]
    client = signed_in(w.cfo)
    req = request_of(w.alm_invoice)
    for i in range(3):
        mfa_clock.tick(60)
        client.post(f"/e/ALM/inbox/{req.pk}/approve/", {"version": req.document_version, "code": "%06d" % i})
    assert MfaDevice.objects.get(user=w.cfo).failures == 3 and failures(w.cfo).count() >= 3
    mfa_clock.tick(60)
    code = next_code(w.cfo)
    monkeypatch.setattr(views.inbox_services, "decide", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    client.raise_request_exception = False
    assert client.post(f"/e/ALM/inbox/{req.pk}/approve/", {"version": req.document_version,
                                                           "code": code}).status_code == 500
    assert MfaDevice.objects.get(user=w.cfo).last_counter == int(mfa_clock.at.timestamp()) // 30
