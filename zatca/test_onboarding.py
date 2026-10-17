"""Profile, key hierarchy, custody and EGS onboarding (R-4 to R-10, R-34, R-36, R-41, R-45, R-50; AT-1 to AT-9,
AT-30, AT-37, AT-38, AT-43, AT-48, AT-50, AT-57, AT-68, AT-70, AT-71)."""
import base64
import logging
from datetime import date, datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.x509.oid import NameOID, ObjectIdentifier
from django.core import serializers

from core import access
from core.access.models import AccessLog
from core.access.testing import make_user, member
from zatca import crypto, keystore, services
from zatca.conftest import NOW, keys, onboard
from zatca.messages import ZatcaError
from zatca.models import (
    EgsCredential,
    EgsUnit,
    EntityMasterKey,
    KeyCustodianDelegation,
    OnboardingEvent,
    ZatcaProfile,
)

pytestmark = pytest.mark.django_db


def _egs(zw, entity, admin, code="EGS1", **kw):
    return services.create_egs_unit(entity, user=admin, code=code, name_en="EGS", name_ar="وحدة",
                                    serial_number=f"1-TestERP|2-1.0|3-{code}", industry="Real estate",
                                    supports_standard=kw.get("standard", True),
                                    supports_simplified=kw.get("simplified", True), branch_name_en="Branch")


def _profile(zw, entity, admin):
    return services.create_profile(entity, user=admin, go_live_date=date(2026, 1, 1))


# --- R-4: profile ----------------------------------------------------------------------------------

def test_at1_profile_needs_vat_number(zw):
    keys(zw, zw.tst1, zw.admin1)
    profile = _profile(zw, zw.tst1, zw.admin1)
    type(zw.tst1).objects.filter(pk=zw.tst1.pk).update(vat_number="")
    with pytest.raises(ZatcaError) as exc:
        services.enable_profile(profile, user=zw.admin1)
    assert exc.value.code == "profile_invalid" and "VAT" in exc.value.en and "الرقم الضريبي" in exc.value.ar


@pytest.mark.parametrize("field,value,code", [("building_no", "754", "building_no"), ("postal_code", "2343", "postal_code"),
                                              ("district_ar", "", "district")])
def test_at48_address_fields_listed(zw, field, value, code):
    keys(zw, zw.tst1, zw.admin1)
    profile = _profile(zw, zw.tst1, zw.admin1)
    changes = {field: value}
    if field == "district_ar":
        changes["district_en"] = ""
    type(zw.tst1).objects.filter(pk=zw.tst1.pk).update(**changes)
    with pytest.raises(ZatcaError) as exc:
        services.enable_profile(profile, user=zw.admin1)
    assert [p[0] for p in exc.value.problems] == [code]
    assert all(p[2] for p in exc.value.problems)  # Arabic text


def test_at48_blank_additional_number_is_fine(zw):
    keys(zw, zw.tst1, zw.admin1)
    type(zw.tst1).objects.filter(pk=zw.tst1.pk).update(additional_no="")
    profile = services.enable_profile(_profile(zw, zw.tst1, zw.admin1), user=zw.admin1)
    assert profile.is_enabled
    assert not hasattr(ZatcaProfile, "street_ar")  # AT-45: no address on the profile (3.1)


def test_r50b_profile_needs_master_key(zw):
    profile = _profile(zw, zw.tst1, zw.admin1)
    with pytest.raises(ZatcaError) as exc:
        services.enable_profile(profile, user=zw.admin1)
    assert exc.value.code == "no_master_key"


# --- R-5 to R-8: onboarding state machine ------------------------------------------------------------

def test_at2_csr_generated_no_key_material_in_db_or_logs(zw, caplog):
    caplog.set_level(logging.DEBUG)
    keys(zw, zw.tst1, zw.admin1)
    _profile(zw, zw.tst1, zw.admin1)
    egs = services.generate_csr(_egs(zw, zw.tst1, zw.admin1), user=zw.admin1)
    assert egs.status == "csr_ready"
    assert egs.private_key_ref == "zatca/TST1/EGS1/private_key#v1"
    dump = _db_dump()
    assert "PRIVATE KEY" not in dump and "PRIVATE KEY" not in caplog.text
    event = OnboardingEvent.objects.get(egs_unit=egs, action="csr_generated")
    assert event.detail["private_key_ref"] == egs.private_key_ref


