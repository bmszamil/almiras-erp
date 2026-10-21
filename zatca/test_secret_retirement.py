"""D-19: secret-version retirement after renewal and failed renewal (zatca R-57 to R-60, AT-94 to AT-104, §7), and
the R-10 note of 2026-10-21: a renewed CSID that ZATCA issued but that cannot be taken into use (``key_mismatch``)
marks the unit as needing attention instead of leaving it to fail at ZATCA.

All in the fake world of zatca/conftest.py: the in-memory LocalSecretStore and the fake ZATCA client; no network.
"""
import base64
import json
import logging
import re
from datetime import timedelta

import pytest
from django.core import serializers
from django.db import transaction
from django.utils import timezone

from core import access
from core.access.models import AccessLog
from core.access.testing import make_user, member
from zatca import crypto, keystore, services
from zatca.client import ApiResponse
from zatca.conftest import ADMINS1, NOW, alerts, einvoice_of, issue, onboard, run, xml_text
from zatca.messages import ZatcaError
from zatca.models import EgsCredential, EgsUnit, OnboardingEvent, SubmissionAttempt, ZatcaAlert
from zatca.testing import FakeZatcaClient

pytestmark = pytest.mark.django_db

OTP = "246810"
RETIRED, FAILED = "secret_version_retired", "secret_retirement_failed"


def _events(entity, action):
    return list(OnboardingEvent.objects.filter(entity=entity, action=action).order_by("id"))


def _refs(entity, action, **match):
    return sorted(e.detail["ref"] for e in _events(entity, action)
                  if all(e.detail.get(k) == v for k, v in match.items()))


def _creds(egs):
    production = EgsCredential.objects.get(egs_unit=egs, kind="production", status="active")
    compliance = EgsCredential.objects.get(egs_unit=egs, kind="compliance")
    return production, compliance


def _db_dump():
    from django.apps import apps

    models = list(apps.get_app_config("zatca").get_models()) + [AccessLog]
    return "".join(serializers.serialize("json", m._base_manager.all()) for m in models)


def _no_secret_values(zw, text):
    assert "PRIVATE KEY" not in text
    for secret in zw.fakes.client.issued_secrets:
        assert secret not in text


def _cleared(zw, entity, now=NOW):
    doc = issue(zw, entity)
    run(zw, now=now)
    einv = einvoice_of(doc)
    assert einv.status == "cleared", einv.messages.values_list("code", flat=True)
    return einv


def _custodian(zw, entity):
    """A named master_key custodian of ``entity`` (R-47 / R-58 b alert recipient)."""
    user = make_user(f"keeper_{entity.code.lower()}")
    member(user, entity, "zatca_admin")
    services.grant_key_delegation(entity, user=zw.mp, custodian=user, scope="master_key",
                                  openbao_policy_ref=f"entity-custodian-{entity.code.lower()}")
    return user


def _job(zw, now):
    return services.run_secret_retirement(user=zw.queue, now=now)


# --- R-57: what a committed renewal retires (AT-94, AT-95) ---------------------------------------------------------

