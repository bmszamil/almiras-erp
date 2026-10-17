"""D21 (MP decision, 17 Oct 2026): any change to a Chairman's account needs two approvals together, one
from a CFO and one from a Managing Partner, both in the group root entity. Dual-control pending change;
two distinct people; never the Chairman himself; expiry; full audit. Setup mode / operator commands bypass."""
import datetime

import pytest
from django.contrib.admin.sites import site
from django.test import RequestFactory, override_settings
from django_otp.plugins.otp_totp.models import TOTPDevice

from access import services as access
from access.errors import AccessDenied, BilingualError, Conflict, RuleError
from access.models import AuditLog, ChairmanChange, ChairmanChangeApproval, EntityAccess, Permission, Role, User

pytestmark = pytest.mark.django_db
PW = "N3w-long-passphrase!"
A = ChairmanChange.Action


def req(user):
    r = RequestFactory().get("/admin/")
    r.user = user
    r.META["REMOTE_ADDR"] = "127.0.0.1"
    return r


@pytest.fixture
def cfo(make_user, alm):
    return make_user("cfo", (alm, "cfo"))


@pytest.fixture
def cfo_sub(make_user, sub):
    return make_user("cfo-sub", (sub, "cfo"))


@pytest.fixture
def chairman2(make_user, alm):
    return make_user("chairman2", (alm, "chairman"))


@pytest.fixture
def user_manager(make_user, alm):
    role = Role.objects.create(code="user_admin_test", name_en="User admin", name_ar="مدير مستخدمين")
    role.permissions.add(*Permission.objects.filter(code__in=["access.user.manage", "access.entity_access.manage",
                                                               "core.entity.view"]))
    return make_user("ua", (alm, "user_admin_test"))


@pytest.fixture
def admin_for(api):
    def make(user):
        user.is_staff = True
        user.save()
        return api(user)

    return make


def password_change(actor, chairman):
    return access.request_chairman_change(actor, A.PASSWORD, target=chairman, payload={"password": PW})


# ---------------------------------------------------------------- no single person


@pytest.mark.parametrize("who", ["mp", "cfo", "chairman2", "user_manager"])
def test_d21_no_single_person_changes_a_chairman(request, alm, chairman, admin_for, who):
    actor = request.getfixturevalue(who)
    assert access.can_manage_user(actor, chairman) is False
    old_hash = chairman.password
    for call in (lambda: access.enroll_totp(actor, chairman),
                 lambda: access.deactivate_user(actor, chairman),
                 lambda: access.unlock_user(actor, chairman),
                 lambda: access.grant_access(actor, chairman, alm, "viewer"),
                 lambda: access.revoke_access(actor, EntityAccess.objects.get(user=chairman, role__code="chairman"))):
        with pytest.raises(AccessDenied):
            call()
    c = admin_for(actor)
    assert c.post(f"/admin/access/user/{chairman.pk}/password/",
                  {"password1": PW, "password2": PW, "usable_password": "true"}).status_code in (403, 404)
    chairman.refresh_from_db()
    assert chairman.is_active and chairman.password == old_hash
    assert not TOTPDevice.objects.filter(user=chairman).exists()
    assert not AuditLog.objects.filter(object_id=str(chairman.pk), actor=actor).exclude(
        action__startswith="access.sign_in").exists()


def test_d21_refusal_code_points_to_dual_control(alm, mp, chairman):
    with pytest.raises(AccessDenied) as e:
        access.deactivate_user(mp, chairman)
    assert e.value.code == "chairman_dual_control" and "D21" in e.value.message_en and e.value.message_ar


@pytest.mark.parametrize("who", ["mp", "cfo"])
def test_d21_one_approver_alone_leaves_change_pending(request, alm, chairman, who):
    actor = request.getfixturevalue(who)
    change = password_change(actor, chairman)
    assert change.status == ChairmanChange.Status.PENDING
    with pytest.raises(Conflict) as e:
        access.approve_chairman_change(actor, change)  # nobody approves their own request twice
    assert e.value.code == "dual_control_same_person"
    change.refresh_from_db()
    assert change.status == "pending" and change.approvals.count() == 1
    assert not User.objects.get(pk=chairman.pk).check_password(PW)


