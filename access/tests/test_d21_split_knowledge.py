"""D21 re-review: split knowledge for a dual-controlled initial password. The requester knows the password,
only the applying approver gets a one-time code; enrolment needs both. Single use, expiry, lockout, no leaks."""
import datetime

import pytest
from django.test import Client, override_settings
from django.utils import timezone
from django_otp.oath import TOTP
from django_otp.plugins.otp_totp.models import TOTPDevice

from access import services as access
from access.models import AuditLog, ChairmanChange, User

pytestmark = pytest.mark.django_db
A = ChairmanChange.Action
ENROL = "/access/enrol-2fa/"
INIT_PW = "Initial-from-approvers-3"
OWN_PW = "Chosen-by-myself-2"


@pytest.fixture
def cfo(make_user, alm):
    return make_user("cfo", (alm, "cfo"))


@pytest.fixture
def new_chairman(alm, mp, cfo, make_user):
    """X just became Chairman by dual control: no password, no device, enrolment required."""
    x = make_user("x", (alm, "viewer"))
    change = access.request_chairman_change(mp, A.GRANT, target=x, payload={"role": "chairman", "entity": "ALM"})
    access.approve_chairman_change(cfo, change)
    x.refresh_from_db()
    assert x.totp_enrolment_required and not x.has_usable_password()
    return x


def initial_password(mp, cfo, x, requester="mp"):
    first, second = (mp, cfo) if requester == "mp" else (cfo, mp)
    change = access.request_chairman_change(first, A.PASSWORD, target=x, payload={"password": INIT_PW})
    _, code = access.approve_chairman_change(second, change)
    return code


def step0(client, password, code=""):
    return client.post(ENROL, {"username": "x", "password": password, "code": code}).content.decode()


def totp_code(device):
    device.refresh_from_db()
    t = TOTP(device.bin_key, step=device.step, t0=device.t0, digits=device.digits)
    t.time = device.t0 + max(device.last_t + 1, int(t.t())) * device.step
    return f"{t.token():0{device.digits}d}"


def complete(client, page):
    assert "new_password1" in page
    page = client.post(ENROL, {"new_password1": OWN_PW, "new_password2": OWN_PW}).content.decode()
    assert "otpauth://" in page
    device = TOTPDevice.objects.get(user__username="x", confirmed=False)
    assert "enrolled" in client.post(ENROL, {"token": totp_code(device)}).content.decode()


@pytest.mark.parametrize("requester", ["mp", "cfo"])
def test_code_goes_only_to_the_applying_approver(new_chairman, mp, cfo, requester):
    code = initial_password(mp, cfo, new_chairman, requester)
    assert isinstance(code, access.EnrolmentCode) and len(code.replace("-", "")) >= 10
    # The requester got nothing back from his request call; the code came from the second approval only.
    x = User.objects.get(pk=new_chairman.pk)
    assert x.enrolment_code_required and x.enrolment_code_hash and code not in x.enrolment_code_hash


def test_requester_with_only_the_password_cannot_enrol(new_chairman, mp, cfo):
    initial_password(mp, cfo, new_chairman)
    for code in ("", "WRONG-CODE-123"):
        page = step0(Client(), INIT_PW, code)
        assert "new_password1" not in page and "otpauth://" not in page
    assert AuditLog.objects.filter(action="access.sign_in_failed", after__reason="enrolment_code").count() == 2
    assert User.objects.get(pk=new_chairman.pk).password_set_by_others


def test_approver_with_only_the_code_cannot_enrol(new_chairman, mp, cfo):
    code = initial_password(mp, cfo, new_chairman)
    for password in ("", "guess-the-password-1"):
        page = step0(Client(), password, code)
        assert "new_password1" not in page and "otpauth://" not in page
    # The code was not consumed by failed password attempts: the Chairman can still use it.
    complete(c := Client(), step0(c, INIT_PW, code))


