"""D21 final review: passwords set by others, Chairman grant under dual control, and credential reset when
the Managing Partner or Chairman role is granted (any path, incl. operator bootstrap)."""
import re

import pytest
from django.contrib.auth import authenticate
from django.core.management import call_command
from django.test import Client, RequestFactory
from django_otp.oath import TOTP
from django_otp.plugins.otp_totp.models import TOTPDevice

from access import services as access
from access.errors import AccessDenied, RuleError
from access.models import AuditLog, ChairmanChange, Permission, Role, User

pytestmark = pytest.mark.django_db
A = ChairmanChange.Action
ENROL = "/access/enrol-2fa/"
UA_PW = "Set-by-user-manager-1"
OWN_PW = "Chosen-by-myself-2"
INIT_PW = "Initial-from-approvers-3"


def code(device, step_offset=0):
    device.refresh_from_db()
    t = TOTP(device.bin_key, step=device.step, t0=device.t0, digits=device.digits)
    t.time = device.t0 + (max(device.last_t + 1, int(t.t())) + step_offset) * device.step
    return f"{t.token():0{device.digits}d}"


def rf():
    r = RequestFactory().post("/admin/login/")
    r.META["REMOTE_ADDR"] = "127.0.0.1"
    return r


def can_sign_in(username, password):
    return authenticate(rf(), username=username, password=password) is not None


@pytest.fixture
def cfo(make_user, alm):
    return make_user("cfo", (alm, "cfo"))


@pytest.fixture
def user_manager(make_user, alm):
    role = Role.objects.create(code="user_admin_test", name_en="User admin", name_ar="مدير مستخدمين")
    role.permissions.add(*Permission.objects.filter(code__in=["access.user.manage", "access.entity_access.manage",
                                                               "core.entity.view"]))
    return make_user("ua", (alm, "user_admin_test"))


def self_enrol(username, password, new_password=OWN_PW, code_=""):
    """Run the enrolment page as the user: prove password, set own password if asked, enrol. Returns device."""
    c = Client()
    resp = c.post(ENROL, {"username": username, "password": password, "code": code_})
    assert "no-store" in resp["Cache-Control"]
    if "new_password1" in resp.content.decode():
        resp = c.post(ENROL, {"new_password1": new_password, "new_password2": new_password})
    assert "otpauth://" in resp.content.decode(), resp.content.decode()[:500]
    device = TOTPDevice.objects.get(user__username=username, confirmed=False)
    assert "enrolled" in c.post(ENROL, {"token": code(device)}).content.decode()
    return TOTPDevice.objects.get(pk=device.pk)


def grant_chairman_dual(mp, cfo, user, entity_code="ALM"):
    change = access.request_chairman_change(mp, A.GRANT, target=user, payload={"role": "chairman",
                                                                               "entity": entity_code})
    access.approve_chairman_change(cfo, change)
    assert ChairmanChange.objects.get(pk=change.pk).status == "applied"


# ---------------------------------------------------------------- passwords set by others


def test_every_password_set_by_others_is_flagged_and_only_self_clears(alm, user_manager, mp):
    u = access.create_user(user_manager, username="x", full_name_en="X", full_name_ar="س", password=UA_PW)
    assert u.password_set_by_others is True
    assert User(username="new").password_set_by_others is True  # default for new users
    u.password_set_by_others = False
    u.set_password("Another-one-4")  # any set_password: admin forms, changepassword command, setup mode
    assert u.password_set_by_others is True
    u.save()
    access.password_changed_by_self(u)
    assert User.objects.get(pk=u.pk).password_set_by_others is False
    assert u.check_password("Another-one-4") and not User.objects.get(pk=u.pk).password_set_by_others


def test_admin_password_change_for_another_user_flags_it(alm, user_manager, make_user, api):
    acc = make_user("acc", (alm, "accountant"))
    user_manager.is_staff = True
    user_manager.save()
    c = api(user_manager)
    assert c.post(f"/admin/access/user/{acc.pk}/password/",
                  {"password1": UA_PW, "password2": UA_PW, "usable_password": "true"}).status_code == 302
    assert User.objects.get(pk=acc.pk).password_set_by_others is True
    # The user's own change through /admin/password_change/ clears it.
    acc.refresh_from_db()  # keep the new password hash
    acc.is_staff = True
    acc.save()
    own = api(acc)
    resp = own.post("/admin/password_change/", {"old_password": UA_PW, "new_password1": OWN_PW, "new_password2": OWN_PW})
    assert resp.status_code == 302, re.findall(r'errorlist[^<]*<li>([^<]*)', resp.content.decode())
    assert User.objects.get(pk=acc.pk).password_set_by_others is False


# ---------------------------------------------------------------- scenario (a)


def test_scenario_a_user_manager_initial_password_never_yields_chairman_2fa(alm, mp, cfo, user_manager):
    x = access.create_user(user_manager, username="x", full_name_en="X", full_name_ar="س", password=UA_PW)
    grant_chairman_dual(mp, cfo, x)
    x.refresh_from_db()
    # The grant made the old password worthless and removed every device.
    assert not x.has_usable_password() and x.totp_enrolment_required and x.password_set_by_others
    assert not can_sign_in("x", UA_PW)
    assert "otpauth://" not in Client().post(ENROL, {"username": "x", "password": UA_PW}).content.decode()
    # Credentials are re-established only by CFO + MP: initial password, then X sets his own and enrols.
    change = access.request_chairman_change(mp, A.PASSWORD, target=x, payload={"password": INIT_PW})
    _, one_time = access.approve_chairman_change(cfo, change)
    assert not can_sign_in("x", INIT_PW)  # sign-in refused until he set his own password and enrolled
    device = self_enrol("x", INIT_PW, code_=one_time)
    x.refresh_from_db()
    assert not x.totp_enrolment_required and not x.password_set_by_others and device.confirmed
    assert can_sign_in("x", OWN_PW) and not can_sign_in("x", INIT_PW)
    # Later 2FA reset: the user manager's initial password still opens nothing.
    access.approve_chairman_change(cfo, access.request_chairman_change(mp, A.TOTP_RESET, target=x))
    assert "otpauth://" not in Client().post(ENROL, {"username": "x", "password": UA_PW}).content.decode()
    assert not TOTPDevice.objects.filter(user=x).exists()