def test_at94_committed_renewal_retires_old_key_superseded_and_compliance_secrets(zw, django_capture_on_commit_callbacks):
    egs = onboard(zw, zw.tst1, zw.admin1)
    store, master = zw.fakes.store, services._master_ref(zw.tst1)
    old_key = egs.private_key_ref
    old_production, compliance = _creds(egs)
    d1 = _cleared(zw, zw.tst1)
    with django_capture_on_commit_callbacks(execute=True):
        egs = services.renew_production_csid(egs, OTP, user=zw.admin1)
    assert egs.private_key_ref == "zatca/TST1/EGS1/private_key#v2" and egs.status == "active"

    events = _events(zw.tst1, RETIRED)
    assert sorted((e.detail["item"], e.detail["ref"]) for e in events) == sorted([
        ("private_key", old_key), ("production_secret", old_production.secret_ref),
        ("compliance_secret", compliance.secret_ref)])
    renewed = OnboardingEvent.objects.get(egs_unit=egs, action="pcsid_renewed")
    assert {e.detail["reason"] for e in events} == {"renewed"}
    assert {e.detail["renewal_event"] for e in events} == {renewed.pk}
    assert {e.actor_id for e in events} == {zw.queue.pk} and {e.egs_unit_id for e in events} == {egs.pk}
    _no_secret_values(zw, json.dumps([e.detail for e in events]))

    for use in (lambda: store.sign(old_key, master, b"\x00" * 32),
                lambda: store.read_secret(old_production.secret_ref, master),
                lambda: store.read_secret(compliance.secret_ref, master),
                lambda: store.public_key_der(old_key, master)):
        with pytest.raises(keystore.SecretStoreRefused) as refused:
            use()
        assert refused.value.code == "secret_version_retired"
        assert refused.value.en == ("This key or secret version was retired after a certificate renewal and can no "
                                    "longer be used.")
        assert refused.value.ar == "أُوقف إصدار المفتاح أو السر هذا بعد تجديد الشهادة ولم يعد قابلاً للاستخدام."

    d2 = _cleared(zw, zw.tst1, now=NOW + timedelta(minutes=5))  # D2 is signed with #v2 and its certificate
    new_production = EgsCredential.objects.get(egs_unit=egs, kind="production", status="active")
    assert xml_text(d2.signed_xml, "//ds:X509Certificate") == crypto.cert_base64(
        crypto.cert_from_pem(new_production.certificate_pem))

    # D1 still verifies with certificates only, no key read (AT-64 method)
    calls = list(store.calls)
    signature = base64.b64decode(xml_text(d1.signed_xml, "//ds:SignatureValue"))
    raw_hash = base64.b64decode(d1.invoice_hash)
    superseded = EgsCredential.objects.get(pk=old_production.pk)
    assert superseded.status == "superseded" and superseded.certificate_pem == old_production.certificate_pem
    for cert in (crypto.cert_from_pem(superseded.certificate_pem),
                 crypto.load_cert_base64(xml_text(d1.signed_xml, "//ds:X509Certificate"))):
        assert crypto.verify_hash_signature(cert.public_key(), signature, raw_hash)
    assert store.calls == calls


def test_at95_nothing_destroyed_no_unretire_service_and_retiring_again_is_a_no_op(zw, django_capture_on_commit_callbacks):
    egs = onboard(zw, zw.tst1, zw.admin1)
    store, master = zw.fakes.store, services._master_ref(zw.tst1)
    old_key = egs.private_key_ref
    old_production, compliance = _creds(egs)
    with django_capture_on_commit_callbacks(execute=True):
        services.renew_production_csid(egs, OTP, user=zw.admin1)
    for ref in (old_key, old_production.secret_ref, compliance.secret_ref):
        assert store.is_retired(ref) and store.has_data(ref)  # still present, with its data
    assert not [c for c in store.calls if re.search("delete|destroy|trim|unretire|restore", c)]
    for obj in (services, store, keystore.OpenBaoSecretStore):
        assert not [n for n in dir(obj) if re.search(r"un_?retire|destroy|delete_version|trim|purge_secret", n)]

    before = OnboardingEvent.objects.count()
    assert store.retire_version(old_key, master, reason="renewed") is False  # no error
    services._retire_versions(zw.tst1, [(egs, old_key, "private_key", "renewed", None)], timezone.now())
    _job(zw, timezone.now() + timedelta(days=1))
    assert OnboardingEvent.objects.count() == before  # no second event


# --- R-58: after commit, never rolled back (AT-96, AT-97) -----------------------------------------------------------

class _Rollback(Exception):
    pass


def test_at96_retirement_runs_only_after_commit_and_not_after_a_rollback(zw, django_capture_on_commit_callbacks):
    egs = onboard(zw, zw.tst1, zw.admin1)
    store = zw.fakes.store
    old_key = egs.private_key_ref
    with django_capture_on_commit_callbacks() as callbacks:
        services.renew_production_csid(egs, OTP, user=zw.admin1)
    assert not store.is_retired(old_key) and not _events(zw.tst1, RETIRED)  # before the callbacks run
    for callback in callbacks:
        callback()
    assert store.is_retired(old_key) and len(_events(zw.tst1, RETIRED)) == 3

    egs = EgsUnit.objects.get(pk=egs.pk)
    current = egs.private_key_ref
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        with pytest.raises(_Rollback):
            with transaction.atomic():
                services.renew_production_csid(egs, "135790", user=zw.admin1)
                raise _Rollback
    assert callbacks == []
    egs.refresh_from_db()
    assert egs.private_key_ref == current  # the renewal is not saved
    orphan = "zatca/TST1/EGS1/private_key#v3"
    assert store.has_data(orphan) and not store.is_retired(orphan)  # left for the retirement job (AT-102)
    assert not store.is_retired(current) and len(_events(zw.tst1, RETIRED)) == 3


