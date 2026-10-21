"""MFA: TOTP enrolment, verification, recovery codes, throttle, step-up, reset and the admin gate (web.md R13 to R19,
R26; WT-8, WT-9, WT-10). Codes are computed from the device's secret at the test clock (web.conftest.Clock)."""
import re
from datetime import timedelta

import pyotp
import pytest
from django.contrib.auth import SESSION_KEY
from django.core.management import call_command
from django.db import connection
from django.test import Client

from core.access.models import AccessLog
from core.access.testing import make_user, member
from web import mfa
from web.conftest import PASSWORD, draft_invoice, enrol, next_code, person
from web.models import MfaDevice, RecoveryCode

pytestmark = pytest.mark.django_db


def login(client, user):
    response = client.post("/login/", {"username": user.username, "password": PASSWORD})
    assert response.status_code == 302
    return response


def events(name):
    return AccessLog.objects.filter(event=name)


def secret_of(user):
    return MfaDevice.objects.get(user=user).secret


# --- WT-8: an approver without a device must enrol; codes are throttled; approvals need a verified session ------

def test_wt8_approver_enrols_is_throttled_then_approves_and_an_unverified_approval_is_refused(w, client, mfa_clock,
                                                                                            periods_now):
    p = w.cfo
    login(client, p)
    response = client.get("/e/ALM/inbox/")
    assert response.status_code == 302 and response["Location"].startswith("/mfa/enrol/")  # inbox unreachable (R14)
    page = client.get(response["Location"])
    assert page.status_code == 200 and "web/mfa_enrol.html" in [t.name for t in page.templates]
    assert client.post("/mfa/enrol/").status_code == 302  # start: a new secret on an unconfirmed device
    page = client.get("/mfa/enrol/").content.decode()
    assert 'src="data:image/svg+xml' in page  # QR drawn on the server (R16), no outside service
    assert not re.search(r"""src=["']https?://""", page)
    confirm = client.post("/mfa/enrol/confirm/", {"code": next_code(p)})
    assert confirm.status_code == 200
    codes = re.findall(r"<code>([a-z0-9]{5}-[a-z0-9]{5})</code>", confirm.content.decode())
    assert len(codes) == 10 and RecoveryCode.objects.filter(user=p, used_at__isnull=True).count() == 10
    assert events("mfa_enrolled").filter(actor=p).count() == 1
    assert client.get("/e/ALM/inbox/").status_code == 200  # enrolment verified the session

    client.post("/logout/")
    login(client, p)
    assert client.get("/e/ALM/").status_code == 302  # a new session needs a code again (R14)
    first = client.post("/mfa/verify/", {"code": "000000"})
    assert first.status_code == 400
    assert client.post("/mfa/verify/", {"code": "111111"}).status_code == 429  # within the wait: throttled (R18)
    mfa_clock.tick(2)
    assert client.post("/mfa/verify/", {"code": "222222"}).status_code == 400
    right = pyotp.TOTP(secret_of(p)).at(mfa_clock.at + timedelta(seconds=30))  # next step: unused, within drift
    assert client.post("/mfa/verify/", {"code": right}).status_code == 429  # even a right code waits
    failed = events("mfa_verify_failed").filter(actor=p)
    assert failed.count() == 4 and {r.details["reason"] for r in failed} == {"wrong_code", "throttled"}
    assert all(not re.search(r"\b\d{6}\b", str(r.details)) for r in failed)  # never the code (R26)
    mfa_clock.tick(5)
    assert client.post("/mfa/verify/", {"code": right}).status_code == 302
    assert client.get("/e/ALM/inbox/").status_code == 200

    req = _request_of(w.alm_invoice)
    mfa_clock.tick(30)  # the sign-in code's step is used up: the step-up needs a later one
    approved = client.post(f"/e/ALM/inbox/{req.pk}/approve/", {"version": req.document_version, "code": next_code(p)})
    assert approved.status_code == 302
    w.alm_invoice.refresh_from_db()
    assert w.alm_invoice.status != "submitted"

    second = draft_invoice(w.alm, w.clerk, w.customer, price="80.00", submit=True)
    req2 = _request_of(second)
    session = client.session
    del session[mfa.VERIFIED_KEY]  # the session's OTP record removed
    session.save()
    refused = client.post(f"/e/ALM/inbox/{req2.pk}/approve/", {"version": req2.document_version,
                                                                "code": next_code(p)})
    assert refused.status_code == 302 and refused["Location"].startswith("/mfa/verify/")  # R15
    second.refresh_from_db()
    assert second.status == "submitted"


