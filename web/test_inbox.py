"""The approval inbox (web.md R5 to R8, R15, R21, R23, R24, R39, R40; WT-6, WT-21, WT-22, WT-25; access §10 R61, R62,
R64, R79). Every decision goes through ``core.access.inbox.decide`` and the engine; the views never touch models."""
from datetime import timedelta

import pytest
from django.test import Client

from core.access import delegations
from core.access import inbox as inbox_services
from core.access import services as access
from core.access.authority_models import ApprovalDecision, ApprovalRequest
from core.access.models import AccessLog
from core.access.testing import make_agent
from web.conftest import draft_invoice, next_code, person
from web.models import MfaDevice

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("periods_now")]


def request_of(document, status="open"):
    return ApprovalRequest.objects.get(document_type=document._meta.label_lower, document_id=document.pk,
                                       status=status)


def approve(client, entity, req, code=None, version=None):
    data = {"version": req.document_version if version is None else version}
    if code is not None:
        data["code"] = code
    return client.post(f"/e/{entity}/inbox/{req.pk}/approve/", data)


def status_of(document):
    document.refresh_from_db()
    return document.status


# --- R39 / WT-22: per entity, only what the user may decide; other entities invisible -----------------------------

def test_wt22_inbox_lists_this_entitys_requests_the_user_may_decide_and_nothing_else(w, signed_in):
    sub_invoice = draft_invoice(w.sub, w.sub_clerk, w.sub_customer, price="333.00", submit=True)
    alm_req, sub_req = request_of(w.alm_invoice), request_of(sub_invoice)
    client = signed_in(w.cfo)
    page = client.get("/e/ALM/inbox/")
    body = page.content.decode()
    assert page.status_code == 200 and f"/e/ALM/inbox/{alm_req.pk}/" in body
    assert f"/inbox/{sub_req.pk}/" not in body and "SUB-ONLY-SECRET" not in body

    sub_cfo = person("sub_cfo", (w.sub, "approver"))
    other = Client()
    from web.conftest import enrol

    enrol(sub_cfo)
    other.post("/login/", {"username": "sub_cfo", "password": "made-up-test-password-7Q!"})
    other.post("/mfa/verify/", {"code": next_code(sub_cfo)})
    body = other.get("/e/SUB/inbox/").content.decode()
    assert f"/e/SUB/inbox/{sub_req.pk}/" in body and f"/inbox/{alm_req.pk}/" not in body
    assert other.get("/e/ALM/inbox/").status_code == 404  # ALM is not one of sub_cfo's entities (R4)
    assert other.get(f"/e/SUB/inbox/{alm_req.pk}/").status_code == 404  # another entity's request (R3)
    forged = approve(other, "SUB", alm_req, code=next_code(sub_cfo))
    assert forged.status_code == 404 and status_of(w.alm_invoice) == "submitted"


def test_r64_a_group_parent_role_decides_nothing_in_a_subsidiary(w, signed_in):
    """No cascade for approvals (access R64): the managing partner on ALM sees SUB's pages but not its approvals."""
    draft_invoice(w.sub, w.sub_clerk, w.sub_customer, price="333.00", submit=True)
    mp = person("mp", (w.alm, "managing_partner"))
    client = signed_in(mp)
    assert f"/e/ALM/inbox/{request_of(w.alm_invoice).pk}/" in client.get("/e/ALM/inbox/").content.decode()
    sub_invoice_req = ApprovalRequest.objects.get(entity=w.sub, status="open")
    sub_page = client.get("/e/SUB/inbox/")
    assert sub_page.status_code == 200 and f"/e/SUB/inbox/{sub_invoice_req.pk}/" not in sub_page.content.decode()


def test_viewers_see_an_empty_inbox_and_no_request(w, signed_in):
    client = signed_in(w.viewer)
    page = client.get("/e/ALM/inbox/")
    assert page.status_code == 200 and f"/inbox/{request_of(w.alm_invoice).pk}/" not in page.content.decode()
    assert client.get(f"/e/ALM/inbox/{request_of(w.alm_invoice).pk}/").status_code == 404


def test_agents_and_system_users_have_no_inbox(w):
    assert inbox_services.pending(make_agent("inbox_probe"), w.alm) == []
    assert inbox_services.pending(access.system_user("billing"), w.alm) == []


# --- WT-6 / R79: own documents are not approvable; the engine refuses a forged POST ------------------------------