def test_at97_store_refuses_retirement_renewal_stands_one_alert_then_job_retries(zw, django_capture_on_commit_callbacks):
    keeper = _custodian(zw, zw.tst1)
    egs = onboard(zw, zw.tst1, zw.admin1)
    onboard(zw, zw.tst2, zw.admin2)
    store = zw.fakes.store
    old_key = egs.private_key_ref
    old_production, compliance = _creds(egs)
    store.retirement_unavailable = True
    with django_capture_on_commit_callbacks(execute=True):
        egs = services.renew_production_csid(egs, OTP, user=zw.admin1)
    t0 = timezone.now()

    egs.refresh_from_db()
    assert egs.status == "active" and egs.private_key_ref.endswith("#v2")
    assert EgsCredential.objects.get(pk=old_production.pk).status == "superseded"
    assert EgsCredential.objects.filter(egs_unit=egs, kind="production", status="active").exists()
    assert _cleared(zw, zw.tst1).egs_unit_id == egs.pk  # the new key signs

    for minutes in (2, 5, 10):  # three job runs while the store still refuses (R-24 backoff 1, 2, 4 min)
        assert _job(zw, t0 + timedelta(minutes=minutes))["failed"] == 3
    failures = _events(zw.tst1, FAILED)
    assert len(failures) == 3 * 4
    for ref in (old_key, old_production.secret_ref, compliance.secret_ref):
        mine = [e.detail for e in failures if e.detail["ref"] == ref]
        assert [d["attempt"] for d in mine] == [1, 2, 3, 4]
        assert {d["error_code"] for d in mine} == {"secret_store_unavailable"} and {d["reason"] for d in mine} == {
            "renewed"}
    assert alerts("secret_retirement_failed") == sorted([*ADMINS1, keeper.username, "mp"])  # once, not per retry
    assert ZatcaAlert.objects.filter(kind="secret_retirement_failed", entity=zw.tst2).count() == 0
    assert set(services.pending_secret_retirements(zw.admin1, zw.tst1)) == {
        old_key, old_production.secret_ref, compliance.secret_ref}

    store.retirement_unavailable = False
    assert _job(zw, t0 + timedelta(minutes=30))["retired"] == 3
    assert _refs(zw.tst1, RETIRED, reason="renewed") == sorted([old_key, old_production.secret_ref,
                                                                 compliance.secret_ref])
    with pytest.raises(keystore.SecretStoreRefused):
        store.sign(old_key, services._master_ref(zw.tst1), b"\x00" * 32)
    assert services.pending_secret_retirements(zw.admin1, zw.tst1) == {}
    assert _job(zw, t0 + timedelta(hours=1)) == {"retired": 0, "failed": 0, "errors": 0}  # nothing left to change
    assert not _events(zw.tst2, RETIRED) and not _events(zw.tst2, FAILED)


# --- R-59: a failed renewal retires what it wrote (AT-98 to AT-101) -----------------------------------------------------

def test_at98_otp_refused_retires_the_new_key_version(zw, django_capture_on_commit_callbacks):
    egs = onboard(zw, zw.tst1, zw.admin1)
    production, _ = _creds(egs)
    zw.fakes.client.rejected_otps.add(OTP)
    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(ZatcaError) as exc:
            services.renew_production_csid(egs, OTP, user=zw.admin1)
    assert exc.value.code == "otp_rejected" and exc.value.en and exc.value.ar
    new_key = "zatca/TST1/EGS1/private_key#v2"
    assert zw.fakes.store.is_retired(new_key)
    assert [(e.detail["ref"], e.detail["item"], e.detail["reason"]) for e in _events(zw.tst1, RETIRED)] == [
        (new_key, "private_key", "renewal_failed")]
    egs.refresh_from_db()
    assert egs.private_key_ref == "zatca/TST1/EGS1/private_key#v1" and egs.status == "active"
    assert not egs.attention_reason  # ZATCA issued nothing
    assert EgsCredential.objects.get(pk=production.pk).status == "active"
    assert _cleared(zw, zw.tst1).egs_unit_id == egs.pk  # the old key and credential keep signing
    assert OnboardingEvent.objects.filter(egs_unit=egs, action="otp_rejected").exists()
    assert OTP not in json.dumps([e.detail for e in OnboardingEvent.objects.all()])