def _request_of(document):
    from core.access.authority_models import ApprovalRequest

    return ApprovalRequest.objects.get(document_type=document._meta.label_lower, document_id=document.pk,
                                       status="open")


def test_r15_views_refuse_an_unverified_decision_even_behind_the_gate(w, rf):
    """Defence in depth: the approve view checks the session itself (R15), not only the middleware."""
    from django.contrib.sessions.backends.db import SessionStore

    from web import views

    enrol(w.cfo)
    req = _request_of(w.alm_invoice)
    request = rf.post(f"/e/ALM/inbox/{req.pk}/approve/", {"version": req.document_version, "code": "1"})
    request.user, request.session, request.web_lang = w.cfo, SessionStore(), "en"
    request._dont_enforce_csrf_checks = True
    response = views.inbox_approve(request, "ALM", req.pk)
    assert response.status_code == 403
    w.alm_invoice.refresh_from_db()
    assert w.alm_invoice.status == "submitted"


# --- WT-9: MFA follows the memberships, checked at every request ----------------------------------------------

def test_wt9_a_clerk_needs_no_mfa_until_granted_an_mfa_role(w, signed_in):
    client = signed_in(w.clerk)
    assert client.get("/e/ALM/").status_code == 200  # no MFA role, no device: password only (R13)
    member(w.clerk, w.sub, "approver")
    response = client.get("/e/ALM/")
    assert response.status_code == 302 and response["Location"].startswith("/mfa/enrol/")  # next request (R14)


@pytest.mark.parametrize("role", sorted(mfa.web_services.MFA_ROLES))
def test_r13_every_mfa_role_and_superusers_need_mfa(w, role):
    user = make_user(f"needs_{role}")
    entity = w.alm
    member(user, entity, role)
    assert mfa.requires_mfa(user)
    assert mfa.requires_mfa(w.su) and not mfa.requires_mfa(w.acc) and not mfa.requires_mfa(w.clerk)


def test_r13_mfa_switched_off_in_settings_opens_no_gate(w, client, settings):
    settings.WEB_MFA_ENFORCED = False
    login(client, w.cfo)
    assert client.get("/e/ALM/").status_code == 200


def test_r14_until_verified_only_mfa_pages_language_and_sign_out(w, client):
    enrol(w.cfo)
    login(client, w.cfo)
    assert client.get("/mfa/verify/").status_code == 200
    switched = client.post("/switch-entity/", {"entity": "ALM"})
    assert switched.status_code == 302 and switched["Location"] == "/mfa/verify/"  # a POST is not carried out
    from web.models import UiPreference

    assert not UiPreference.objects.filter(user=w.cfo).exists()
    assert client.post("/language/", {"language": "en", "next": "/mfa/verify/"}).status_code == 302
    assert client.post("/logout/").status_code == 302
    assert SESSION_KEY not in client.session


def test_r17_session_key_rotates_at_verification(w, client):
    enrol(w.cfo)
    login(client, w.cfo)
    before = client.session.session_key
    assert client.post("/mfa/verify/", {"code": next_code(w.cfo)}).status_code == 302
    assert client.session.session_key != before
    assert client.session[mfa.VERIFIED_KEY] == MfaDevice.objects.get(user=w.cfo).pk