def test_wt6_own_document_is_not_listed_shown_read_only_and_a_forged_approval_is_refused(w, signed_in):
    maker = person("maker", (w.alm, "accountant"), (w.alm, "approver"))
    own = draft_invoice(w.alm, maker, w.customer, price="410.00", submit=True)
    req = request_of(own)
    client = signed_in(maker)
    listing = client.get("/e/ALM/inbox/").content.decode()
    assert f"/inbox/{req.pk}/" not in listing and f"/inbox/{request_of(w.alm_invoice).pk}/" in listing
    detail = client.get(f"/e/ALM/inbox/{req.pk}/")
    body = detail.content.decode()
    assert detail.status_code == 200 and f"/inbox/{req.pk}/approve/" not in body  # no approve action (R40)
    forged = approve(client, "ALM", req, code=next_code(maker))
    assert forged.status_code == 403 and "cannot approve" in forged.content.decode()
    assert status_of(own) == "submitted"
    other = signed_in(w.cfo)
    assert approve(other, "ALM", req, code=next_code(w.cfo)).status_code == 302
    assert status_of(own) != "submitted"


# --- R15: approving needs a fresh code (step-up) ---------------------------------------------------------------

def test_r15_approval_needs_a_fresh_authenticator_code(w, signed_in):
    client = signed_in(w.cfo)
    req = request_of(w.alm_invoice)
    assert approve(client, "ALM", req).status_code == 400  # no code
    assert approve(client, "ALM", req, code="000000").status_code == 400  # wrong code
    assert status_of(w.alm_invoice) == "submitted"
    done = approve(client, "ALM", req, code=next_code(w.cfo))
    assert done.status_code == 302 and done["Location"] == "/e/ALM/inbox/"
    assert status_of(w.alm_invoice) != "submitted"
    assert AccessLog.objects.filter(event="mfa_verified", actor=w.cfo, details__purpose="step_up").count() == 1
    assert "تم الاعتماد." in client.get("/e/ALM/inbox/").content.decode()  # the message, in the user's language
    assert request_of(w.alm_invoice, status="approved").decisions.get().user == w.cfo


# --- R62: the delegate path --------------------------------------------------------------------------------------

def test_a_delegate_sees_and_approves_on_behalf_of_the_delegator(w, signed_in):
    delegations.delegate_authority(w.cfo, entity=w.alm, delegate=w.acc, valid_to=access.now() + timedelta(days=10),
                                   reason_en="Annual leave", reason_ar="إجازة سنوية")
    req = request_of(w.alm_invoice)
    client = signed_in(w.acc)
    listing = client.get("/e/ALM/inbox/").content.decode()
    assert f"/e/ALM/inbox/{req.pk}/" in listing
    assert ("On behalf of" in listing) or ("نيابة عن" in listing)
    assert approve(client, "ALM", req).status_code == 400  # a delegate gives a fresh code (verification item 1)
    assert status_of(w.alm_invoice) == "submitted"
    assert MfaDevice.objects.filter(user=w.acc, confirmed=True).exists()  # the delegation made MFA required
    done = approve(client, "ALM", req, code=next_code(w.acc))
    assert done.status_code == 302 and status_of(w.alm_invoice) != "submitted"
    assert AccessLog.objects.filter(event="mfa_verified", actor=w.acc, details__purpose="step_up").exists()
    decision = request_of(w.alm_invoice, status="approved").decisions.get()
    assert decision.user == w.acc and decision.on_behalf_of == w.cfo and decision.delegation is not None


def test_without_a_delegation_the_accountant_sees_nothing_to_decide(w, signed_in):
    client = signed_in(w.acc)
    req = request_of(w.alm_invoice)
    assert f"/inbox/{req.pk}/" not in client.get("/e/ALM/inbox/").content.decode()
    assert approve(client, "ALM", req).status_code in (403, 404)
    assert status_of(w.alm_invoice) == "submitted"


# --- R8 / WT-21: send back with a reason (Arabic required, English optional) ------------------------------------