@pytest.mark.parametrize("answer, code", [
    (ApiResponse(503, {}), "zatca_unreachable"),
    (FakeZatcaClient.timeout(), "zatca_unreachable"),
    (ApiResponse(200, {"requestID": "1"}), "no_certificate"),
])
def test_at99_unreachable_timeout_or_no_certificate_retire_the_attempts_key(zw, answer, code,
                                                                           django_capture_on_commit_callbacks):
    egs = onboard(zw, zw.tst1, zw.admin1)
    for attempt in (2, 3):
        zw.fakes.client.script["renew_production_csid"] = [answer]
        with django_capture_on_commit_callbacks(execute=True):
            with pytest.raises(ZatcaError) as exc:
                services.renew_production_csid(egs, OTP, user=zw.admin1)
        assert exc.value.code == code
        assert zw.fakes.store.is_retired(f"zatca/TST1/EGS1/private_key#v{attempt}")
    assert _refs(zw.tst1, RETIRED, reason="renewal_failed") == [
        "zatca/TST1/EGS1/private_key#v2", "zatca/TST1/EGS1/private_key#v3"]
    assert not OnboardingEvent.objects.filter(egs_unit=egs, action="otp_rejected").exists()  # R-7
    egs.refresh_from_db()
    assert egs.private_key_ref.endswith("#v1") and not egs.attention_reason


def test_at100_key_mismatch_retires_new_key_and_new_secret_and_keeps_the_old_credential(zw,
                                                                                        django_capture_on_commit_callbacks):
    egs = onboard(zw, zw.tst1, zw.admin1)
    production, _ = _creds(egs)
    zw.fakes.client.developer_portal_production_certificate = True  # a certificate for another key
    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(ZatcaError) as exc:
            services.renew_production_csid(egs, OTP, user=zw.admin1)
    assert exc.value.code == "key_mismatch" and "not for the new key pair" in exc.value.en and exc.value.ar
    assert EgsCredential.objects.filter(egs_unit=egs, kind="production").count() == 1
    assert EgsCredential.objects.get(pk=production.pk).status == "active"
    new_key, new_secret = "zatca/TST1/EGS1/private_key#v2", "zatca/TST1/EGS1/production_csid_secret#v2"
    assert _refs(zw.tst1, RETIRED, reason="renewal_failed") == sorted([new_key, new_secret])
    assert zw.fakes.store.is_retired(new_key) and zw.fakes.store.is_retired(new_secret)
    egs.refresh_from_db()
    assert egs.private_key_ref.endswith("#v1")


def test_at101_store_unavailable_during_failed_renewal_keeps_the_users_error(zw, django_capture_on_commit_callbacks):
    egs = onboard(zw, zw.tst1, zw.admin1)
    store = zw.fakes.store
    zw.fakes.client.rejected_otps.add(OTP)
    store.retirement_unavailable = True
    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(ZatcaError) as exc:
            services.renew_production_csid(egs, OTP, user=zw.admin1)
    t0 = timezone.now()
    assert exc.value.code == "otp_rejected"  # not a store error
    new_key = "zatca/TST1/EGS1/private_key#v2"
    [failure] = _events(zw.tst1, FAILED)
    assert failure.detail == {"ref": new_key, "item": "private_key", "reason": "renewal_failed",
                              "error_code": "secret_store_unavailable", "attempt": 1}
    assert alerts("secret_retirement_failed") == sorted([*ADMINS1, "mp"])
    store.retirement_unavailable = False
    _job(zw, t0 + timedelta(minutes=2))
    assert store.is_retired(new_key) and _refs(zw.tst1, RETIRED, reason="renewal_failed") == [new_key]
    assert len(alerts("secret_retirement_failed")) == 3  # no new alert


# --- R-60: the reconciliation job (AT-102, AT-103) ---------------------------------------------------------------