def _db_dump():
    from django.apps import apps

    models = [m for m in apps.get_app_config("zatca").get_models()] + [AccessLog]
    return "".join(serializers.serialize("json", m._base_manager.all()) for m in models)


def test_at68_csr_fields_and_oids(zw):
    keys(zw, zw.tst1, zw.admin1)
    _profile(zw, zw.tst1, zw.admin1)
    egs = services.generate_csr(_egs(zw, zw.tst1, zw.admin1), user=zw.admin1)
    csr = x509.load_pem_x509_csr(egs.csr_pem.encode())
    assert [a.oid for a in csr.subject] == [NameOID.COUNTRY_NAME, NameOID.ORGANIZATIONAL_UNIT_NAME,
                                             NameOID.ORGANIZATION_NAME, NameOID.COMMON_NAME]
    assert csr.subject.get_attributes_for_oid(NameOID.COUNTRY_NAME)[0].value == "SA"
    san = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    dirname = san.get_values_for_type(x509.DirectoryName)[0]
    values = {a.oid.dotted_string: a.value for a in dirname}
    assert values == {"2.5.4.4": "1-TestERP|2-1.0|3-EGS1", "0.9.2342.19200300.100.1.1": "311111111100003",
                      "2.5.4.12": "1100", "2.5.4.26": "ABCD1234", "2.5.4.15": "Real estate"}
    template = csr.extensions.get_extension_for_oid(ObjectIdentifier("1.3.6.1.4.1.311.20.2")).value.value
    assert template == b"\x0c\x15TSTZATCA-Code-Signing"
    assert csr.public_key().curve.name == "secp256k1"
    assert csr.signature_algorithm_oid.dotted_string == "1.2.840.10045.4.3.2"  # ecdsa-with-SHA256
    assert csr.is_signature_valid


def test_at3_otp_issues_compliance_csid_and_is_never_stored(zw, caplog):
    caplog.set_level(logging.DEBUG)
    keys(zw, zw.tst1, zw.admin1)
    _profile(zw, zw.tst1, zw.admin1)
    egs = services.generate_csr(_egs(zw, zw.tst1, zw.admin1), user=zw.admin1)
    egs = services.enter_otp(egs, "918273", user=zw.admin1)
    assert egs.status == "compliance_csid"
    credential = EgsCredential.objects.get(egs_unit=egs, kind="compliance", status="active")
    assert credential.secret_ref == "zatca/TST1/EGS1/compliance_csid_secret#v1"
    dump = _db_dump()
    assert "918273" not in dump and "918273" not in caplog.text
    secret = zw.fakes.client.issued_secrets[0]
    assert secret not in dump and secret not in caplog.text  # R-6: CSID secrets never in the DB or logs
    assert zw.fakes.store.read_secret(credential.secret_ref, services._master_ref(zw.tst1)) == secret
    assert OnboardingEvent.objects.filter(egs_unit=egs, action="otp_entered").exists()


@pytest.mark.parametrize("otp", ["12345", "12a456", "1234567", ""])
def test_at50_bad_otp_refused_before_any_call(zw, otp):
    keys(zw, zw.tst1, zw.admin1)
    _profile(zw, zw.tst1, zw.admin1)
    egs = services.generate_csr(_egs(zw, zw.tst1, zw.admin1), user=zw.admin1)
    with pytest.raises(ZatcaError) as exc:
        services.enter_otp(egs, otp, user=zw.admin1)
    assert exc.value.code == "otp_format" and exc.value.ar
    assert not zw.fakes.client.calls
    assert not OnboardingEvent.objects.filter(action="otp_entered").exists()


