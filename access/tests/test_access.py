"""Access rules: R1-R3, R18 (permission side), R19 (append-only), D12, D13, D14."""
import datetime

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django_otp.oath import totp
from django_otp.plugins.otp_totp.models import TOTPDevice

from access import services as access
from access.catalogue import ROLES
from access.errors import AccessDenied, BilingualError, RuleError
from access.models import AuditLog, EntityAccess, Role

pytestmark = pytest.mark.django_db


def test_custom_user_model_is_active():
    assert get_user_model()._meta.label == "access.User"


def test_starting_roles_seeded_by_migration():
    codes = set(Role.objects.values_list("code", flat=True))
    assert codes >= {"managing_partner", "chairman", "cfo", "accountant", "business_analyst",
                     "pa_drafter", "auditor", "viewer", "agent_drafter"}
    assert all(Role.objects.get(code=c).is_system for c in ROLES)


def test_system_role_cannot_be_deleted():
    with pytest.raises(BilingualError):
        Role.objects.get(code="viewer").delete()


def test_d12_role_in_one_entity_gives_nothing_in_another(make_user, alm, sub):
    u = make_user("u", (alm, "managing_partner"))
    assert access.has_perm(u, alm, "approvals.request.act")
    assert access.permissions_for(u, sub) == set()


def test_r2_permissions_are_union_of_roles_in_that_entity_only(make_user, alm, sub):
    u = make_user("u", (alm, "viewer"), (alm, "accountant"), (sub, "auditor"))
    perms = access.permissions_for(u, alm)
    assert "approvals.request.submit" in perms and "access.audit.view" not in perms
    assert "access.audit.view" in access.permissions_for(u, sub)


def test_d13_business_analyst_sees_only_investments_deals_reports(make_user, alm):
    u = make_user("analyst", (alm, "business_analyst"))
    modules = {p.split(".")[0] for p in access.permissions_for(u, alm)}
    assert modules == {"core", "investments", "deals", "reports"}
    assert not any(p.endswith((".create", ".act", ".post", ".submit")) for p in access.permissions_for(u, alm))


def test_d13_pa_drafts_and_uploads_but_never_approves_or_posts(make_user, alm):
    perms = access.permissions_for(make_user("faisal", (alm, "pa_drafter")), alm)
    assert {"ledger.entry.create", "documents.document.upload", "approvals.request.submit"} <= perms
    assert "approvals.request.act" not in perms and "ledger.entry.post" not in perms


def test_d13_auditor_is_read_only(make_user, alm):
    perms = access.permissions_for(make_user("auditor", (alm, "auditor")), alm)
    assert "access.audit.view" in perms
    assert all(p.endswith((".view",)) for p in perms)


def test_r18_agent_permissions_stripped_even_if_role_grants_them(make_user, alm, agent):
    # Even a manually crafted grant of a powerful role cannot give an agent act/post rights.
    from access.models import Permission

    role = Role.objects.get(code="agent_drafter")

    role.permissions.add(*Permission.objects.filter(code__in=["approvals.request.act", "ledger.entry.post"]))
    perms = access.permissions_for(agent, alm)
    assert "approvals.request.act" not in perms and "ledger.entry.post" not in perms


def test_r18_agent_cannot_be_granted_managing_partner(make_user, alm, agent, mp):
    with pytest.raises(RuleError):
        access.grant_access(mp, agent, alm, "managing_partner")


def test_expired_or_revoked_access_gives_nothing(make_user, alm, mp):
    u = make_user("temp")
    g = access.grant_access(mp, u, alm, "viewer", valid_from=datetime.date(2020, 1, 1), valid_to=datetime.date(2020, 12, 31))
    assert not access.has_perm(u, alm, "core.entity.view")
    g = access.grant_access(mp, u, alm, "viewer", valid_from=datetime.date(2020, 1, 1))
    assert access.has_perm(u, alm, "core.entity.view")
    access.revoke_access(mp, g)
    assert not access.has_perm(u, alm, "core.entity.view")


def test_only_entity_access_managers_can_grant(make_user, alm, accountant):
    u = make_user("x")
    with pytest.raises(AccessDenied):
        access.grant_access(accountant, u, alm, "viewer")


def test_grant_is_audited_with_after(alm, mp, make_user):
    u = make_user("x")
    g = access.grant_access(mp, u, alm, "viewer")
    row = AuditLog.objects.get(action="access.grant", object_id=str(g.pk))
    assert row.actor == mp and row.entity == alm and row.after["role_id"] == g.role_id