def test_at102_job_retires_unreferenced_versions_after_the_grace_period(zw, settings):
    settings.ZATCA_RETIRE_GRACE_MINUTES = 120
    store = zw.fakes.store
    t0 = timezone.now()
    egs = onboard(zw, zw.tst1, zw.admin1)
    first_production, compliance = _creds(egs)
    # Two renewals whose after-commit retirement never ran (as after a crash), then a renewal still in progress.
    egs = services.renew_production_csid(egs, OTP, user=zw.admin1)
    second_production = EgsCredential.objects.get(egs_unit=egs, kind="production", status="active")
    egs = services.renew_production_csid(egs, "135790", user=zw.admin1)
    assert egs.private_key_ref == "zatca/TST1/EGS1/private_key#v3"
    zw.fakes.client.rejected_otps.add("975310")
    with pytest.raises(ZatcaError):
        services.renew_production_csid(egs, "975310", user=zw.admin1)  # wrote #v4; its callback never ran
    active = EgsCredential.objects.get(egs_unit=egs, kind="production", status="active")
    # U2: compliance_passed, never renewed
    u2 = services.create_egs_unit(zw.tst1, user=zw.admin1, code="EGS2", name_en="TST1 EGS2", name_ar="وحدة",
                                  serial_number="1-TestERP|2-1.0|3-TST1-EGS2", industry="Real estate",
                                  branch_name_en="Test Branch")
    services.generate_csr(u2, user=zw.admin1)
    services.enter_otp(u2, "123457", user=zw.admin1)
    u2 = services.run_compliance_checks(u2, user=zw.admin1)
    assert u2.status == "compliance_passed"
    u2_compliance = EgsCredential.objects.get(egs_unit=u2, kind="compliance")

    old = t0 - timedelta(hours=3)
    for ref in ("zatca/TST1/EGS1/private_key#v1", "zatca/TST1/EGS1/private_key#v2", "zatca/TST1/EGS1/private_key#v3",
                first_production.secret_ref, second_production.secret_ref, active.secret_ref, compliance.secret_ref,
                u2.private_key_ref, u2_compliance.secret_ref):
        store.backdate(ref, old)
    v4 = "zatca/TST1/EGS1/private_key#v4"
    store.backdate(v4, t0 - timedelta(minutes=10))

    counts = _job(zw, t0)
    expected = sorted(["zatca/TST1/EGS1/private_key#v1", "zatca/TST1/EGS1/private_key#v2",
                       first_production.secret_ref, second_production.secret_ref, compliance.secret_ref])
    assert counts["retired"] == 5 and _refs(zw.tst1, RETIRED, reason="unreferenced") == expected
    for kept in (egs.private_key_ref, v4, active.secret_ref, u2.private_key_ref, u2_compliance.secret_ref):
        assert not store.is_retired(kept)
    _cleared(zw, zw.tst1)  # the current key and the active secret still sign and authenticate

    before = OnboardingEvent.objects.count()
    _job(zw, t0 + timedelta(hours=2))
    assert store.is_retired(v4) and not store.is_retired(egs.private_key_ref) and not store.is_retired(active.secret_ref)
    new = list(OnboardingEvent.objects.order_by("id")[before:])
    assert [(e.action, e.detail["ref"], e.detail["reason"]) for e in new] == [(RETIRED, v4, "unreferenced")]
    _job(zw, t0 + timedelta(hours=3))
    assert OnboardingEvent.objects.count() == before + 1  # events only for versions that change


def test_at103_retirement_stays_inside_the_entity(zw, django_capture_on_commit_callbacks):
    egs1 = onboard(zw, zw.tst1, zw.admin1)
    egs2 = onboard(zw, zw.tst2, zw.admin2)
    store = zw.fakes.store
    with django_capture_on_commit_callbacks(execute=True):
        services.renew_production_csid(egs1, OTP, user=zw.admin1)
    _job(zw, timezone.now() + timedelta(days=1))
    retired = [e.detail["ref"] for e in OnboardingEvent.objects.filter(action=RETIRED)]
    assert retired and all(ref.startswith("zatca/TST1/") for ref in retired)
    assert not OnboardingEvent.objects.filter(entity=zw.tst2, action__in=(RETIRED, FAILED)).exists()
    with pytest.raises(keystore.SecretStoreRefused):  # R-50 a: never under another entity's master key
        store.retire_version(egs2.private_key_ref, services._master_ref(zw.tst1), reason="renewed")
    assert not store.is_retired(egs2.private_key_ref)
    assert _cleared(zw, zw.tst2).egs_unit_id == egs2.pk


# --- R-57: stamping with a retired version (AT-104) -------------------------------------------------------------------

def test_at104_stamp_with_a_retired_key_is_refused_before_the_icv(zw, caplog):
    caplog.set_level(logging.DEBUG)
    egs = onboard(zw, zw.tst1, zw.admin1)
    zw.fakes.store.retire_version(egs.private_key_ref, services._master_ref(zw.tst1), reason="fixture")
    sign_calls = zw.fakes.store.calls.count("sign")
    doc = issue(zw, zw.tst1)
    run(zw)
    einv = einvoice_of(doc)
    egs.refresh_from_db()
    assert einv.status == "pending" and einv.icv is None and not einv.signed_xml and egs.last_icv == 0
    assert einv.hold_reason == "egs_not_ready"
    message = einv.messages.get(code="secret_version_retired")
    assert message.message_en == keystore.RETIRED_EN and message.message_ar == keystore.RETIRED_AR
    assert zw.fakes.store.calls.count("sign") == sign_calls  # refused before anything was signed
    _no_secret_values(zw, _db_dump() + caplog.text)