def test_verification_goes_back_to_the_page_asked_for_but_never_off_site(w, client):
    enrol(w.cfo)
    login(client, w.cfo)
    gate = client.get("/e/ALM/inbox/")
    assert gate["Location"] == "/mfa/verify/?next=%2Fe%2FALM%2Finbox%2F"
    done = client.post("/mfa/verify/", {"code": next_code(w.cfo), "next": "/e/ALM/inbox/"})
    assert done["Location"] == "/e/ALM/inbox/"
    client.post("/logout/")
    login(client, w.cfo)
    done = client.post("/mfa/verify/", {"code": next_code(w.cfo), "next": "https://evil.example/"})
    assert done["Location"] == "/"


# --- R16: TOTP, the secret at rest, recovery codes ---------------------------------------------------------------

def test_r16_totp_is_rfc6238_with_one_step_of_drift_and_each_code_works_once(w, mfa_clock):
    enrol(w.cfo)
    totp = pyotp.TOTP(secret_of(w.cfo))
    mfa_clock.tick(60)
    now_code = totp.at(mfa_clock.at)
    assert mfa.verify(w.cfo, now_code)
    with pytest.raises(mfa.MfaError) as replay:  # the same step again: refused (no replay)
        mfa.verify(w.cfo, now_code)
    assert replay.value.code == "wrong_code"
    mfa_clock.tick(10)  # wait out the throttle
    assert mfa.verify(w.cfo, totp.at(mfa_clock.at + timedelta(seconds=30)))  # one step ahead (drift)
    mfa_clock.tick(120)
    with pytest.raises(mfa.MfaError):
        mfa.verify(w.cfo, totp.at(mfa_clock.at - timedelta(seconds=120)))  # four steps old: refused


def test_r33_codes_may_be_typed_with_arabic_indic_digits(w, mfa_clock):
    enrol(w.cfo)
    code = next_code(w.cfo)
    arabic = code.translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩"))
    assert mfa.verify(w.cfo, f" {arabic[:3]} {arabic[3:]} ")


def test_r16_the_secret_is_encrypted_at_rest(w):
    enrol(w.cfo)
    secret = secret_of(w.cfo)
    with connection.cursor() as cursor:
        cursor.execute("SELECT secret FROM web_mfadevice WHERE user_id = %s", [w.cfo.pk])
        (raw,) = cursor.fetchone()
    assert raw.startswith("wenc1:") and secret not in raw and len(secret) == 32


def test_r16_recovery_codes_are_hashed_single_use_and_not_a_step_up(w, client):
    codes = enrol(w.cfo)
    stored = list(RecoveryCode.objects.filter(user=w.cfo).values_list("code_hash", flat=True))
    assert len(stored) == 10 and not set(stored) & {c.replace("-", "") for c in codes} and \
        not set(stored) & set(codes)
    login(client, w.cfo)
    used = client.post("/mfa/verify/", {"recovery_code": codes[0].upper()})
    assert used.status_code == 302 and client.get("/e/ALM/").status_code == 200
    assert events("mfa_recovery_code_used").filter(actor=w.cfo).get().details["remaining"] == 9
    client.post("/logout/")
    login(client, w.cfo)
    assert client.post("/mfa/verify/", {"recovery_code": codes[0]}).status_code == 400  # used once only
    with pytest.raises(mfa.MfaError):
        mfa.verify(w.cfo, codes[1], purpose="step_up")  # a recovery code is not an authenticator code
    for row in AccessLog.objects.all():
        assert not any(c in str(row.details) or c.replace("-", "") in str(row.details) for c in codes)


def test_r16_new_recovery_codes_replace_the_old_ones(w):
    old = enrol(w.cfo)
    new = mfa.renew_recovery_codes(w.cfo, next_code(w.cfo))
    assert len(new) == 10 and not set(new) & set(old)
    with pytest.raises(mfa.MfaError):
        mfa.use_recovery_code(w.cfo, old[0])
    assert events("mfa_recovery_codes_renewed").count() == 1


def test_r16_security_page_renews_codes_with_a_code(w, signed_in):
    client = signed_in(w.cfo)
    assert client.get("/mfa/").status_code == 200
    renewed = client.post("/mfa/", {"code": next_code(w.cfo)})
    assert renewed.status_code == 200 and len(re.findall(r"<code>", renewed.content.decode())) == 10


