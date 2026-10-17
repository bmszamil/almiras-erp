"""D21 follow-up (MP decisions 17 Oct 2026): (a) Chairman self-service; (b) a dual-controlled 2FA reset never
reveals a secret to anyone; hardened apply path; revoke choices; role loss before the second approval."""
import pytest
from django.conf import settings
from django.contrib.admin.sites import site
from django.core.exceptions import PermissionDenied
from django.test import Client, RequestFactory
from django_otp.oath import totp
from django_otp.plugins.otp_totp.models import TOTPDevice

from access import services as access
from access.admin import ChairmanChangeForm
from access.errors import AccessDenied, RuleError
from access.models import AuditLog, ChairmanChange, EntityAccess, User

pytestmark = pytest.mark.django_db
A = ChairmanChange.Action
PW = "Chairman-own-passphrase-1"
SET_PW = "Set-by-approvers-passphrase-2"
ENROL = "/access/enrol-2fa/"


def req(user):
    r = RequestFactory().get("/admin/")
    r.user = user
    r.META["REMOTE_ADDR"] = "127.0.0.1"
    return r


def next_code(device):
    """Code for the step after the last one used (inside the accepted window even across a step boundary)."""
    from django_otp.oath import TOTP

    device.refresh_from_db()
    t = TOTP(device.bin_key, step=device.step, t0=device.t0, digits=device.digits)
    t.time = device.t0 + (device.last_t + 1) * device.step
    return f"{t.token():0{device.digits}d}"


def code(device, drift=0):
    value = totp(device.bin_key, step=device.step, t0=device.t0, digits=device.digits, drift=drift)
    return f"{value:0{device.digits}d}"


@pytest.fixture
def cfo(make_user, alm):
    return make_user("cfo", (alm, "cfo"))


@pytest.fixture
def chairman(make_user, alm):
    u = make_user("chairman", (alm, "chairman"))
    u.set_password(PW)
    u.is_staff = True
    u.save()
    TOTPDevice.objects.create(user=u, name="old phone", confirmed=True)
    return u


@pytest.fixture
def admin_for(api):
    def make(user):
        user.is_staff = True
        user.save()
        return api(user)

    return make


def dual(mp, cfo, action, target, payload=None):
    change = access.request_chairman_change(mp, action, target=target, payload=payload)
    access.approve_chairman_change(cfo, change)
    change.refresh_from_db()
    return change


def all_text(response):
    return response.content.decode() + str(response.cookies) + str(dict(response.headers))


# ---------------------------------------------------------------- (a) self-service


def test_a_chairman_changes_own_profile_and_password(alm, chairman, admin_for):
    c = admin_for(chairman)
    data = {"username": "chairman", "full_name_en": "Chair Self", "full_name_ar": "رئيس", "email": "own@example.test",
            "language": "ar", "default_entity": "", "_save": "Save"}
    assert c.post(f"/admin/access/user/{chairman.pk}/change/", data).status_code == 302
    chairman.refresh_from_db()
    assert (chairman.full_name_en, chairman.email, chairman.language) == ("Chair Self", "own@example.test", "ar")
    assert chairman.is_active and chairman.is_staff  # read-only privilege fields were not touched
    resp = c.post(f"/admin/access/user/{chairman.pk}/password/",
                  {"password1": "Brand-new-own-pass-3", "password2": "Brand-new-own-pass-3", "usable_password": "true"})
    assert resp.status_code == 302
    assert User.objects.get(pk=chairman.pk).check_password("Brand-new-own-pass-3")
    assert AuditLog.objects.filter(action="access.user.password_change", actor=chairman,
                                   after__self_service=True).exists()
    assert access.enroll_totp(chairman, chairman, name="new phone").user == chairman  # own device, signed in


def test_a_self_service_never_covers_roles_privileges_or_active_status(alm, chairman):
    ma = site._registry[User]
    for field, value in (("is_active", False), ("is_staff", False), ("is_superuser", True),
                         ("totp_enrolment_required", True), ("password_set_by_others", True)):
        me = User.objects.get(pk=chairman.pk)
        setattr(me, field, value)
        with pytest.raises(PermissionDenied):
            ma.save_model(req(chairman), me, form=None, change=True)
    assert {"is_active", "is_staff", "is_superuser"} <= set(ma.get_readonly_fields(req(chairman), chairman))
    for call in (lambda: access.grant_access(chairman, chairman, alm, "managing_partner"),
                 lambda: access.revoke_access(chairman, EntityAccess.objects.get(user=chairman)),
                 lambda: access.deactivate_user(chairman, chairman),
                 lambda: access.request_chairman_change(chairman, A.UPDATE, target=chairman,
                                                        payload={"fields": {"is_staff": False}})):
        with pytest.raises(AccessDenied):
            call()
    chairman.refresh_from_db()
    assert chairman.is_active and chairman.is_staff and not chairman.is_superuser