def test_scenario_a_reset_refused_while_chairman_still_has_a_password_others_set(alm, mp, cfo, make_user):
    x = make_user("x", (alm, "chairman"))
    x.set_password(UA_PW)  # e.g. given by a user manager and never changed
    x.save()
    with pytest.raises(RuleError) as e:
        access.request_chairman_change(mp, A.TOTP_RESET, target=x)
    assert e.value.code == "factors_apart"


# ---------------------------------------------------------------- scenario (b)


def test_scenario_b_ordinary_users_credentials_die_when_made_chairman(alm, mp, cfo, chairman, user_manager, make_user):
    x = make_user("x", (alm, "accountant"))
    # While X is ordinary, the user manager sets his password and enrols his device (receiving the secret).
    x.set_password(UA_PW)
    x.save()
    device = access.enroll_totp(user_manager, x)
    assert device.user == x
    # A single Chairman (or MP, or user manager) can no longer make X a Chairman.
    for actor in (chairman, mp, user_manager):
        with pytest.raises(AccessDenied) as e:
            access.grant_access(actor, x, alm, "chairman")
        assert e.value.code == "chairman_role_protected"
    assert not access.has_role(x, alm, "chairman")
    grant_chairman_dual(mp, cfo, x)
    x.refresh_from_db()
    assert not TOTPDevice.objects.filter(user=x).exists() and not x.has_usable_password()
    assert not can_sign_in("x", UA_PW)
    assert "otpauth://" not in Client().post(ENROL, {"username": "x", "password": UA_PW}).content.decode()
    assert AuditLog.objects.filter(action="access.totp.reset", object_id=str(x.pk),
                                   after__reason="role_granted:chairman").exists()


def test_chairman_grant_target_cannot_approve_own_promotion(alm, mp, make_user):
    x = make_user("x", (alm, "cfo"))
    with pytest.raises(AccessDenied):
        access.request_chairman_change(x, A.GRANT, target=x, payload={"role": "chairman", "entity": "ALM"})
    change = access.request_chairman_change(mp, A.GRANT, target=x, payload={"role": "chairman", "entity": "ALM"})
    with pytest.raises(AccessDenied):
        access.approve_chairman_change(x, change)
    assert not access.has_role(x, alm, "chairman")


# ---------------------------------------------------------------- MP grant reset


def test_mp_grant_resets_credentials_and_chairman_reestablishes_them(alm, chairman, user_manager, make_user, api):
    x = make_user("x", (alm, "accountant"))
    x.set_password(UA_PW)
    x.save()
    access.enroll_totp(user_manager, x)
    access.grant_access(user_manager, x, alm, "managing_partner")
    x.refresh_from_db()
    assert not TOTPDevice.objects.filter(user=x).exists() and not x.has_usable_password()
    assert x.totp_enrolment_required and not can_sign_in("x", UA_PW)
    with pytest.raises(AccessDenied):
        access.enroll_totp(user_manager, x)  # X is MP now: D19
    with pytest.raises(RuleError):
        access.enroll_totp(chairman, x)  # nobody enrols for him: he must do it himself
    chairman.is_staff = True
    chairman.save()
    c = api(chairman)
    assert c.post(f"/admin/access/user/{x.pk}/password/",
                  {"password1": INIT_PW, "password2": INIT_PW, "usable_password": "true"}).status_code == 302
    self_enrol("x", INIT_PW)
    assert can_sign_in("x", OWN_PW) and not can_sign_in("x", INIT_PW)


def test_api_refused_while_enrolment_pending(alm, make_user, api):
    x = make_user("x", (alm, "viewer"))
    c = api(x)
    assert c.get("/api/v1/me").status_code == 200
    User.objects.filter(pk=x.pk).update(totp_enrolment_required=True)
    assert c.get("/api/v1/me").status_code == 403


def test_regrant_of_an_active_role_does_not_reset_again(alm, mp):
    TOTPDevice.objects.create(user=mp, name="phone", confirmed=True)
    access.grant_access(None, mp, alm, "managing_partner")  # still held: only dates refreshed
    assert TOTPDevice.objects.filter(user=mp).exists()


# ---------------------------------------------------------------- operator bootstrap


def test_operator_bootstrap_first_mp_must_self_set_password_and_self_enrol(alm):
    from django.core.management.base import CommandError

    u = access.create_user(None, username="first-mp", full_name_en="First", full_name_ar="الأول", password=UA_PW)
    call_command("grant_access", "first-mp", "ALM", "managing_partner")
    u.refresh_from_db()
    assert u.totp_enrolment_required and not u.has_usable_password()
    with pytest.raises((CommandError, RuleError)):
        call_command("setup_totp", "first-mp")  # operator cannot enrol for him any more
    assert not TOTPDevice.objects.filter(user=u, confirmed=True).exists()
    u.set_password(INIT_PW)  # what `manage.py changepassword first-mp` does
    u.save()
    assert u.password_set_by_others and not can_sign_in("first-mp", INIT_PW)
    self_enrol("first-mp", INIT_PW)
    assert can_sign_in("first-mp", OWN_PW)
    u.refresh_from_db()
    assert not u.password_set_by_others and not u.totp_enrolment_required