def test_at37_otp_rejected_by_zatca(zw):
    keys(zw, zw.tst1, zw.admin1)
    _profile(zw, zw.tst1, zw.admin1)
    egs = services.generate_csr(_egs(zw, zw.tst1, zw.admin1), user=zw.admin1)
    zw.fakes.client.rejected_otps.add("111111")
    with pytest.raises(ZatcaError) as exc:
        services.enter_otp(egs, "111111", user=zw.admin1)
    assert exc.value.code == "otp_rejected" and "new OTP" in exc.value.en and exc.value.ar
    egs.refresh_from_db()
    assert egs.status == "csr_ready"
    event = OnboardingEvent.objects.get(egs_unit=egs, action="otp_rejected")
    assert "111111" not in str(event.detail)
    assert len([c for c in zw.fakes.client.calls if c[0] == "compliance_csid"]) == 1  # no automatic resubmission


def test_at4_all_six_samples_pass(zw):
    keys(zw, zw.tst1, zw.admin1)
    _profile(zw, zw.tst1, zw.admin1)
    egs = services.generate_csr(_egs(zw, zw.tst1, zw.admin1), user=zw.admin1)
    services.enter_otp(egs, "123456", user=zw.admin1)
    egs = services.run_compliance_checks(egs, user=zw.admin1)
    assert egs.status == "compliance_passed"
    checks = [c for c in zw.fakes.client.calls if c[0] == "compliance_check"]
    assert len(checks) == 6
    from zatca import ubl

    kinds = set()
    for _, _, body in checks:
        xml = base64.b64decode(body["invoice"])
        kinds.add((ubl.find_text(xml, "/*/cbc:InvoiceTypeCode/@name")[:2], ubl.find_text(xml, "/*/cbc:InvoiceTypeCode")))
        assert crypto.invoice_hash(xml) == body["invoiceHash"]
    assert kinds == {(p, t) for p in ("01", "02") for t in ("388", "383", "381")}


def test_r8_flag_1000_sends_three_standard_samples(zw):
    keys(zw, zw.tst1, zw.admin1)
    _profile(zw, zw.tst1, zw.admin1)
    egs = services.generate_csr(_egs(zw, zw.tst1, zw.admin1, simplified=False), user=zw.admin1)
    services.enter_otp(egs, "123456", user=zw.admin1)
    services.run_compliance_checks(egs, user=zw.admin1)
    assert len([c for c in zw.fakes.client.calls if c[0] == "compliance_check"]) == 3


def test_at5_failed_sample_back_to_draft_then_pcsid_refused(zw):
    keys(zw, zw.tst1, zw.admin1)
    _profile(zw, zw.tst1, zw.admin1)
    egs = services.generate_csr(_egs(zw, zw.tst1, zw.admin1), user=zw.admin1)
    services.enter_otp(egs, "123456", user=zw.admin1)
    zw.fakes.client.fail_compliance_types.add(("simplified", "383"))
    with pytest.raises(ZatcaError) as exc:
        services.run_compliance_checks(egs, user=zw.admin1)
    assert exc.value.code == "compliance_failed" and "simplified-383" in exc.value.en
    egs.refresh_from_db()
    assert egs.status == "draft"
    assert OnboardingEvent.objects.filter(egs_unit=egs, action="compliance_checks_failed").exists()
    with pytest.raises(ZatcaError):
        services.request_production_csid(egs, user=zw.admin1)
    with pytest.raises(ZatcaError):  # the next step is a new CSR, then a new OTP
        services.enter_otp(egs, "654321", user=zw.admin1)
    egs = services.generate_csr(egs, user=zw.admin1)
    assert egs.status == "csr_ready" and egs.private_key_ref.endswith("#v2")


def test_at6_pcsid_from_draft_refused(zw):
    keys(zw, zw.tst1, zw.admin1)
    _profile(zw, zw.tst1, zw.admin1)
    egs = _egs(zw, zw.tst1, zw.admin1)
    with pytest.raises(ZatcaError) as exc:
        services.request_production_csid(egs, user=zw.admin1)
    assert exc.value.code == "egs_transition"
    with pytest.raises(ZatcaError):
        services.run_compliance_checks(egs, user=zw.admin1)
    assert not zw.fakes.client.calls


