"""Fixes of the verification review of f95e1c8 (2026-10-21): delegates need MFA, access admins neither reset nor set
the password of MFA users, rekey reads ciphertext, recovery-code fingerprints, group-tree scope of the group managing
partner, alerts for people without memberships, SECRET_KEY deploy check, adapter versions on real instances.
(Item 6, the band at issue, is in core/test_authority_matrix.py; item 7, replays, in web/test_mfa.py.)"""
from datetime import timedelta
from importlib import import_module

import pytest
from django.apps import apps as django_apps
from django.db import connection
from django.test import Client
from django.utils import timezone

from core.access import delegations, engine
from core.access import services as access
from core.access.authority_models import AccessAlert
from core.access.models import AccessLog
from core.models import Entity
from web import crypto, mfa
from web.conftest import enrol, next_code, person
from web.models import MfaDevice, RecoveryCode

pytestmark = pytest.mark.django_db


def delegate_to_acc(w):
    return delegations.delegate_authority(w.cfo, entity=w.alm, delegate=w.acc, valid_to=access.now() + timedelta(days=10),
                                          reason_en="Leave", reason_ar="إجازة")


# --- item 1: an active approval delegate must use MFA ---------------------------------------------------------------

def test_item1_an_active_delegate_needs_mfa_and_meets_the_gate(w, client):
    assert not mfa.requires_mfa(w.acc)
    delegation = delegate_to_acc(w)
    assert mfa.requires_mfa(w.acc) and mfa.must_verify(w.acc)
    client.post("/login/", {"username": "acc", "password": "made-up-test-password-7Q!"})
    gate = client.get("/e/ALM/")
    assert gate.status_code == 302 and gate["Location"].startswith("/mfa/enrol/")
    delegations.revoke_delegation(w.cfo, delegation, "back from leave")
    assert not mfa.requires_mfa(w.acc)  # only while the delegation is in its active window


def test_item1_requires_mfa_is_one_query(w, django_assert_num_queries):
    delegate_to_acc(w)
    access.needs_mfa(w.acc)  # loads the profile once (cached on the user)
    with django_assert_num_queries(1):
        assert access.needs_mfa(w.acc)


# --- item 2: admin password of an MFA user ------------------------------------------------------------------------

def test_item2_only_superusers_or_the_group_mp_set_an_mfa_users_password_in_admin(w):
    admin = person("alm_admin", (w.alm, "access_admin"))
    admin.is_staff = True
    admin.save(update_fields=["is_staff"])
    mp = person("mp", (w.alm, "managing_partner"))
    assert not access.may_set_password(admin, w.cfo) and not access.may_set_password(admin, w.hr)
    assert access.may_set_password(mp, w.cfo) and access.may_set_password(w.su, w.cfo)
    assert access.may_set_password(admin, w.viewer)  # no MFA: as before (admin's own rights)
    assert access.may_set_password(w.cfo, w.cfo)  # oneself
    client = Client()
    client.force_login(admin)
    refused = client.get(f"/admin/auth/user/{w.cfo.pk}/password/")
    assert refused.status_code == 403
    row = AccessLog.objects.get(event="permission_denied", actor=admin, permission="admin.set_password")
    assert row.subject_user == w.cfo and row.details["reason"] == "mfa_user_password"
    su = Client()
    su.force_login(w.su)
    assert su.get(f"/admin/auth/user/{w.cfo.pk}/password/").status_code == 200
    posted = Client()
    posted.force_login(admin)
    before = w.cfo.password
    posted.post(f"/admin/auth/user/{w.cfo.pk}/password/", {"password1": "x-New-pass-123!", "password2": "x-New-pass-123!"})
    w.cfo.refresh_from_db()
    assert w.cfo.password == before


# --- item 3: rekey reads the stored ciphertext --------------------------------------------------------------------

def test_item3_rekey_with_one_key_re_encrypts_nothing(w, settings):
    settings.WEB_MFA_KEYS = ["k" * 40]
    enrol(w.cfo)
    enrol(w.su)
    result = mfa.rekey()
    assert result["reencrypted"] == 0 and result["old_recovery_codes"] == 0
    settings.WEB_MFA_KEYS = ["n" * 40, "k" * 40]
    assert mfa.rekey()["reencrypted"] == 2
    assert mfa.rekey()["reencrypted"] == 0  # now all under the first key


# --- item 4: recovery codes made before key_id existed ------------------------------------------------------------

def _backfill():
    import_module("web.migrations.0005_recovery_code_key_id_backfill").backfill(django_apps, None)