# --- R-10 note (2026-10-21): renewed CSID issued, key mismatch -> the unit needs attention ---------------------------------

def test_key_mismatch_after_issue_marks_the_unit_and_stops_signing_and_submitting(zw, monkeypatch,
                                                                                   django_capture_on_commit_callbacks):
    egs = onboard(zw, zw.tst1, zw.admin1)
    queued_doc = issue(zw, zw.tst1)
    services.stamp_document(einvoice_of(queued_doc), user=zw.queue, now=NOW)  # stamped between the check and ZATCA
    assert einvoice_of(queued_doc).status == "queued"
    monkeypatch.setattr(services, "_refuse_renewal_in_flight", lambda unit: None)
    zw.fakes.client.developer_portal_production_certificate = True
    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(ZatcaError) as exc:
            services.renew_production_csid(egs, OTP, user=zw.admin1)
    assert exc.value.code == "key_mismatch" and "stops signing and submitting" in exc.value.en and exc.value.ar
    egs.refresh_from_db()
    assert egs.attention_reason == "renewal_key_mismatch" and egs.attention_since is not None
    assert egs.status == "active"  # still active locally; nothing pretends ZATCA did not issue
    event = OnboardingEvent.objects.get(egs_unit=egs, action="renewal_needs_attention")
    assert event.detail["reason"] == "renewal_key_mismatch"
    assert alerts("egs_needs_attention") == sorted([*ADMINS1, "mp"])
    [alert] = ZatcaAlert.objects.filter(kind="egs_needs_attention", recipient=zw.mp)
    assert "needs attention" in alert.message_en and "تحتاج إلى متابعة" in alert.message_ar

    calls = len(zw.fakes.client.calls)
    new_doc = issue(zw, zw.tst1)
    run(zw, now=NOW + timedelta(minutes=5))
    queued, held = einvoice_of(queued_doc), einvoice_of(new_doc)
    assert queued.status == "queued" and not SubmissionAttempt.objects.filter(einvoice=queued).exists()
    assert len(zw.fakes.client.calls) == calls  # nothing sent to fail at ZATCA
    message = queued.messages.get(code="egs_needs_attention")
    assert "EGS1 needs attention" in message.message_en and message.message_ar
    assert held.status == "pending" and held.icv is None and held.messages.filter(code="egs_needs_attention").exists()
    egs.refresh_from_db()
    assert egs.last_icv == 1
    run(zw, now=NOW + timedelta(minutes=20))  # no repeated messages or alerts
    assert queued.messages.filter(code="egs_needs_attention").count() == 1
    assert ZatcaAlert.objects.filter(kind="egs_needs_attention", einvoice=queued).count() == len(ADMINS1)

    with pytest.raises(ZatcaError):
        services.clear_egs_attention(egs, user=zw.admin1, reason=" ")
    with pytest.raises(access.AccessDenied):
        services.clear_egs_attention(egs, user=zw.accountant, reason="x")
    services.clear_egs_attention(egs, user=zw.admin1, reason="ZATCA confirmed the old CSID is valid (ticket 42)")
    assert OnboardingEvent.objects.filter(egs_unit=egs, action="attention_cleared").exists()
    run(zw, now=NOW + timedelta(minutes=30))
    assert einvoice_of(queued_doc).status == "cleared" and einvoice_of(new_doc).status == "cleared"


def test_a_successful_renewal_or_a_revocation_ends_the_attention(zw, django_capture_on_commit_callbacks):
    egs = onboard(zw, zw.tst1, zw.admin1)
    zw.fakes.client.developer_portal_production_certificate = True
    with pytest.raises(ZatcaError):
        services.renew_production_csid(egs, OTP, user=zw.admin1)
    zw.fakes.client.developer_portal_production_certificate = False
    egs = services.renew_production_csid(egs, "135790", user=zw.admin1)
    assert not egs.attention_reason and egs.attention_since is None
    _cleared(zw, zw.tst1)
    zw.fakes.client.developer_portal_production_certificate = True
    with pytest.raises(ZatcaError):
        services.renew_production_csid(egs, "975310", user=zw.admin1)
    egs.refresh_from_db()
    assert egs.attention_reason == "renewal_key_mismatch"
    services.revoke_egs_unit(egs, user=zw.admin1, reason="re-onboard")
    egs.refresh_from_db()
    assert egs.status == "revoked" and not egs.attention_reason