def test_d21_two_mps_or_two_cfos_are_not_enough(alm, mp, cfo, chairman, make_user):
    mp2 = make_user("mp2", (alm, "managing_partner"))
    cfo2 = make_user("cfo2", (alm, "cfo"))
    for first, second in ((mp, mp2), (cfo, cfo2)):
        change = password_change(first, chairman)
        change, result = access.approve_chairman_change(second, change)
        change.refresh_from_db()
        assert change.status == "pending" and result is None
    assert not User.objects.get(pk=chairman.pk).check_password(PW)


# ---------------------------------------------------------------- CFO + MP applies, audited


@pytest.mark.parametrize("order", ["mp_first", "cfo_first"])
def test_d21_cfo_and_mp_together_apply_password_change(alm, mp, cfo, chairman, order):
    first, second = (mp, cfo) if order == "mp_first" else (cfo, mp)
    change = password_change(first, chairman)
    assert "password" not in change.payload and "password_hash" in change.payload  # never plain text
    change, _ = access.approve_chairman_change(second, change)
    change.refresh_from_db()
    assert change.status == "applied" and change.decided_by == second
    assert User.objects.get(pk=chairman.pk).check_password(PW)
    actions = list(AuditLog.objects.filter(object_type="access.chairmanchange", object_id=str(change.pk))
                   .order_by("id").values_list("action", "actor__username"))
    assert actions == [("access.chairman_change.request", first.username),
                       ("access.chairman_change.approve", second.username),
                       ("access.chairman_change.apply", second.username)]
    pw = AuditLog.objects.get(action="access.user.password_change", object_id=str(chairman.pk))
    assert pw.after["chairman_change"] == change.pk
    for row in AuditLog.objects.all():
        assert change.payload["password_hash"] not in str(row.after) + str(row.before)


def test_d21_each_kind_of_change_applies_after_both_approvals(alm, sub, mp, cfo, chairman):
    def both(action, **kw):
        change = access.request_chairman_change(mp, action, **kw)
        change, result = access.approve_chairman_change(cfo, change)
        assert ChairmanChange.objects.get(pk=change.pk).status == "applied"
        return result

    TOTPDevice.objects.create(user=chairman, name="old", confirmed=True)
    assert both(A.TOTP_RESET, target=chairman) is None  # never returns a device or secret
    assert not TOTPDevice.objects.filter(user=chairman).exists()
    assert User.objects.get(pk=chairman.pk).totp_enrolment_required
    both(A.UNLOCK, target=chairman)
    grant = both(A.GRANT, target=chairman, payload={"role": "viewer", "entity": "LZM"})
    assert access.has_role(chairman, sub, "viewer") and grant.granted_by == cfo
    both(A.REVOKE, target=chairman, payload={"grant": grant.pk})
    assert not access.has_role(chairman, sub, "viewer")
    both(A.UPDATE, target=chairman, payload={"fields": {"email": "chair@example.test", "is_staff": True}})
    chairman.refresh_from_db()
    assert chairman.email == "chair@example.test" and chairman.is_staff
    both(A.DEACTIVATE, target=chairman)
    assert User.objects.get(pk=chairman.pk).is_active is False
    for action in ("access.totp.reset", "access.unlock", "access.grant", "access.revoke",
                   "access.user.privilege_change", "access.user.deactivate"):
        row = AuditLog.objects.filter(action=action, actor=cfo).latest("id")
        assert row.after.get("chairman_change"), action