# ---------------------------------------------------------------- (b) 2FA reset reveals no secret


def test_b_reset_via_admin_shows_no_secret_to_approvers(alm, mp, cfo, chairman, admin_for):
    change = access.request_chairman_change(mp, A.TOTP_RESET, target=chairman)
    c = admin_for(cfo)
    resp = c.post("/admin/access/chairmanchange/", {"action": "approve_selected", "_selected_action": [change.pk]},
                  follow=True)
    assert resp.status_code == 200
    assert "otpauth" not in all_text(resp) and "secret" not in all_text(resp).lower()
    assert ChairmanChange.objects.get(pk=change.pk).status == "applied"
    assert not TOTPDevice.objects.filter(user=chairman).exists()
    assert User.objects.get(pk=chairman.pk).totp_enrolment_required
    for client in (c, admin_for(mp)):
        assert "otpauth" not in client.get(f"/admin/access/chairmanchange/{change.pk}/change/").content.decode()
    for row in AuditLog.objects.all():
        assert "otpauth" not in str(row.after) and "key" not in (row.after or {})


def test_b_messages_are_stored_in_the_session_not_a_cookie(alm, mp, cfo, chairman, admin_for):
    assert settings.MESSAGE_STORAGE == "django.contrib.messages.storage.session.SessionStorage"
    change = access.request_chairman_change(mp, A.UNLOCK, target=chairman)
    resp = admin_for(cfo).post("/admin/access/chairmanchange/",
                               {"action": "approve_selected", "_selected_action": [change.pk]})
    assert "messages" not in resp.cookies


def test_b_chairman_enrols_himself_after_reset_and_can_sign_in(alm, mp, cfo, chairman):
    dual(mp, cfo, A.TOTP_RESET, chairman)
    c = Client()
    page = c.get(ENROL)
    assert page.status_code == 200 and "no-store" in page["Cache-Control"]
    resp = c.post(ENROL, {"username": "chairman", "password": PW})
    assert resp.status_code == 200 and "no-store" in resp["Cache-Control"]
    assert "otpauth://" in resp.content.decode()
    device = TOTPDevice.objects.get(user=chairman, confirmed=False)
    assert c.post(ENROL, {"token": "000000" if code(device) != "000000" else "111111"}).status_code == 200
    assert not TOTPDevice.objects.get(pk=device.pk).confirmed
    TOTPDevice.objects.get(pk=device.pk).throttle_reset()  # django-otp throttles after a wrong code
    done = c.post(ENROL, {"token": code(device)})
    assert "enrolled" in done.content.decode()
    chairman.refresh_from_db()
    assert not chairman.totp_enrolment_required and TOTPDevice.objects.get(pk=device.pk).confirmed
    assert AuditLog.objects.filter(action="access.totp.enroll", actor=chairman, after__self_service=True).exists()
    # Normal sign-in with password + new code now works (next time step: a used code is never accepted twice).
    login = Client().post("/admin/login/", {"username": "chairman", "password": PW, "otp_device": device.persistent_id,
                                            "otp_token": next_code(device), "next": "/admin/"})
    assert login.status_code == 302 and login["Location"] == "/admin/"
    # The page cannot be used again.
    assert "otpauth://" not in Client().post(ENROL, {"username": "chairman", "password": PW}).content.decode()


def test_b_enrolment_page_refuses_users_without_a_reset_and_wrong_passwords(alm, mp, cfo, chairman):
    mp.set_password("Mp-own-passphrase-9")
    mp.save()
    for creds in ({"username": "mp", "password": "Mp-own-passphrase-9"},       # approver: no reset pending
                  {"username": "chairman", "password": PW}):                     # no reset happened
        assert "otpauth://" not in Client().post(ENROL, creds).content.decode()
    dual(mp, cfo, A.TOTP_RESET, chairman)
    assert "otpauth://" not in Client().post(ENROL, {"username": "chairman", "password": "wrong"}).content.decode()
    assert not TOTPDevice.objects.filter(user=chairman).exists()