def test_store_failure_after_zatca_issued_also_needs_attention(zw, monkeypatch, django_capture_on_commit_callbacks):
    egs = onboard(zw, zw.tst1, zw.admin1)
    store = zw.fakes.store
    original = store.write_secret

    def sealed_after_issue(*args, **kwargs):
        store.sealed = True
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "write_secret", sealed_after_issue)
    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(ZatcaError) as exc:
            services.renew_production_csid(egs, OTP, user=zw.admin1)
    assert exc.value.code == "secret_store_unavailable"
    egs.refresh_from_db()
    assert egs.attention_reason == "renewal_unconfirmed" and egs.private_key_ref.endswith("#v1")
    assert _events(zw.tst1, FAILED)[0].detail["ref"] == "zatca/TST1/EGS1/private_key#v2"  # pending for the job


# --- §7 permissions -----------------------------------------------------------------------------------------------

def test_only_the_zatca_system_user_runs_retirement_and_viewing_follows_section_7(zw, django_capture_on_commit_callbacks):
    egs = onboard(zw, zw.tst1, zw.admin1)
    with django_capture_on_commit_callbacks(execute=True):
        services.renew_production_csid(egs, OTP, user=zw.admin1)
    for user in (zw.admin1, zw.accountant, zw.mp, zw.agent):
        with pytest.raises(access.AccessDenied):
            services.run_secret_retirement(user=user)
    assert len(services.secret_retirement_events(zw.admin1, zw.tst1)) == 3
    assert len(services.secret_retirement_events(zw.viewer, zw.tst1)) == 3  # read-only viewer / auditor
    assert services.pending_secret_retirements(zw.viewer, zw.tst1) == {}
    for user in (zw.accountant, zw.agent, zw.clerk):
        with pytest.raises(access.AccessDenied):
            services.secret_retirement_events(user, zw.tst1)
    with pytest.raises(access.AccessDenied):  # TST2's admin never sees TST1's events
        services.secret_retirement_events(zw.admin2, zw.tst1)


def test_retirement_job_management_command(zw, django_capture_on_commit_callbacks):
    from django.core.management import call_command

    onboard(zw, zw.tst1, zw.admin1)
    call_command("zatca_retire_secrets")  # nothing to change: no error, no event
    assert not OnboardingEvent.objects.filter(action__in=(RETIRED, FAILED)).exists()


# --- OQ-34 (f) default: the OpenBao KV v2 adapter (fake transport; no network) ---------------------------------------

class FakeBao:
    """KV v2 metadata and AppRole login, as OpenBao answers them; records every request (never real values)."""

    def __init__(self):
        self.metadata = {}  # path -> {"versions": {"1": {...}}, "custom_metadata": {...}}
        self.requests = []
        self.sealed = False
        self.roles = {"signer-role": "signer-token", "retire-role": "retire-token"}

    def add(self, path, versions):
        self.metadata[path] = {"versions": {str(v): {"created_time": "2026-10-20T08:00:00.123456789Z",
                                                     "deletion_time": "", "destroyed": False} for v in versions},
                               "custom_metadata": None}

    def __call__(self, method, url, headers, body):
        path = url.split("/v1/", 1)[1]
        self.requests.append((method, path, headers.get("X-Vault-Token"), headers.get("Content-Type")))
        if self.sealed:
            return 503, {"errors": ["Vault is sealed"]}
        data = json.loads(body) if body else {}
        if path == "auth/approle/login":
            token = self.roles.get(data.get("role_id"))
            return (200, {"auth": {"client_token": token, "lease_duration": 3600}}) if token else (400, {})
        mount, _, rest = path.partition("/")
        kind, _, item = rest.partition("/")
        assert mount == "zatca-kv" and kind == "metadata", path  # never data/, delete/, destroy/
        token = headers.get("X-Vault-Token")
        if method == "GET":
            if token not in ("signer-token", "retire-token"):
                return 403, {}
            return (200, {"data": self.metadata[item]}) if item in self.metadata else (404, {})
        if method == "PATCH":
            if token != "retire-token":  # the signer never writes metadata/
                return 403, {}
            assert headers.get("Content-Type") == "application/merge-patch+json"
            meta = self.metadata[item]
            meta["custom_metadata"] = {**(meta["custom_metadata"] or {}), **data["custom_metadata"]}
            return 204, {}
        raise AssertionError(f"unexpected {method} {path}")


