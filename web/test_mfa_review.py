"""Fixes of the independent security review of web slice 2 (2026-10-21): MFA state outside the request transaction,
escalating locks with alerts, fail-closed encryption and key rotation, inbox versions and reasons, enrolment expiry,
DEBUG off by default, and MFA for every approval-right holder (OQ-29)."""
from datetime import timedelta

import pyotp
import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import connection
from django.test import Client

from core.access import engine
from core.access import inbox as inbox_services
from core.access.authority_models import AccessAlert, ApprovalRequest
from core.access.models import AccessLog
from core.access.testing import make_user, member
from web import crypto, mfa, views
from payroll.test_pg_review_2026_10_20c import seeded_db  # noqa: F401  (a fixture: reseeds after the flush)
from web.conftest import PASSWORD, enrol, next_code, person
from web.models import MfaDevice, RecoveryCode

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("periods_now")]


def request_of(document):
    return ApprovalRequest.objects.get(document_type=document._meta.label_lower, document_id=document.pk,
                                       status="open")


def failures(user):
    return AccessLog.objects.filter(event="mfa_verify_failed", actor=user)


# --- item 1: code checks survive the request transaction --------------------------------------------------------

CODE_VIEWS = ("mfa_enrol_confirm", "mfa_verify", "mfa_security", "inbox_approve", "inbox_send_back")


def test_item1_every_view_that_checks_a_code_runs_outside_the_request_transaction():
    for name in CODE_VIEWS:
        assert "default" in getattr(getattr(views, name), "_non_atomic_requests", set()), name


@pytest.fixture
def atomic_requests(monkeypatch):
    """ATOMIC_REQUESTS as in production (PostgreSQL settings), on the test database."""
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", True)


def test_item1_an_unknown_request_is_404_before_any_code_is_checked(w, signed_in, atomic_requests):
    client = signed_in(w.cfo)
    before = failures(w.cfo).count()
    for i in range(6):
        assert client.post("/e/ALM/inbox/999999/approve/", {"version": "1", "code": "%06d" % i}).status_code == 404
    assert failures(w.cfo).count() == before  # nothing to guess against: the code is not even read
    assert MfaDevice.objects.get(user=w.cfo).failures == 0


def test_item1_wrong_step_up_codes_are_counted_locked_and_logged_under_atomic_requests(w, signed_in, mfa_clock,
                                                                                       atomic_requests):
    client = signed_in(w.cfo)
    req = request_of(w.alm_invoice)
    for i in range(5):
        mfa_clock.tick(60)
        client.post(f"/e/ALM/inbox/{req.pk}/approve/", {"version": req.document_version, "code": "%06d" % i})
    device = MfaDevice.objects.get(user=w.cfo)
    assert device.failures == 5 and device.locked_until is not None
    assert failures(w.cfo).filter(details__purpose="step_up").count() == 5
    w.alm_invoice.refresh_from_db()
    assert w.alm_invoice.status == "submitted"


def test_item1_a_used_code_stays_used_when_the_decision_then_fails(w, signed_in, mfa_clock, monkeypatch,
                                                                   atomic_requests):
    client = signed_in(w.cfo)
    req = request_of(w.alm_invoice)
    code = next_code(w.cfo)

    def boom(*args, **kwargs):
        raise RuntimeError("module failure after the step-up")

    original = views.inbox_services.decide
    monkeypatch.setattr(views.inbox_services, "decide", boom)
    client.raise_request_exception = False
    assert client.post(f"/e/ALM/inbox/{req.pk}/approve/", {"version": req.document_version,
                                                           "code": code}).status_code == 500
    step = int(mfa_clock.at.timestamp()) // 30
    assert MfaDevice.objects.get(user=w.cfo).last_counter == step  # not rolled back: the code cannot be replayed
    assert AccessLog.objects.filter(event="mfa_verified", actor=w.cfo, details__purpose="step_up").exists()
    monkeypatch.setattr(views.inbox_services, "decide", original)
    replay = client.post(f"/e/ALM/inbox/{req.pk}/approve/", {"version": req.document_version, "code": code})
    assert replay.status_code == 400
    w.alm_invoice.refresh_from_db()
    assert w.alm_invoice.status == "submitted"