def test_b_password_setters_can_never_also_obtain_2fa(alm, mp, cfo, chairman):
    # Password set by the approvers: no 2FA reset until the Chairman changes it himself.
    dual(mp, cfo, A.PASSWORD, chairman, {"password": SET_PW})
    chairman.refresh_from_db()
    assert chairman.password_set_by_others and chairman.check_password(SET_PW)
    with pytest.raises(RuleError) as e:
        access.request_chairman_change(mp, A.TOTP_RESET, target=chairman)
    assert e.value.code == "factors_apart"
    # Even if the flags were both set somehow, the known password does not open the enrolment page.
    User.objects.filter(pk=chairman.pk).update(totp_enrolment_required=True)
    assert "otpauth://" not in Client().post(ENROL, {"username": "chairman", "password": SET_PW}).content.decode()
    User.objects.filter(pk=chairman.pk).update(totp_enrolment_required=False)
    # The Chairman changes the password himself; then a reset is possible again.
    access.password_changed_by_self(User.objects.get(pk=chairman.pk))
    assert not User.objects.get(pk=chairman.pk).password_set_by_others
    access.request_chairman_change(mp, A.TOTP_RESET, target=User.objects.get(pk=chairman.pk))


def test_b_no_password_set_while_reenrolment_pending_also_at_apply_time(alm, mp, cfo, chairman):
    pending_pw = access.request_chairman_change(mp, A.PASSWORD, target=chairman, payload={"password": SET_PW})
    dual(mp, cfo, A.TOTP_RESET, chairman)
    with pytest.raises(RuleError) as e:
        access.request_chairman_change(cfo, A.PASSWORD, target=User.objects.get(pk=chairman.pk),
                                       payload={"password": SET_PW})
    assert e.value.code == "factors_apart"
    with pytest.raises(RuleError):
        access.approve_chairman_change(cfo, pending_pw)  # checked again when it would apply
    assert ChairmanChange.objects.get(pk=pending_pw.pk).status == "pending"
    assert User.objects.get(pk=chairman.pk).check_password(PW)


# ---------------------------------------------------------------- 4. forged apply path


def test_forged_or_reused_change_objects_are_refused(alm, mp, cfo, chairman):
    pending = access.request_chairman_change(mp, A.DEACTIVATE, target=chairman)
    forged = ChairmanChange(pk=pending.pk, entity=alm, target=chairman, action=A.DEACTIVATE,
                            status=ChairmanChange.Status.APPLIED, requested_by=mp, expires_at=pending.expires_at)
    forged._applying = True
    access._applying_ids().add(pending.pk)  # even with the in-progress marker, the DB row is still pending
    try:
        with pytest.raises(AccessDenied):
            access.deactivate_user(mp, chairman, _approved_change=forged)
    finally:
        access._applying_ids().discard(pending.pk)
    applied = dual(mp, cfo, A.UNLOCK, chairman)  # a finished change cannot be reused for another action
    for change in (applied, ChairmanChange.objects.get(pk=applied.pk)):
        with pytest.raises(AccessDenied):
            access.deactivate_user(mp, chairman, _approved_change=change)
        with pytest.raises(AccessDenied):
            access.grant_access(mp, chairman, alm, "managing_partner", _approved_change=change)
    assert User.objects.get(pk=chairman.pk).is_active
    assert not access.has_role(chairman, alm, "managing_partner")


# ---------------------------------------------------------------- 5. revoke choices


def test_revoke_choices_limited_to_the_selected_chairman(alm, mp, chairman, make_user, accountant):
    other_chair = make_user("chair-b", (alm, "chairman"))
    form = ChairmanChangeForm()
    choices = set(form.fields["revoke_grant"].queryset.values_list("user__username", flat=True))
    assert choices == {"chairman", "chair-b"}  # no grants of non-Chairmen
    wrong = EntityAccess.objects.get(user=other_chair)
    form = ChairmanChangeForm(data={"action": "revoke", "target": chairman.pk, "revoke_grant": wrong.pk})
    assert not form.is_valid()
    right = EntityAccess.objects.get(user=chairman)
    form = ChairmanChangeForm(data={"action": "revoke", "target": chairman.pk, "revoke_grant": right.pk})
    assert form.is_valid(), form.errors
    with pytest.raises(Exception):  # the service also refuses another user's grant
        access.request_chairman_change(mp, A.REVOKE, target=chairman, payload={"grant": wrong.pk})


# ---------------------------------------------------------------- 6. role lost before the second approval


@pytest.mark.parametrize("loser", ["requester", "approver_cfo_role"])
def test_role_lost_between_request_and_second_approval_does_not_apply(alm, mp, cfo, chairman, loser):
    change = access.request_chairman_change(mp, A.DEACTIVATE, target=chairman)
    if loser == "requester":
        EntityAccess.objects.filter(user=mp, role__code="managing_partner").update(revoked_at="2026-10-17T10:00:00Z")
        access.approve_chairman_change(cfo, change)
    else:
        EntityAccess.objects.filter(user=cfo, role__code="cfo").update(revoked_at="2026-10-17T10:00:00Z")
        with pytest.raises(AccessDenied):
            access.approve_chairman_change(cfo, change)
    assert ChairmanChange.objects.get(pk=change.pk).status == "pending"
    assert User.objects.get(pk=chairman.pk).is_active
