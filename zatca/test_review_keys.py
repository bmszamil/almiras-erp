"""Review of 838e89b, findings 1, 6, 14, 16: master-key rotation (probe p1), custody, the local secret store
and key-reference guards."""
from datetime import timedelta

import pytest
from django.core.checks import run_checks
from django.core.exceptions import ImproperlyConfigured

from core import access
from core.access.testing import make_user, member
from zatca import checks, crypto, keystore, services
from zatca.conftest import NOW, einvoice_of, issue, onboard, run, xml_text
from zatca.messages import ZatcaError
from zatca.models import EgsCredential, EgsUnit, EntityMasterKey, GroupSuperKey, OnboardingEvent

pytestmark = pytest.mark.django_db


# --- finding 1 (probe p1): master-key rotation --------------------------------------------------------------

def test_f1_rotation_then_sign_submit_renew_and_sign_again(zw):
    egs = onboard(zw, zw.tst1, zw.admin1)
    store = zw.fakes.store
    secret_ref = EgsCredential.objects.get(egs_unit=egs, kind="production", status="active").secret_ref
    mk = services.rotate_entity_master_key(zw.tst1, user=zw.mp)
    assert mk.key_version == 2 and OnboardingEvent.objects.filter(entity=zw.tst1, action="master_key_rotated").exists()
    # the store re-wrapped the EGS key and the CSID secret under the new version; DB references unchanged
    egs.refresh_from_db()
    assert store.protecting_version(egs.private_key_ref) == 2 and store.protecting_version(secret_ref) == 2
    standard, simplified = issue(zw, zw.tst1), issue(zw, zw.tst1, simplified=True)
    run(zw)
    assert einvoice_of(standard).status == "cleared" and einvoice_of(simplified).status == "reported"
    egs = services.renew_production_csid(egs, "246810", user=zw.admin1)
    assert store.protecting_version(egs.private_key_ref) == 2
    services.rotate_entity_master_key(zw.tst1, user=zw.mp)  # a second rotation after the renewal
    after = issue(zw, zw.tst1)
    run(zw, now=NOW + timedelta(minutes=5))
    einv = einvoice_of(after)
    assert einv.status == "cleared" and einv.icv == 3
    new_cert = EgsCredential.objects.get(egs_unit=egs, kind="production", status="active")
    assert xml_text(einv.signed_xml, "//ds:X509Certificate") == crypto.cert_base64(
        crypto.cert_from_pem(new_cert.certificate_pem))


def test_f1_item_opens_under_its_own_version_and_never_another_entitys(zw):
    onboard(zw, zw.tst1, zw.admin1)
    onboard(zw, zw.tst2, zw.admin2)
    store = zw.fakes.store
    old_master = services._master_ref(zw.tst1)
    egs = EgsUnit.objects.get(entity=zw.tst1)
    services.rotate_entity_master_key(zw.tst1, user=zw.mp)
    # a signer still holding the old version (in flight during the rotation) opens the re-wrapped item
    assert store.sign(egs.private_key_ref, old_master, b"\x00" * 32)
    with pytest.raises(keystore.SecretStoreRefused):  # R-50 a: another entity's master key never opens it
        store.sign(egs.private_key_ref, services._master_ref(zw.tst2), b"\x00" * 32)


def test_f14_rotation_with_the_store_unavailable_changes_nothing(zw):
    onboard(zw, zw.tst1, zw.admin1)
    zw.fakes.store.sealed = True
    with pytest.raises(ZatcaError) as exc:
        services.rotate_entity_master_key(zw.tst1, user=zw.mp)
    assert exc.value.code == "secret_store_unavailable" and exc.value.ar
    zw.fakes.store.sealed = False
    assert EntityMasterKey.objects.get(entity=zw.tst1).key_version == 1
    assert not OnboardingEvent.objects.filter(entity=zw.tst1, action="master_key_rotated").exists()


def test_f14_only_the_super_key_holder_creates_or_rotates_master_keys(zw):
    services.register_group_super_key(zw.par, user=zw.mp)
    mp2 = make_user("mp2")
    member(mp2, zw.par, "managing_partner")
    with pytest.raises(access.AccessDenied):
        services.create_entity_master_key(zw.tst2, user=mp2)
    assert not EntityMasterKey.objects.filter(entity=zw.tst2).exists()
    services.create_entity_master_key(zw.tst2, user=zw.mp)
    with pytest.raises(access.AccessDenied):
        services.rotate_entity_master_key(zw.tst2, user=mp2)
    assert EntityMasterKey.objects.get(entity=zw.tst2).key_version == 1


def test_f16_key_reference_guards_in_the_model(zw):
    services.register_group_super_key(zw.par, user=zw.mp)
    mk = services.create_entity_master_key(zw.tst1, user=zw.mp)
    sk = GroupSuperKey.objects.get(entity=zw.par)
    for obj, change in ((sk, dict(key_ref="zatca/_group/PAR/super_key#v9")),  # new ref without a version bump
                        (sk, dict(key_version=2)),  # new version, same ref
                        (mk, dict(key_version=2))):
        fresh = type(obj).objects.get(pk=obj.pk)
        for name, value in change.items():
            setattr(fresh, name, value)
        fresh._service_write = True
        with pytest.raises(ZatcaError):
            fresh.save()


# --- finding 6: the local secret store never runs outside development -----------------------------------------

@pytest.mark.parametrize("debug, production, refused", [(False, False, True), (True, True, True),
                                                         (False, True, True), (True, False, False)])
def test_f6_system_check_refuses_the_local_store(settings, debug, production, refused):
    settings.ZATCA_SECRET_STORE = "zatca.keystore.LocalSecretStore"
    settings.DEBUG, settings.ZATCA_PRODUCTION_ALLOWED = debug, production
    errors = checks.check_secret_store()
    assert [e.id for e in errors] == (["zatca.E001"] if refused else [])
    assert [e.id for e in run_checks(tags=["zatca"])] == (["zatca.E001"] if refused else [])
    previous = keystore.override(None)
    try:
        if refused:
            with pytest.raises(ImproperlyConfigured):
                keystore.get_store()
        else:
            assert isinstance(keystore.get_store(), keystore.LocalSecretStore)
    finally:
        keystore.override(previous)
        keystore._instances.clear()


def test_f6_openbao_store_passes_the_check(settings):
    settings.ZATCA_SECRET_STORE = "zatca.keystore.OpenBaoSecretStore"
    settings.DEBUG, settings.ZATCA_PRODUCTION_ALLOWED = False, True
    assert checks.check_secret_store() == []