@pytest.mark.skipif(connection.vendor != "postgresql", reason="ATOMIC_REQUESTS is set in the PostgreSQL settings")
@pytest.mark.django_db(transaction=True, databases=["default", "access_log"])
def test_item1_postgresql_failures_and_used_steps_are_committed_despite_errors(seeded_db, w, signed_in, mfa_clock,
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


# --- item 3: escalating locks, alert on the second -------------------------------------------------------------

def _lock(user, clock):
    for _ in range(5):
        clock.tick(60)
        with pytest.raises(mfa.MfaError):
            mfa.verify(user, "123456")
    return MfaDevice.objects.get(user=user)


def test_item3_each_lock_in_a_row_doubles_and_the_second_alerts_the_person_and_access_admins(w, mfa_clock):
    admin = person("alm_admin", (w.alm, "access_admin"))
    enrol(w.cfo)
    first = _lock(w.cfo, mfa_clock)
    assert first.lock_count == 1 and first.locked_until - mfa_clock.at == timedelta(minutes=15)
    assert not AccessAlert.objects.filter(kind="mfa_locked").exists()
    mfa_clock.tick(15 * 60)
    second = _lock(w.cfo, mfa_clock)
    assert second.lock_count == 2 and second.locked_until - mfa_clock.at == timedelta(minutes=30)
    alerted = set(AccessAlert.objects.filter(kind="mfa_locked").values_list("recipient_id", flat=True))
    assert alerted == {w.cfo.pk, admin.pk}
    assert all(a.message_ar and a.message_en for a in AccessAlert.objects.filter(kind="mfa_locked"))
    mfa_clock.tick(30 * 60)
    assert mfa.verify(w.cfo, next_code(w.cfo))
    assert MfaDevice.objects.get(user=w.cfo).lock_count == 0  # a right code ends the run of locks


def test_item3_locks_are_capped_at_24_hours():
    assert mfa._lock_length(1) == timedelta(minutes=15) and mfa._lock_length(3) == timedelta(hours=1)
    assert mfa._lock_length(12) == timedelta(hours=24)


# --- item 4: fail closed, key checks, rotation --------------------------------------------------------------------

def test_item4_an_unencrypted_stored_secret_is_refused_outside_debug(w, settings):
    enrol(w.cfo)
    with connection.cursor() as cursor:  # raw SQL bypasses the field, as tampering would
        cursor.execute("UPDATE web_mfadevice SET secret = %s WHERE user_id = %s", ["JBSWY3DPEHPK3PXP", w.cfo.pk])
    settings.DEBUG = False
    with pytest.raises(ImproperlyConfigured):
        MfaDevice.objects.get(user=w.cfo)
    settings.DEBUG = True
    assert MfaDevice.objects.get(user=w.cfo).secret == "JBSWY3DPEHPK3PXP"


def test_item4_no_keys_fail_closed(w, settings):
    enrol(w.cfo)
    settings.DEBUG, settings.WEB_MFA_KEYS = False, []
    with pytest.raises(ImproperlyConfigured):
        MfaDevice.objects.get(user=w.cfo)
    with pytest.raises(ImproperlyConfigured):
        crypto.encrypt("x")


def test_item4_e002_checks_key_length_and_format(settings):
    from web.checks import mfa_keys_configured

    settings.DEBUG = False
    for keys in ([], ["short"], ["x" * 20 + " " + "y" * 20], ["dev-only-" + "z" * 40]):
        settings.WEB_MFA_KEYS = keys
        assert mfa_keys_configured()[0].id == "web.E002", keys
    settings.WEB_MFA_KEYS = ["A" * 43, "B" * 43]
    assert mfa_keys_configured() == []


def test_item4_rotation_old_key_decrypts_rekey_moves_secrets_and_reports_old_recovery_codes(w, settings):
    old_key, new_key = "o" * 40, "n" * 40
    settings.WEB_MFA_KEYS = [old_key]
    codes = enrol(w.cfo)
    secret = MfaDevice.objects.get(user=w.cfo).secret
    settings.WEB_MFA_KEYS = [new_key, old_key]
    assert MfaDevice.objects.get(user=w.cfo).secret == secret  # the old key still decrypts
    assert mfa.use_recovery_code(w.cfo, codes[0])  # and old codes still work while the old key is configured
    call_command("web_mfa_rekey")
    assert AccessLog.objects.get(event="mfa_rekeyed").details["reencrypted"] == 1
    settings.WEB_MFA_KEYS = [new_key]  # old key removed
    assert MfaDevice.objects.get(user=w.cfo).secret == secret
    with pytest.raises(mfa.MfaError):
        mfa.use_recovery_code(w.cfo, codes[1])  # one-way hashes under the removed key: renew (reported by rekey)
    settings.WEB_MFA_KEYS = [new_key, old_key]
    result = mfa.rekey(expire_old_recovery_codes=True)
    assert result["old_recovery_codes"] == 9 and result["users"] == ["cfo"] and result["expired"]
    assert not RecoveryCode.objects.filter(user=w.cfo, used_at__isnull=True).exists()
    fresh = mfa.renew_recovery_codes(w.cfo, next_code(w.cfo))
    assert set(RecoveryCode.objects.filter(user=w.cfo, used_at__isnull=True)
               .values_list("key_id", flat=True)) == {crypto.key_id(new_key)} and len(fresh) == 10


# --- item 5: an empty version is never decided -----------------------------------------------------------------

def test_item5_an_empty_version_is_refused(w):
    req = request_of(w.alm_invoice)
    enrol(w.cfo)
    with pytest.raises(inbox_services.InboxRefused) as exc:
        inbox_services.decide(w.cfo, w.alm, req.pk, version="", approve=True)
    assert exc.value.code_name == "no_version"
    w.alm_invoice.refresh_from_db()
    assert w.alm_invoice.status == "submitted"


def test_item5_every_adapter_supplies_a_version():
    from django.apps import apps

    for (label, activity), adapter in engine.adapters().items():
        names = {f.name for f in apps.get_model(label)._meta.get_fields()}
        assert adapter.version or names & {"version", "submitted_at", "modified_at"}, (label, activity)


# --- item 6: the English reason reaches services that take one -------------------------------------------------

@pytest.mark.parametrize("label,activity,hook,service", [
    ("billing.contract", "billing.contract", "return_", "billing.services.return_contract"),
    ("billing.contractversion", "billing.version", "reject", "billing.services.reject_version"),
    ("hr.employmentcontract", "hr.contract", "reject", "hr.services.reject_contract"),
    ("tax.vatreturn", "tax.vat_return", "return_", "tax.services.return_to_draft"),
])
def test_item6_adapters_pass_reason_en(monkeypatch, label, activity, hook, service):
    seen = {}
    monkeypatch.setattr(service, lambda doc, **kwargs: seen.update(kwargs))
    token = engine._note.set(("السبب", "The reason"))
    try:
        getattr(engine.adapters()[(label, activity)], hook)(object(), "user", "السبب")
    finally:
        engine._note.reset(token)
    assert seen["reason_ar"] == "السبب" and seen["reason_en"] == "The reason"


# --- item 7: a started enrolment expires ---------------------------------------------------------------------------

def test_item7_an_enrolment_expires_after_15_minutes(w, mfa_clock, client):
    device = mfa.start_enrolment(w.cfo)
    mfa_clock.tick(16 * 60)
    assert mfa.pending_device(w.cfo) is None
    with pytest.raises(mfa.MfaError) as exc:
        mfa.confirm_enrolment(w.cfo, pyotp.TOTP(device.secret).at(mfa_clock.at))
    assert exc.value.code == "expired"
    client.post("/login/", {"username": "cfo", "password": PASSWORD})
    page = client.get("/mfa/enrol/").content.decode()
    assert "data:image/svg+xml" not in page  # the old QR code is not shown again: start again
    assert mfa.start_enrolment(w.cfo) and mfa.pending_device(w.cfo) is not None


# --- item 8: DEBUG off by default ----------------------------------------------------------------------------------

def test_item8_debug_is_off_unless_switched_on():
    from web.test_auth import _settings_value

    assert _settings_value({}, "DEBUG") == "[False]"
    assert _settings_value({"DJANGO_DEBUG": "1"}, "DEBUG") == "[True]"


# --- OQ-29: every approval-right holder needs MFA; step-up on every inbox approval -----------------------------

def test_oq29_holders_of_any_approval_right_need_mfa_and_others_do_not(w):
    assert mfa.requires_mfa(w.hr)  # hr_officer: hr.approve_leave
    for role in ("department_head", "payroll_officer"):
        user = make_user(f"oq29_{role}")
        member(user, w.alm, role)
        assert mfa.requires_mfa(user), role
    assert not mfa.requires_mfa(w.acc) and not mfa.requires_mfa(w.clerk) and not mfa.requires_mfa(w.viewer)


def test_oq29_an_hr_officer_must_enrol_and_verify_every_session(w, signed_in):
    client = signed_in(w.hr)
    assert MfaDevice.objects.filter(user=w.hr, confirmed=True).exists()  # enrolled at sign-in (R14)
    other = Client()
    other.post("/login/", {"username": "hr", "password": PASSWORD})
    assert other.get("/e/ALM/")["Location"].startswith("/mfa/verify/")
    assert client.get("/e/ALM/").status_code == 200