def test_d21_role_edits_affecting_a_chairman_need_dual_control(alm, mp, cfo, chairman, user_manager):
    role_admin = site._registry[Role]
    viewer = Role.objects.get(code="viewer")
    access.grant_access(None, chairman, alm, "viewer")  # a Chairman holds viewer now
    for role in (viewer, Role.objects.get(code="chairman")):
        assert access.can_edit_role(user_manager, role) is False
        assert role_admin.has_change_permission(req(user_manager), role) is False
        assert role_admin.has_change_permission(req(mp), role) is False
    new = sorted(set(viewer.permissions.values_list("code", flat=True)) | {"reports.report.view"})
    change = access.request_chairman_change(cfo, A.ROLE_PERMISSIONS, role=viewer, payload={"permissions": new})
    assert "reports.report.view" not in viewer.permissions.values_list("code", flat=True)
    access.approve_chairman_change(mp, change)
    assert sorted(viewer.permissions.values_list("code", flat=True)) == new
    assert AuditLog.objects.filter(action="access.role.change", after__chairman_change=change.pk).exists()
    with pytest.raises(RuleError):  # roles no Chairman holds use the normal role edit
        access.request_chairman_change(cfo, A.ROLE_PERMISSIONS, role=Role.objects.get(code="auditor"),
                                       payload={"permissions": []})


# ---------------------------------------------------------------- who may approve


def test_d21_chairman_can_never_approve_his_own_change(alm, mp, cfo, chairman):
    access.grant_access(None, chairman, alm, "cfo")  # even holding the CFO role
    with pytest.raises(AccessDenied) as e:
        password_change(chairman, chairman)
    assert e.value.code == "dual_control_own_account"
    change = password_change(mp, chairman)
    with pytest.raises(AccessDenied):
        access.approve_chairman_change(chairman, change)
    with pytest.raises(AccessDenied):
        access.reject_chairman_change(chairman, change)
    assert ChairmanChange.objects.get(pk=change.pk).status == "pending"


def test_d21_another_chairman_is_not_an_approver(alm, mp, chairman, chairman2):
    with pytest.raises(AccessDenied) as e:
        password_change(chairman2, chairman)
    assert e.value.code == "dual_control_approver"
    change = password_change(mp, chairman)
    with pytest.raises(AccessDenied):
        access.approve_chairman_change(chairman2, change)


def test_d21_cfo_of_a_subsidiary_is_refused(alm, sub, mp, cfo_sub, chairman, make_user):
    with pytest.raises(AccessDenied):
        password_change(cfo_sub, chairman)
    change = password_change(mp, chairman)
    with pytest.raises(AccessDenied):
        access.approve_chairman_change(cfo_sub, change)
    mp_sub = make_user("mp-sub", (sub, "managing_partner"))
    with pytest.raises(AccessDenied):
        access.approve_chairman_change(mp_sub, change)
    with pytest.raises(RuleError):
        access.request_chairman_change(mp, A.UNLOCK, target=chairman, entity=sub)  # must be the root entity
    assert ChairmanChange.objects.get(pk=change.pk).status == "pending"


def test_d21_only_for_chairmen(alm, mp, cfo, accountant):
    with pytest.raises(RuleError) as e:
        access.request_chairman_change(mp, A.UNLOCK, target=accountant)
    assert e.value.code == "not_chairman_change"


# ---------------------------------------------------------------- expiry and rejection


def test_d21_default_expiry_is_seven_days(alm, mp, chairman):
    change = password_change(mp, chairman)
    assert change.expires_at - change.requested_at == datetime.timedelta(days=7)


def test_d21_expired_request_cannot_be_approved_and_expiry_is_audited(alm, mp, cfo, chairman):
    with override_settings(ERP_CHAIRMAN_CHANGE_EXPIRY_DAYS=0):
        change = password_change(mp, chairman)
    with pytest.raises(Conflict) as e:
        access.approve_chairman_change(cfo, change)
    assert e.value.code == "dual_control_expired"
    assert ChairmanChange.objects.get(pk=change.pk).status == "expired"
    assert AuditLog.objects.filter(action="access.chairman_change.expire", object_id=str(change.pk)).exists()
    assert not User.objects.get(pk=chairman.pk).check_password(PW)
    assert not ChairmanChangeApproval.objects.filter(change=change, approver=cfo).exists()


def test_d21_cron_expires_due_requests(alm, mp, chairman):
    with override_settings(ERP_CHAIRMAN_CHANGE_EXPIRY_DAYS=0):
        due = password_change(mp, chairman)
    fresh = password_change(mp, chairman)
    assert access.expire_chairman_changes() == 1
    assert ChairmanChange.objects.get(pk=due.pk).status == "expired"
    assert ChairmanChange.objects.get(pk=fresh.pk).status == "pending"