def test_r5_full_path_ends_active_with_one_active_credential_per_kind(zw):
    egs = onboard(zw, zw.tst1, zw.admin1)
    assert egs.status == "active"
    assert EgsCredential.objects.filter(egs_unit=egs, kind="production", status="active").count() == 1
    compliance = EgsCredential.objects.get(egs_unit=egs, kind="compliance")
    assert compliance.status == "superseded" and compliance.replaced_by.kind == "production"


# --- R-10: renewal ---------------------------------------------------------------------------------

def test_at7_renewal_due_30_days_before_and_admins_alerted(zw):
    egs = onboard(zw, zw.tst1, zw.admin1)
    onboard(zw, zw.tst2, zw.admin2)
    credential = EgsCredential.objects.get(egs_unit=egs, kind="production", status="active")
    services.refresh_credential_statuses(user=zw.queue, now=credential.valid_to - timedelta(days=31))
    egs.refresh_from_db()
    assert egs.status == "active"
    services.refresh_credential_statuses(user=zw.queue, now=credential.valid_to - timedelta(days=20))
    egs.refresh_from_db()
    assert egs.status == "renewal_due"
    from zatca.models import ZatcaAlert

    recipients = set(ZatcaAlert.objects.filter(kind="renewal_due", entity=zw.tst1).values_list("recipient__username",
                                                                                                flat=True))
    assert recipients == {"admin1", "admin1b"}


def test_at8_expired_credential_blocks_stamping(zw):
    from zatca.conftest import einvoice_of, issue, run

    egs = onboard(zw, zw.tst1, zw.admin1)
    credential = EgsCredential.objects.get(egs_unit=egs, kind="production", status="active")
    doc = issue(zw, zw.tst1)
    signs_before = zw.fakes.store.calls.count("sign")
    run(zw, now=credential.valid_to + timedelta(minutes=1))
    einv = einvoice_of(doc)
    assert einv.status == "pending" and einv.icv is None and not einv.signed_xml
    assert einv.hold_reason == "egs_not_ready" and einv.messages.filter(code="credential").exists()
    assert zw.fakes.store.calls.count("sign") == signs_before  # nothing signed
    egs.refresh_from_db()
    assert egs.last_icv == 0
    services.refresh_credential_statuses(user=zw.queue, now=credential.valid_to + timedelta(minutes=1))
    egs.refresh_from_db()
    credential.refresh_from_db()
    assert egs.status == "expired" and credential.status == "expired"


def test_at9_renewal_uses_a_new_key_pair(zw):
    egs = onboard(zw, zw.tst1, zw.admin1)
    old = EgsCredential.objects.get(egs_unit=egs, kind="production", status="active")
    old_ref = egs.private_key_ref
    egs = services.renew_production_csid(egs, "246810", user=zw.admin1)
    new = EgsCredential.objects.get(egs_unit=egs, kind="production", status="active")
    old.refresh_from_db()
    assert old.status == "superseded" and old.replaced_by == new
    assert egs.private_key_ref != old_ref and keystore.parse_ref(egs.private_key_ref)[1] == \
        keystore.parse_ref(old_ref)[1] + 1
    old_pub = crypto.cert_from_pem(old.certificate_pem).public_key()
    new_pub = crypto.cert_from_pem(new.certificate_pem).public_key()
    assert crypto.spki_der(old_pub) != crypto.spki_der(new_pub)
    assert EgsCredential.objects.filter(egs_unit=egs, kind="production", status="active").count() == 1
    assert egs.status == "active"
    assert OnboardingEvent.objects.filter(egs_unit=egs, action="pcsid_renewed").exists()