def test_r1_scope_filters_by_entity(make_user, alm, sub):
    u = make_user("u", (alm, "accountant"))
    assert list(access.scope(EntityAccess.objects.all(), u, "core.entity.view")) == list(EntityAccess.objects.filter(entity=alm))


def test_r3_user_with_audit_cannot_be_deleted_only_deactivated(make_user, alm, mp):
    u = make_user("leaver", (alm, "viewer"))
    access.update_profile(u, language="ar")  # u now has an audit row
    with pytest.raises(BilingualError):
        u.delete()
    access.deactivate_user(mp, u)
    u.refresh_from_db()
    assert u.is_active is False
    assert not access.has_perm(u, alm, "core.entity.view")


def test_t18_audit_rows_cannot_be_updated_or_deleted(mp, alm):
    row = access.audit(mp, "test.action", alm)
    row.action = "changed"
    with pytest.raises(BilingualError):
        row.save()
    with pytest.raises(BilingualError):
        row.delete()
    with pytest.raises(BilingualError):
        AuditLog.objects.filter(pk=row.pk).update(action="x")
    with pytest.raises(BilingualError):
        AuditLog.objects.all().delete()
    assert AuditLog.objects.get(pk=row.pk).action == "test.action"


def test_agent_source_is_agent_name(agent):
    assert agent.source == "agent:invoice-reader"


# ---------------------------------------------------------------- D14 two-factor


def _admin_user(make_user, alm):
    u = make_user("boss", (alm, "managing_partner"))
    u.is_staff = u.is_superuser = True
    u.set_password("a-Long-test-pass-91")
    u.save()
    return u


def test_d14_admin_login_without_otp_token_is_refused(make_user, alm):
    u = _admin_user(make_user, alm)
    TOTPDevice.objects.create(user=u, name="app", confirmed=True)
    c = Client()
    r = c.post("/admin/login/", {"username": "boss", "password": "a-Long-test-pass-91", "next": "/admin/"})
    assert r.status_code == 200  # form re-shown with error, not redirected
    assert c.get("/admin/").status_code == 302


def test_d14_admin_login_without_device_is_refused(make_user, alm):
    _admin_user(make_user, alm)
    c = Client()
    c.post("/admin/login/", {"username": "boss", "password": "a-Long-test-pass-91", "otp_token": "123456"})
    assert c.get("/admin/").status_code == 302


def test_d14_admin_login_with_password_and_totp_succeeds_and_is_audited(make_user, alm):
    u = _admin_user(make_user, alm)
    device = TOTPDevice.objects.create(user=u, name="app", confirmed=True)
    token = str(totp(device.bin_key, step=device.step, t0=device.t0, digits=device.digits)).zfill(device.digits)
    c = Client()
    r = c.post("/admin/login/", {"username": "boss", "password": "a-Long-test-pass-91", "otp_device": device.persistent_id,
                                 "otp_token": token, "next": "/admin/"})
    assert r.status_code == 302
    assert c.get("/admin/").status_code == 200
    assert AuditLog.objects.filter(action="access.sign_in", actor=u).exists()


def test_d14_api_refuses_password_only_session(make_user, alm):
    u = make_user("u", (alm, "viewer"))
    c = Client()
    c.force_login(u)  # password session, no second factor
    r = c.get("/api/v1/me")
    assert r.status_code == 403
    body = r.json()
    assert body["message_en"] and body["message_ar"]


def test_d14_api_accepts_verified_session(make_user, alm, api):
    u = make_user("u", (alm, "viewer"))
    r = api(u).get("/api/v1/me")
    assert r.status_code == 200
    assert r.json()["entities"][0]["code"] == "ALM"


def test_r4_service_token_auth_and_revocation(agent, api):
    c = api(agent)
    assert c.get("/api/v1/me").status_code == 200
    for t in agent.service_tokens.all():
        access.revoke_service_token(None, t)
    assert c.get("/api/v1/me").status_code in (401, 403)


def test_r4_service_user_cannot_use_password_session(agent):
    c = Client()
    c.force_login(agent)
    assert c.get("/api/v1/me").status_code == 403


def test_identity_seam_is_configurable(settings):
    from access.identity import LocalTotpIdentity, identity

    assert isinstance(identity(), LocalTotpIdentity)
    assert settings.ERP_IDENTITY_ADAPTER == "access.identity.LocalTotpIdentity"