def verified(actor):
    """A verified session of ``actor`` (enrolled here if needed), as a session mapping."""
    if mfa.device_of(actor) is None:
        enrol(actor)
    return {mfa.VERIFIED_KEY: MfaDevice.objects.get(user=actor).pk}


def reset(actor, user, reason="Phone lost, ticket 7", **kwargs):
    session = kwargs.pop("session", None)
    session = verified(actor) if session is None else session
    return mfa.reset_device(actor, user, reason=reason, session=session, code=kwargs.pop("code", None)
                            or next_code(actor))


def test_r16_an_enrolled_user_cannot_enrol_again_or_reset_themselves(w, client):
    enrol(w.cfo)
    with pytest.raises(mfa.MfaError) as exc:
        mfa.start_enrolment(w.cfo)
    assert exc.value.code == "already_enrolled"
    login(client, w.cfo)
    assert client.post("/mfa/enrol/")["Location"].startswith("/mfa/verify/")
    with pytest.raises(mfa.MfaError) as own:
        reset(w.cfo, w.cfo, reason="lost phone")
    assert own.value.code == "not_allowed"


@pytest.fixture
def group_mp(w):
    return person("mp", (w.alm, "managing_partner"))


def test_r16_group_managing_partner_resets_an_approver_with_a_verified_session_and_a_code_logged(w, group_mp):
    enrol(w.cfo)
    with pytest.raises(mfa.MfaError) as no_reason:
        reset(group_mp, w.cfo, reason=" ")
    assert no_reason.value.code == "reason_required"
    with pytest.raises(mfa.MfaError) as unverified:
        reset(group_mp, w.cfo, session={})  # review item 2: the actor's own session must be verified
    assert unverified.value.code == "session_not_verified"
    with pytest.raises(mfa.MfaError) as wrong:
        reset(group_mp, w.cfo, code="000000")  # and a fresh code (step-up)
    assert wrong.value.code == "wrong_code" and MfaDevice.objects.filter(user=w.cfo).exists()
    _clock_wait()
    assert reset(group_mp, w.cfo)
    row = events("mfa_reset").get()
    assert row.actor == group_mp and row.subject_user == w.cfo and row.details["reason"] == "Phone lost, ticket 7"
    assert not MfaDevice.objects.filter(user=w.cfo).exists() and not RecoveryCode.objects.filter(user=w.cfo).exists()
    assert AccessLog.objects.filter(event="mfa_verified", actor=group_mp, details__purpose="reset").exists()
    with pytest.raises(mfa.MfaError):
        reset(group_mp, w.su, reason="superusers only by the operator command")


def _clock_wait():
    from web.conftest import _CLOCK

    _CLOCK[0].tick(60)


def test_r16_access_admins_reset_only_people_without_mfa_roles_wholly_in_their_entities(w, group_mp):
    admin = person("alm_admin", (w.alm, "access_admin"))
    hr = w.hr  # hr_officer in ALM: needs MFA through an approval right (OQ-29) but holds no MFA role
    enrol(hr)
    assert not mfa.may_reset(admin, w.cfo)  # approver: an MFA role (review item 2)
    assert not mfa.may_reset(person("sub_admin", (w.sub, "access_admin")), hr)  # another entity's admin
    assert mfa.may_reset(admin, hr)
    member(hr, w.sub, "viewer")
    assert not mfa.may_reset(admin, hr)  # a membership where the actor is not access admin
    assert mfa.may_reset(group_mp, hr) and mfa.may_reset(group_mp, w.cfo)


def test_r16_managing_partner_and_chairman_are_reset_only_by_the_group_managing_partner(w, group_mp):
    admin = person("alm_admin", (w.alm, "access_admin"))
    chairman = person("chair", (w.alm, "chairman"))
    other_mp = person("mp2", (w.alm, "managing_partner"))
    assert not mfa.may_reset(admin, chairman) and not mfa.may_reset(admin, other_mp)
    assert mfa.may_reset(group_mp, chairman) and mfa.may_reset(group_mp, other_mp)