def test_wt21_return_needs_an_arabic_reason_and_stores_both_languages(w, signed_in):
    client = signed_in(w.cfo)
    req = request_of(w.alm_invoice)
    url = f"/e/ALM/inbox/{req.pk}/send-back/"
    empty = client.post(url, {"version": req.document_version, "reason_ar": "", "reason_en": "Fix the quantity"})
    assert empty.status_code == 400 and status_of(w.alm_invoice) == "submitted"
    done = client.post(url, {"version": req.document_version, "reason_ar": "يرجى تصحيح الكمية",
                             "reason_en": "Fix the quantity"})
    assert done.status_code == 302 and status_of(w.alm_invoice) == "draft"
    decision = ApprovalDecision.objects.get(request=req, decision="returned")
    assert decision.note == "يرجى تصحيح الكمية" and decision.note_en == "Fix the quantity" and decision.user == w.cfo
    history = client.get(f"/e/ALM/inbox/{req.pk}/").content.decode()  # closed: history only, no actions
    assert "يرجى تصحيح الكمية" in history and "Fix the quantity" in history
    assert f"/inbox/{req.pk}/approve/" not in history


def test_r8_the_creator_cannot_send_back_their_own_document_from_the_inbox(w, signed_in):
    client = signed_in(w.clerk)
    req = request_of(w.alm_invoice)
    response = client.post(f"/e/ALM/inbox/{req.pk}/send-back/", {"version": req.document_version,
                                                                  "reason_ar": "سحب"})
    assert response.status_code in (403, 404) and status_of(w.alm_invoice) == "submitted"


# --- R7 / WT-25: the version shown is the version decided -----------------------------------------------------

def test_wt25_a_changed_document_is_refused_reload_and_review_again(w, signed_in):
    client = signed_in(w.cfo)
    req = request_of(w.alm_invoice)
    stale = approve(client, "ALM", req, code=next_code(w.cfo), version="1999")
    assert stale.status_code == 409 and ("Reload" in stale.content.decode() or "أعد تحميله" in stale.content.decode())
    assert status_of(w.alm_invoice) == "submitted"
    assert approve(client, "ALM", req, code=next_code(w.cfo)).status_code == 302


# --- R5, R21, R23, R24: POST only, CSRF, no writes on GET, no personal data -----------------------------------

def test_r5_r21_decisions_are_csrf_protected_posts(w, signed_in):
    client = signed_in(w.cfo)
    req = request_of(w.alm_invoice)
    assert client.get(f"/e/ALM/inbox/{req.pk}/approve/").status_code == 405
    assert client.get(f"/e/ALM/inbox/{req.pk}/send-back/").status_code == 405
    strict = Client(enforce_csrf_checks=True)
    strict.cookies = client.cookies
    assert approve(strict, "ALM", req, code=next_code(w.cfo)).status_code == 403
    assert status_of(w.alm_invoice) == "submitted"


def test_r5_opening_the_inbox_writes_nothing(w, signed_in):
    client = signed_in(w.cfo)
    req = request_of(w.alm_invoice)
    before = (AccessLog.objects.count(), ApprovalRequest.objects.count(), ApprovalDecision.objects.count())
    assert client.get("/e/ALM/inbox/").status_code == 200
    assert client.get(f"/e/ALM/inbox/{req.pk}/").status_code == 200
    assert (AccessLog.objects.count(), ApprovalRequest.objects.count(), ApprovalDecision.objects.count()) == before


def test_r24_the_inbox_shows_no_personal_data_of_an_individual_buyer(w, signed_in):
    from invoicing import services as invoicing

    invoicing.submit_sales_document(w.alm_person_invoice, user=w.clerk)
    req = request_of(w.alm_person_invoice)
    client = signed_in(w.cfo)
    for url in ("/e/ALM/inbox/", f"/e/ALM/inbox/{req.pk}/"):
        body = client.get(url).content.decode()
        assert f"/inbox/{req.pk}/" in body or url.endswith(f"{req.pk}/")
        assert "Made Up Person" not in body and "شخص مختلق" not in body and "Buyer Street" not in body


def test_r28_inbox_pages_render_in_both_languages(w, signed_in):
    client = signed_in(w.cfo)
    req = request_of(w.alm_invoice)
    arabic = client.get(f"/e/ALM/inbox/{req.pk}/").content.decode()
    assert '<html lang="ar" dir="rtl">' in arabic and "صندوق الاعتمادات" in arabic
    client.post("/language/", {"language": "en"})
    english = client.get(f"/e/ALM/inbox/{req.pk}/").content.decode()
    assert '<html lang="en" dir="ltr">' in english and "Approval inbox" in english