def test_r10_one_active_credential_per_kind_in_the_database(zw):
    from django.db import IntegrityError, transaction

    egs = onboard(zw, zw.tst1, zw.admin1)
    active = EgsCredential.objects.get(egs_unit=egs, kind="production", status="active")
    dup = EgsCredential(entity=zw.tst1, egs_unit=egs, kind="production", environment="sandbox",
                        certificate_pem=active.certificate_pem, secret_ref="x#v1", zatca_request_id="1",
                        valid_from=active.valid_from, valid_to=active.valid_to, created_by=zw.admin1)
    dup._service_write = True
    with pytest.raises(IntegrityError), transaction.atomic():
        dup.save()


# --- R-41, R-50: custody and the key hierarchy ----------------------------------------------------------

def test_at43_delegation_is_per_entity_and_revocation_bites(zw):
    services.register_group_super_key(zw.par, user=zw.mp)
    services.create_entity_master_key(zw.tst1, user=zw.mp)
    services.create_entity_master_key(zw.tst2, user=zw.mp)
    member(zw.admin1, zw.tst2, "zatca_admin")
    d = services.grant_key_delegation(zw.tst1, user=zw.mp, custodian=zw.admin1, scope="onboard_renew",
                                      openbao_policy_ref="zatca-tst1-onboard")
    _profile(zw, zw.tst1, zw.admin1)
    services.create_profile(zw.tst2, user=zw.admin2)
    services.generate_csr(_egs(zw, zw.tst1, zw.admin1), user=zw.admin1)
    egs2 = _egs(zw, zw.tst2, zw.admin2)
    with pytest.raises(access.AccessDenied) as exc:
        services.generate_csr(egs2, user=zw.admin1)
    assert exc.value.code == "no_key_delegation"
    services.revoke_key_delegation(d, user=zw.mp, reason="rotation of staff")
    with pytest.raises(access.AccessDenied):
        services.generate_csr(_egs(zw, zw.tst1, zw.admin1, code="EGS2"), user=zw.admin1)
    assert OnboardingEvent.objects.filter(action="delegation_granted", entity=zw.tst1).exists()
    assert OnboardingEvent.objects.filter(action="delegation_revoked", entity=zw.tst1).exists()
    assert AccessLog.objects.filter(event="delegation_granted", entity=zw.tst1).exists()
    assert AccessLog.objects.filter(event="delegation_revoked", entity=zw.tst1).exists()
    assert "PRIVATE KEY" not in _db_dump()


def test_at71_custody_rules(zw):
    services.register_group_super_key(zw.par, user=zw.mp)
    k = zw.admin1  # ZATCA admin on TST1 only
    services.create_entity_master_key(zw.tst1, user=zw.mp)
    d = services.grant_key_delegation(zw.tst1, user=zw.mp, custodian=k, scope="master_key",
                                      openbao_policy_ref="zatca-tst1-master")
    assert d.entity == zw.tst1
    with pytest.raises(access.AccessDenied) as exc:  # no ZATCA admin on TST2
        services.grant_key_delegation(zw.tst2, user=zw.mp, custodian=k, scope="master_key",
                                      openbao_policy_ref="zatca-tst2-master")
    assert exc.value.code == "custodian_not_zatca_admin"
    with pytest.raises(ZatcaError) as exc2:  # unseal withdrawn (D-12)
        services.grant_key_delegation(zw.par, user=zw.mp, custodian=k, scope="unseal", openbao_policy_ref="x")
    assert exc2.value.code == "scope_withdrawn"
    with pytest.raises(access.AccessDenied):  # Z (a ZATCA admin, not the MP) may not grant
        services.grant_key_delegation(zw.tst1, user=zw.admin1b, custodian=k, scope="revoke", openbao_policy_ref="x")
    services_tst2 = zw.tst2
    with pytest.raises(access.AccessDenied):  # Z may not create a master key either
        services.create_entity_master_key(services_tst2, user=zw.admin2)
    with pytest.raises(access.AccessDenied):  # K's rotation needs the super key (MP only)
        services.rotate_entity_master_key(zw.tst1, user=k)
    mk = services.rotate_entity_master_key(zw.tst1, user=zw.mp)
    assert mk.key_version == 2