def test_d21_reject_is_final_and_audited(alm, mp, cfo, chairman):
    change = password_change(mp, chairman)
    access.reject_chairman_change(cfo, change, reason="Not requested by the Chairman")
    assert ChairmanChange.objects.get(pk=change.pk).status == "rejected"
    with pytest.raises(Conflict):
        access.approve_chairman_change(cfo, change)
    row = AuditLog.objects.get(action="access.chairman_change.reject", object_id=str(change.pk))
    assert row.actor == cfo and row.after["reason"] == "Not requested by the Chairman"


def test_d21_requests_and_approvals_cannot_be_edited_or_deleted(alm, mp, chairman):
    change = password_change(mp, chairman)
    for call in (lambda: ChairmanChange.objects.filter(pk=change.pk).update(status="applied"),
                 lambda: ChairmanChange.objects.all().delete(),
                 lambda: change.save(),
                 lambda: change.delete(),
                 lambda: ChairmanChangeApproval.objects.all().update(roles=[]),
                 lambda: change.approvals.get().delete()):
        with pytest.raises(BilingualError):
            call()
    assert ChairmanChange.objects.get(pk=change.pk).status == "pending"


# ---------------------------------------------------------------- bypasses that stay


def test_d21_setup_mode_and_operator_commands_still_bypass(alm, mp, chairman, admin_for):
    access.enroll_totp(None, chairman)  # `setup_totp`
    access.unlock_user(None, chairman)  # `unlock_user`
    access.grant_access(None, chairman, alm, "viewer")  # `grant_access`
    access.enroll_totp(mp, chairman, name="Second", setup=True)
    with override_settings(ERP_ADMIN_SETUP_MODE=True):
        mp.is_superuser = True
        mp.save()
        c = admin_for(mp)
        assert c.post(f"/admin/access/user/{chairman.pk}/password/",
                      {"password1": PW, "password2": PW, "usable_password": "true"}).status_code == 302
    assert User.objects.get(pk=chairman.pk).check_password(PW)


def test_d21_private_bypass_cannot_be_forged(alm, mp, chairman):
    change = password_change(mp, chairman)
    change._applying = True  # pending, not applied: refused
    with pytest.raises(AccessDenied):
        access.deactivate_user(mp, chairman, _approved_change=change)
    assert User.objects.get(pk=chairman.pk).is_active


def test_d19_still_chairman_manages_mp(alm, mp, chairman):
    access.deactivate_user(chairman, mp)
    assert User.objects.get(pk=mp.pk).is_active is False


# ---------------------------------------------------------------- admin (HTTP)


def test_d21_admin_request_and_approve(alm, mp, cfo, chairman, admin_for):
    c_mp = admin_for(mp)
    resp = c_mp.post("/admin/access/chairmanchange/add/", {"action": "deactivate", "target": chairman.pk})
    assert resp.status_code == 302, resp.content.decode()[:3000]
    change = ChairmanChange.objects.get()
    assert change.requested_by == mp and change.status == "pending"
    assert User.objects.get(pk=chairman.pk).is_active
    c_cfo = admin_for(cfo)
    assert c_cfo.get(f"/admin/access/chairmanchange/{change.pk}/change/").status_code == 200
    resp = c_cfo.post("/admin/access/chairmanchange/", {"action": "approve_selected", "_selected_action": [change.pk]})
    assert resp.status_code == 302
    assert ChairmanChange.objects.get(pk=change.pk).status == "applied"
    assert User.objects.get(pk=chairman.pk).is_active is False


def test_d21_admin_refuses_non_approvers(alm, mp, chairman, chairman2, admin_for):
    change = password_change(mp, chairman)
    c = admin_for(chairman2)
    assert c.get("/admin/access/chairmanchange/add/").status_code == 403
    c.post("/admin/access/chairmanchange/", {"action": "approve_selected", "_selected_action": [change.pk]})
    assert ChairmanChange.objects.get(pk=change.pk).status == "pending"
    page = admin_for(mp).get(f"/admin/access/chairmanchange/{change.pk}/change/").content.decode()
    assert change.payload["password_hash"] not in page