def test_item4_backfill_sets_the_fingerprint_only_when_one_key_is_configured(w, settings):
    settings.WEB_MFA_KEYS = ["k" * 40]
    codes = enrol(w.cfo)
    RecoveryCode.objects.filter(user=w.cfo).update(key_id="")  # as web/0004 left existing rows
    settings.WEB_MFA_KEYS = ["k" * 40, "o" * 40]
    _backfill()
    assert set(RecoveryCode.objects.filter(user=w.cfo).values_list("key_id", flat=True)) == {""}  # not certain
    result = mfa.rekey(expire_old_recovery_codes=True)
    assert result["unknown_key_recovery_codes"] == 10 and result["unknown_key_users"] == ["cfo"]
    assert result["old_recovery_codes"] == 0 and not result["expired"]
    assert mfa.use_recovery_code(w.cfo, codes[0])  # never auto-expired
    settings.WEB_MFA_KEYS = ["k" * 40]
    _backfill()
    assert set(RecoveryCode.objects.filter(user=w.cfo).values_list("key_id", flat=True)) == {crypto.key_id()}
    assert mfa.rekey(expire_old_recovery_codes=True)["old_recovery_codes"] == 0
    assert mfa.use_recovery_code(w.cfo, codes[1])


# --- item 5: the group MP acts within their own group tree --------------------------------------------------------

def test_item5_a_managing_partner_of_another_group_resets_nobody_here(w):
    other = Entity.objects.create(code="OTHG", name_en="Other group", name_ar="مجموعة أخرى", consolidation="parent",
                                  parent=None)
    stranger = person("other_mp", (other, "managing_partner"))
    group_mp = person("mp", (w.alm, "managing_partner"))
    assert not mfa.may_reset(stranger, w.cfo) and not access.may_set_password(stranger, w.cfo)
    assert mfa.may_reset(group_mp, w.cfo)  # cfo: ALM only, in ALM's tree
    outsider = person("both", (w.alm, "accountant"), (other, "accountant"))
    assert not mfa.may_reset(group_mp, outsider)  # a membership outside ALM's tree


# --- item 8: a lock alert for a person without memberships --------------------------------------------------------

def test_item8_second_lock_of_a_person_without_memberships_alerts_the_operators(w, mfa_clock):
    operator = person("operator")
    operator.is_superuser = operator.is_staff = True
    operator.save(update_fields=["is_superuser", "is_staff"])
    enrol(w.su)
    for _ in range(2):
        for _ in range(5):
            mfa_clock.tick(60)
            with pytest.raises(mfa.MfaError):
                mfa.verify(w.su, "123456")
        mfa_clock.tick(24 * 3600)
    alerts = AccessAlert.objects.filter(kind="mfa_locked")
    assert set(alerts.values_list("recipient_id", flat=True)) == {operator.pk, w.su.pk}
    assert set(alerts.values_list("entity__code", flat=True)) == {"ALM"}  # the group parent
    assert AccessLog.objects.filter(event="access_alert_sent").count() == alerts.count()


# --- item 9: SECRET_KEY in production -------------------------------------------------------------------------------

def test_item9_e003_refuses_the_dev_or_a_short_secret_key(settings):
    from web.checks import secret_key_strong

    settings.DEBUG = False
    for key in ("dev-only-change-me", "x" * 49, "django-insecure-" + "y" * 60):
        settings.SECRET_KEY = key
        assert secret_key_strong()[0].id == "web.E003", key
    settings.SECRET_KEY = "R" * 64
    assert secret_key_strong() == []
    settings.DEBUG, settings.SECRET_KEY = True, "dev-only-change-me"
    assert secret_key_strong() == []


def test_item9_the_deploy_stack_generates_a_long_secret_key():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    init = (root / "deploy/init-secrets.sh").read_text(encoding="utf-8")
    assert "django_secret_key" in init and "head -c 64 /dev/urandom" in init  # ~86 characters
    assert "DJANGO_SECRET_KEY_FILE: /run/secrets/django_secret_key" in (root / "deploy/docker-compose.yml").read_text()
    assert "init-secrets.sh" in (root / ".github/workflows/deploy-check.yml").read_text()


# --- item 10: every adapter gives a real instance a non-empty version ---------------------------------------------

def _instance(label):
    model = django_apps.get_model(label)
    obj = model()
    obj.pk = 999_999
    now = timezone.now()
    for field in model._meta.concrete_fields:
        if field.get_internal_type() == "DateTimeField" and getattr(obj, field.attname) is None:
            setattr(obj, field.attname, now)
        elif field.get_internal_type() == "DateField" and getattr(obj, field.attname) is None:
            setattr(obj, field.attname, now.date())
        elif field.name in ("stored_hash", "inputs_hash") and not getattr(obj, field.attname):
            setattr(obj, field.attname, "h" * 16)
        elif field.name == "version" and not getattr(obj, field.attname):
            setattr(obj, field.attname, 1)
    return obj


def test_item10_every_adapter_gives_a_submitted_instance_a_non_empty_version():
    for (label, activity), adapter in sorted(engine.adapters().items()):
        assert engine.document_version(adapter, _instance(label)), (label, activity)