def test_r41_agents_and_accountants_never_onboard(zw):
    keys(zw, zw.tst1, zw.admin1)
    _profile(zw, zw.tst1, zw.admin1)
    egs = _egs(zw, zw.tst1, zw.admin1)
    for user in (zw.accountant, zw.agent, zw.cfo):
        with pytest.raises(access.AccessDenied):
            services.generate_csr(egs, user=user)


def test_at70_hierarchy_and_entity_isolation(zw):
    from zatca.conftest import einvoice_of, issue, run

    egs1 = onboard(zw, zw.tst1, zw.admin1)
    onboard(zw, zw.tst2, zw.admin2)
    store = zw.fakes.store
    m1, m2 = services._master_ref(zw.tst1), services._master_ref(zw.tst2)
    raw = b"\x01" * 32
    assert store.sign(egs1.private_key_ref, m1, raw)
    with pytest.raises(keystore.SecretStoreRefused):  # TST1's EGS key under TST2's master key
        store.sign(egs1.private_key_ref, m2, raw)
    store.unavailable_master_paths.add(keystore.parse_ref(m1)[0])
    d1, d2 = issue(zw, zw.tst1), issue(zw, zw.tst2)
    run(zw)
    e1, e2 = einvoice_of(d1), einvoice_of(d2)
    assert e1.status == "pending" and e1.icv is None
    egs1.refresh_from_db()
    assert egs1.last_icv == 0
    assert e2.status == "cleared"  # TST2 keeps signing
    assert OnboardingEvent.objects.filter(entity=zw.tst1, action="secret_store_unavailable").count() == 1
    assert not OnboardingEvent.objects.filter(entity=zw.tst2, action="secret_store_unavailable").exists()
    # TST3: no master key yet -> no CSR
    from invoicing.conftest import make_entity

    tst3 = make_entity("TST3", "full", zw.par, vat="333333333300003", cr="1010000003", short_address="ABCD1234")
    admin3 = make_user("admin3")
    member(admin3, tst3, "zatca_admin")
    services.create_profile(tst3, user=admin3)
    egs3 = _egs(zw, tst3, admin3)
    services.grant_key_delegation(tst3, user=zw.mp, custodian=admin3, scope="onboard_renew",
                                  openbao_policy_ref="zatca-tst3")
    with pytest.raises(ZatcaError) as exc:
        services.generate_csr(egs3, user=admin3)
    assert exc.value.code == "no_master_key"
    services.create_entity_master_key(tst3, user=zw.mp)
    assert services.generate_csr(egs3, user=admin3).status == "csr_ready"
    assert "PRIVATE KEY" not in _db_dump()


# --- R-45, R-36, R-29: VAT status, environments, production -------------------------------------------

def test_at57_vat_deregistration_revokes_only_that_entity(zw):
    from zatca.conftest import einvoice_of, issue, run

    egs1 = onboard(zw, zw.tst1, zw.admin1)
    onboard(zw, zw.tst2, zw.admin2)
    services.record_vat_status_change(zw.tst1, user=zw.admin1, change="deregistered", reason="test")
    egs1.refresh_from_db()
    assert egs1.status == "revoked"
    assert set(EgsCredential.objects.filter(egs_unit=egs1).values_list("status", flat=True)) <= {"revoked", "superseded"}
    assert OnboardingEvent.objects.filter(entity=zw.tst1, action="vat_status_changed").exists()
    d1, d2 = issue(zw, zw.tst1), issue(zw, zw.tst2)
    run(zw)
    assert einvoice_of(d1).icv is None
    assert einvoice_of(d2).status == "cleared"


def test_at38_production_profile_with_sandbox_credential_refused(zw):
    from zatca.conftest import einvoice_of, issue, run

    egs = onboard(zw, zw.tst1, zw.admin1)
    profile = ZatcaProfile.objects.get(entity=zw.tst1)
    profile.environment = "production"
    profile._service_write = True
    profile.save()
    doc = issue(zw, zw.tst1)
    run(zw)
    einv = einvoice_of(doc)
    assert einv.icv is None and einv.messages.filter(code="environment").exists()
    egs.refresh_from_db()
    assert egs.last_icv == 0