def test_chairman_with_both_enrols_and_code_is_single_use(new_chairman, mp, cfo):
    code = initial_password(mp, cfo, new_chairman)
    c = Client()
    complete(c, step0(c, INIT_PW, code))
    x = User.objects.get(pk=new_chairman.pk)
    assert not x.totp_enrolment_required and not x.enrolment_code_required and not x.password_set_by_others
    assert x.check_password(OWN_PW)
    assert "otpauth://" not in step0(Client(), INIT_PW, code)  # nothing pending, code gone


def test_abandoned_after_code_needs_a_new_dual_control_password(new_chairman, mp, cfo):
    code = initial_password(mp, cfo, new_chairman)
    assert "new_password1" in step0(Client(), INIT_PW, code)  # code redeemed, then the session is abandoned
    for c in ("", code):
        assert "new_password1" not in step0(Client(), INIT_PW, c)  # single use: requester cannot continue
    code2 = initial_password(mp, cfo, User.objects.get(pk=new_chairman.pk))
    complete(c := Client(), step0(c, INIT_PW, code2))


def test_expired_code_refused_and_new_reset_needed(new_chairman, mp, cfo):
    with override_settings(ERP_ENROLMENT_CODE_HOURS=0):
        code = initial_password(mp, cfo, new_chairman)
    assert "new_password1" not in step0(Client(), INIT_PW, code)
    assert AuditLog.objects.filter(action="access.enrolment_code.expired", object_id=str(new_chairman.pk)).exists()
    code2 = initial_password(mp, cfo, User.objects.get(pk=new_chairman.pk))
    x = User.objects.get(pk=new_chairman.pk)
    assert x.enrolment_code_expires_at - timezone.now() > datetime.timedelta(hours=71)  # default 72 h
    complete(c := Client(), step0(c, INIT_PW, code2))


def test_wrong_codes_count_towards_lockout(new_chairman, mp, cfo):
    code = initial_password(mp, cfo, new_chairman)
    for _ in range(5):
        step0(Client(), INIT_PW, "WRONG-CODE-123")
    assert "new_password1" not in step0(Client(), INIT_PW, code)  # locked: even the right code waits
    assert AuditLog.objects.filter(action="access.sign_in_locked").exists()


def test_code_never_in_audit_log_or_messages(new_chairman, mp, cfo, api):
    cfo.is_staff = True
    cfo.save()
    change = access.request_chairman_change(mp, A.PASSWORD, target=new_chairman, payload={"password": INIT_PW})
    c = api(cfo)
    resp = c.post("/admin/access/chairmanchange/", {"action": "approve_selected", "_selected_action": [change.pk]})
    assert resp.status_code == 200 and "no-store" in resp["Cache-Control"]
    import re

    shown = re.search(r"<code dir=\"ltr\">([0-9A-F-]+)</code>", resp.content.decode()).group(1)
    assert ChairmanChange.objects.get(pk=change.pk).status == "applied"
    later = c.get("/admin/access/chairmanchange/").content.decode()  # session messages rendered here
    assert shown not in later and "messages" not in resp.cookies
    for row in AuditLog.objects.all():
        assert shown not in str(row.after) + str(row.before)
    assert shown not in str(User.objects.filter(pk=new_chairman.pk).values().get())
    # The requester never sees it.
    mp.is_staff = True
    mp.save()
    assert shown not in api(mp).get(f"/admin/access/chairmanchange/{change.pk}/change/").content.decode()
    complete(cl := Client(), step0(cl, INIT_PW, shown))


def test_mp_initial_password_from_chairman_needs_no_code(alm, chairman, make_user, api):
    """D19: a Chairman alone may manage an MP; no split knowledge there."""
    x = make_user("x", (alm, "viewer"))
    access.grant_access(None, x, alm, "managing_partner")
    chairman.is_staff = True
    chairman.save()
    api(chairman).post(f"/admin/access/user/{x.pk}/password/",
                       {"password1": INIT_PW, "password2": INIT_PW, "usable_password": "true"})
    assert not User.objects.get(pk=x.pk).enrolment_code_required
    complete(c := Client(), step0(c, INIT_PW))