def test_r16_a_reset_ends_the_verified_session_at_the_next_request(w, signed_in, group_mp):
    client = signed_in(w.cfo)
    assert client.get("/e/ALM/").status_code == 200
    reset(group_mp, w.cfo, reason="lost")
    response = client.get("/e/ALM/")
    assert response.status_code == 302 and response["Location"].startswith("/mfa/enrol/")


def test_r16_operator_command_resets_a_superuser_with_a_reason(w):
    enrol(w.su)
    call_command("web_mfa_reset", "su", "--reason", "Lost token, ticket 9")
    assert not MfaDevice.objects.filter(user=w.su).exists()
    row = events("mfa_reset").get()
    assert row.actor is None and row.source == "command:web_mfa_reset" and row.details["reason"]


# --- R18: lockout of codes ---------------------------------------------------------------------------------------

def test_r18_five_wrong_codes_lock_the_device_for_15_minutes(w, client, mfa_clock):
    enrol(w.cfo)
    for _ in range(5):
        mfa_clock.tick(60)
        with pytest.raises(mfa.MfaError):
            mfa.verify(w.cfo, "123456")
    device = MfaDevice.objects.get(user=w.cfo)
    assert device.failures == 5 and device.locked_until is not None
    login(client, w.cfo)
    locked = client.post("/mfa/verify/", {"code": next_code(w.cfo)})
    assert locked.status_code == 429 and "15" in locked.content.decode()  # right code, still locked
    mfa_clock.tick(15 * 60)
    assert client.post("/mfa/verify/", {"code": next_code(w.cfo)}).status_code == 302
    assert MfaDevice.objects.get(user=w.cfo).failures == 0


def test_r18_wrong_step_up_codes_count_towards_the_lock(w, mfa_clock):
    enrol(w.cfo)
    for _ in range(5):
        mfa_clock.tick(60)
        with pytest.raises(mfa.MfaError):
            mfa.verify(w.cfo, "999999", purpose="step_up")
    assert MfaDevice.objects.get(user=w.cfo).locked_until is not None
    assert {r.details["purpose"] for r in events("mfa_verify_failed")} == {"step_up"}


# --- WT-10 / R19: Django admin -----------------------------------------------------------------------------------

def test_wt10_admin_needs_a_verified_superuser_and_refuses_other_staff(w, settings, signed_in):
    settings.WEB_ADMIN_REQUIRE_MFA = True
    anonymous = Client()
    to_login = anonymous.get("/admin/")
    assert to_login.status_code == 302 and to_login["Location"].startswith("/login/?next=")
    assert anonymous.get("/admin/login/")["Location"].startswith("/login/")  # admin's own sign-in is not used

    client = signed_in(w.su)  # verified superuser
    assert client.get("/admin/").status_code == 200
    session = client.session
    del session[mfa.VERIFIED_KEY]
    session.save()
    unverified = client.get("/admin/")
    assert unverified.status_code == 302 and unverified["Location"].startswith("/mfa/verify/")

    staff = person("staff_t")
    staff.is_staff = True
    staff.save(update_fields=["is_staff"])
    other = Client()
    login(other, staff)
    assert other.get("/admin/")["Location"].startswith("/mfa/enrol/")  # staff need a verified session too
    enrol(staff)
    assert other.post("/mfa/verify/", {"code": next_code(staff)}).status_code == 302
    settings.WEB_ADMIN_SUPERUSERS_ONLY = True  # OQ-10: once the membership screens exist
    refused = other.get("/admin/")
    assert refused.status_code == 403 and "غير مسموح" in refused.content.decode()

    plain = Client()
    login(plain, w.acc)
    assert plain.get("/admin/").status_code == 403  # not staff at all


def test_r26_mfa_logs_hold_no_secret(w, signed_in):
    signed_in(w.cfo)
    secret = secret_of(w.cfo)
    rows = AccessLog.objects.filter(event__startswith="mfa_")
    assert {"mfa_enrolled", "mfa_verified"} <= set(rows.values_list("event", flat=True))
    assert all(secret not in str(r.details) for r in rows)