def test_r29_production_not_selectable_in_test_settings(zw):
    keys(zw, zw.tst1, zw.admin1)
    profile = _profile(zw, zw.tst1, zw.admin1)
    req = services.request_production_switch(profile, user=zw.admin1)
    access.decide_dual_control(zw.admin1b, req, True)
    with pytest.raises(ZatcaError) as exc:
        services.switch_to_production(profile, user=zw.admin1, request=req)
    assert exc.value.code == "production_blocked"
    profile.refresh_from_db()
    assert profile.environment == "sandbox"


def test_production_switch_needs_a_second_approver(zw, settings):
    settings.ZATCA_PRODUCTION_ALLOWED = True
    keys(zw, zw.tst1, zw.admin1)
    profile = _profile(zw, zw.tst1, zw.admin1)
    req = services.request_production_switch(profile, user=zw.admin1)
    with pytest.raises(access.AccessDenied):  # self-approval
        access.decide_dual_control(zw.admin1, req, True)
    with pytest.raises(access.AccessDenied):  # not yet approved
        services.switch_to_production(profile, user=zw.admin1, request=req)
    access.decide_dual_control(zw.admin1b, req, True)
    profile = services.switch_to_production(profile, user=zw.admin1, request=req)
    assert profile.environment == "production"
    assert OnboardingEvent.objects.filter(entity=zw.tst1, action="environment_changed").exists()


def test_at30_agent_denied_everything_but_status(zw):
    from zatca.conftest import einvoice_of, issue

    egs = onboard(zw, zw.tst1, zw.admin1)
    doc = issue(zw, zw.tst1)
    einv = einvoice_of(doc)
    profile = ZatcaProfile.objects.get(entity=zw.tst1)
    attempts = [
        lambda: services.create_egs_unit(zw.tst1, user=zw.agent, code="X", name_en="x", name_ar="x",
                                         serial_number="1-a|2-b|3-c", industry="x"),
        lambda: services.generate_csr(egs, user=zw.agent),
        lambda: services.enter_otp(egs, "123456", user=zw.agent),
        lambda: services.stamp_document(einv, user=zw.agent),
        lambda: services.send_einvoice(einv, user=zw.agent),
        lambda: services.process_queue(user=zw.agent),
        lambda: services.refresh_credential_statuses(user=zw.agent),
        lambda: services.submit_document(doc, zw.agent),
        lambda: services.retry_now(einv, user=zw.agent),
        lambda: services.request_production_switch(profile, user=zw.agent),
        lambda: services.switch_to_production(profile, user=zw.agent, request=None),
        lambda: services.record_vat_status_change(zw.tst1, user=zw.agent, change="suspended"),
    ]
    for attempt in attempts:
        with pytest.raises(access.AccessDenied):
            attempt()
    statuses = list(services.einvoice_statuses(zw.agent))
    assert statuses == [{"pk": einv.pk, "entity_id": zw.tst1.pk, "source_document_id": doc.pk, "status": "pending"}]
    assert not services.einvoices(zw.agent).exists()


def test_r6_db_and_events_hold_references_only_after_full_onboarding(zw, caplog):
    caplog.set_level(logging.DEBUG)
    onboard(zw, zw.tst1, zw.admin1)
    dump = _db_dump()
    for secret in zw.fakes.client.issued_secrets:
        assert secret not in dump and secret not in caplog.text
    assert "PRIVATE KEY" not in dump and "123456" not in dump
    for event in OnboardingEvent.objects.all():  # references only, e.g. "zatca/TST1/EGS1/..._secret#v1"
        for value in event.detail.values():
            assert not isinstance(value, str) or "#v" in value or len(value) < 64
    assert EntityMasterKey.objects.get(entity=zw.tst1).key_ref == "zatca/TST1/master_key#v1"