@pytest.fixture
def bao(settings, tmp_path):
    for name, value in (("ROLE_ID", "signer-role"), ("SECRET_ID", "s1"), ("RETIRE_ROLE_ID", "retire-role"),
                        ("RETIRE_SECRET_ID", "s2")):
        path = tmp_path / name.lower()
        path.write_text(value)
        setattr(settings, f"ZATCA_OPENBAO_{name}_FILE", str(path))
    settings.ZATCA_OPENBAO_ADDRESS = "https://openbao.test:8200"
    fake = FakeBao()
    fake.add("zatca/TST1/EGS1/private_key", [1, 2])
    return fake, keystore.OpenBaoSecretStore(transport=fake)


MASTER1, MASTER2 = "zatca/TST1/master_key#v1", "zatca/TST2/master_key#v1"


def test_openbao_retirement_flags_custom_metadata_and_refuses_retired_reads(bao):
    fake, store = bao
    ref = "zatca/TST1/EGS1/private_key#v1"
    assert store.retire_version(ref, MASTER1, reason="renewed") is True
    flag = fake.metadata["zatca/TST1/EGS1/private_key"]["custom_metadata"]["retired_v1"]
    assert flag.endswith("|renewed") and re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\|", flag)
    patches = [r for r in fake.requests if r[0] == "PATCH"]
    assert patches == [("PATCH", "zatca-kv/metadata/zatca/TST1/EGS1/private_key", "retire-token",
                        "application/merge-patch+json")]
    assert store.retire_version(ref, MASTER1, reason="renewed") is False  # idempotent: no second patch
    assert len([r for r in fake.requests if r[0] == "PATCH"]) == 1
    for read in (lambda: store.sign(ref, MASTER1, b"\x00" * 32), lambda: store.public_key_der(ref, MASTER1),
                 lambda: store.read_secret(ref, MASTER1)):
        with pytest.raises(keystore.SecretStoreRefused) as refused:
            read()
        assert refused.value.code == "secret_version_retired"
    with pytest.raises(keystore.SecretStoreUnavailable):  # v2 is not retired; reading key material is not built
        store.sign("zatca/TST1/EGS1/private_key#v2", MASTER1, b"\x00" * 32)
    rows = store.list_versions("zatca/TST1/EGS1/private_key", MASTER1)
    assert [(r["version"], r["retired"]) for r in rows] == [(1, True), (2, False)]
    assert rows[0]["created_at"].isoformat() == "2026-10-20T08:00:00.123456+00:00"
    assert not [r for r in fake.requests if "/data/" in r[1] or "delete" in r[1] or "destroy" in r[1]]


def test_openbao_adapter_refuses_other_entities_unknown_versions_and_fails_closed(bao, settings):
    fake, store = bao
    with pytest.raises(keystore.SecretStoreRefused):
        store.retire_version("zatca/TST1/EGS1/private_key#v1", MASTER2, reason="renewed")
    with pytest.raises(keystore.SecretStoreRefused):
        store.retire_version("zatca/TST1/EGS1/private_key#v9", MASTER1, reason="renewed")
    assert not [r for r in fake.requests if r[0] == "PATCH"]
    fake.sealed = True
    with pytest.raises(keystore.SecretStoreUnavailable):
        store.retire_version("zatca/TST1/EGS1/private_key#v1", MASTER1, reason="renewed")
    settings.ZATCA_OPENBAO_ADDRESS = ""
    with pytest.raises(keystore.SecretStoreUnavailable):
        keystore.OpenBaoSecretStore(transport=fake).list_versions("zatca/TST1/EGS1/private_key", MASTER1)
    with pytest.raises(keystore.SecretStoreUnavailable):  # writes and the hierarchy stay unbuilt (OQ-34 a)
        store.write_secret("zatca/TST1/EGS1/production_csid_secret", "x", MASTER1)


def test_openbao_policies_back_the_adapter():
    """OQ-34 (f): the signer reads metadata but never writes it; the retirement identity only reads and patches
    metadata of its entity; no ERP identity can delete, undelete or destroy."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "deploy" / "openbao" / "policies"
    signer = (root / "erp-signer.hcl.tmpl").read_text()
    retire = (root / "erp-retire.hcl.tmpl").read_text()
    assert 'path "zatca-kv/metadata/zatca/${ENTITY}/*" {\n  capabilities = ["read"]' in signer
    assert 'path "zatca-kv/metadata/zatca/${ENTITY}/*" {\n  capabilities = ["read", "patch"]' in retire
    assert "zatca-kv/data/" not in retire
    for text in (signer, retire, (root / "erp-onboard.hcl.tmpl").read_text()):
        for verb in ("delete", "undelete", "destroy"):
            assert f'path "zatca-kv/{verb}/*" {{\n  capabilities = ["deny"]' in text
