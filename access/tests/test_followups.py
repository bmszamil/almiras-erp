"""Follow-ups (17 Oct 2026): D19 (only the Chairman manages a Managing Partner), 2a (system user
unmanageable), 2b (editor records and request editors append-only)."""
import datetime
from datetime import date

import pytest
from django.contrib.admin.sites import site
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, override_settings
from django.utils import timezone

from access import services as access
from access.errors import AccessDenied, BilingualError, RuleError
from access.models import AuditLog, EntityAccess, Permission, Role, User

pytestmark = pytest.mark.django_db
LINES = [{"account": "5200", "debit": 100}, {"account": "2300", "credit": 100}]


def req(user):
    r = RequestFactory().get("/admin/")
    r.user = user
    r.META["REMOTE_ADDR"] = "127.0.0.1"
    return r


def user_admin():
    return site._registry[User]


@pytest.fixture
def mp2(make_user, alm):
    return make_user("mp2", (alm, "managing_partner"))


@pytest.fixture
def user_manager(make_user, alm):
    role = Role.objects.create(code="user_admin_test", name_en="User admin", name_ar="مدير مستخدمين")
    role.permissions.add(*Permission.objects.filter(code__in=["access.user.manage", "access.entity_access.manage",
                                                               "core.entity.view"]))
    return make_user("ua", (alm, "user_admin_test"))


# ---------------------------------------------------------------- D19: services


@pytest.mark.parametrize("who", ["mp2", "user_manager"])
def test_d19_other_mp_or_user_manager_cannot_manage_mp(request, alm, mp, who):
    actor = request.getfixturevalue(who)
    assert access.can_manage_user(actor, mp) is False
    with pytest.raises(AccessDenied) as e:
        access.deactivate_user(actor, mp)
    assert e.value.code == "mp_protected" and e.value.message_ar
    for call in (lambda: access.unlock_user(actor, mp),
                 lambda: access.enroll_totp(actor, mp),
                 lambda: access.grant_access(actor, mp, alm, "viewer"),
                 lambda: access.revoke_access(actor, EntityAccess.objects.get(user=mp, role__code="managing_partner"))):
        with pytest.raises(AccessDenied):
            call()
    mp.refresh_from_db()
    assert mp.is_active and not EntityAccess.objects.filter(user=mp, revoked_at__isnull=False).exists()
    assert not AuditLog.objects.filter(action__in=["access.user.deactivate", "access.unlock", "access.revoke",
                                                   "access.totp.enroll"], object_id=str(mp.pk)).exists()


def test_d19_chairman_can_manage_mp_and_it_is_audited(alm, mp, chairman):
    assert access.can_manage_user(chairman, mp) is True
    access.unlock_user(chairman, mp)
    access.enroll_totp(chairman, mp)
    g = access.grant_access(chairman, mp, alm, "viewer")
    access.revoke_access(chairman, g)
    access.deactivate_user(chairman, mp)
    actions = set(AuditLog.objects.filter(actor=chairman).values_list("action", flat=True))
    assert {"access.unlock", "access.totp.enroll", "access.grant", "access.revoke", "access.user.deactivate"} <= actions
    assert User.objects.get(pk=mp.pk).is_active is False


def test_d19_chairman_needed_in_every_entity_where_target_is_mp(alm, sub, make_user):
    mp_both = make_user("mpboth", (alm, "managing_partner"), (sub, "managing_partner"))
    chair_alm = make_user("chairalm", (alm, "chairman"))
    assert access.can_manage_user(chair_alm, mp_both) is False
    access.grant_access(None, chair_alm, sub, "chairman")
    assert access.can_manage_user(chair_alm, mp_both) is True


def test_d19_mp_cannot_manage_self(mp, alm):
    with pytest.raises(AccessDenied):
        access.deactivate_user(mp, mp)
    with pytest.raises(AccessDenied):
        access.grant_access(mp, mp, alm, "viewer")


def test_d19_setup_mode_is_excepted(alm, mp, mp2):
    access.unlock_user(mp2, mp, setup=True)
    assert AuditLog.objects.filter(action="access.unlock", actor=mp2, object_id=str(mp.pk)).exists()


def test_d19_non_mp_users_still_follow_review_b(alm, user_manager, make_user):
    acc = make_user("acc", (alm, "accountant"))
    access.deactivate_user(user_manager, acc)
    assert User.objects.get(pk=acc.pk).is_active is False


# ---------------------------------------------------------------- D19: admin


def test_d19_admin_user_changes_to_mp_only_by_chairman(alm, mp, mp2, chairman, user_manager):
    ma = user_admin()
    for actor in (mp2, user_manager):
        assert ma.has_change_permission(req(actor), mp) is False
        assert {"is_active", "is_staff"} <= set(ma.get_readonly_fields(req(actor), mp))
        mp.is_active = False
        with pytest.raises(PermissionDenied):
            ma.save_model(req(actor), mp, form=None, change=True)
        with pytest.raises(PermissionDenied):
            ma.user_change_password(req(actor), str(mp.pk))
    assert User.objects.get(pk=mp.pk).is_active is True
    assert ma.has_change_permission(req(chairman), mp) is True
    mp.is_active = False
    request = req(chairman)
    ma.save_model(request, mp, form=None, change=True)
    ma.save_related(request, type("F", (), {"instance": mp, "save_m2m": lambda self: None})(), [], True)
    assert AuditLog.objects.filter(action="access.user.privilege_change", actor=chairman, object_id=str(mp.pk)).exists()


def test_d19_admin_unlock_action_refused_for_other_mp(alm, mp, mp2):
    ma = user_admin()
    ma.message_user = lambda *a, **k: None
    ma.unlock_selected(req(mp2), User.objects.filter(pk=mp.pk))
    assert not AuditLog.objects.filter(action="access.unlock").exists()


def test_d19_roles_held_by_mp_edited_only_with_chairman(alm, mp, chairman):
    role_admin = site._registry[Role]
    mp_role = Role.objects.get(code="managing_partner")
    viewer = Role.objects.get(code="viewer")
    assert role_admin.has_change_permission(req(mp), mp_role) is False  # nobody but a Chairman
    assert role_admin.has_change_permission(req(mp), viewer) is True     # no MP holds viewer
    with pytest.raises(PermissionDenied):
        role_admin.save_model(req(mp), mp_role, form=None, change=True)
    with override_settings(ERP_ADMIN_SETUP_MODE=True):
        mp.is_superuser = True
        mp.save()
        assert role_admin.has_change_permission(req(mp), mp_role) is True


def test_d19_api_has_no_way_to_manage_another_user(mp, mp2, api):
    """The API exposes only /me for user data; it cannot change other users or privileges."""
    c = api(mp2)
    resp = c.patch("/api/v1/me", {"is_active": False, "user_type": "service_agent", "is_staff": True}, format="json")
    assert resp.status_code == 200
    mp2.refresh_from_db()
    mp.refresh_from_db()
    assert mp2.is_active and mp2.user_type == "human" and mp.is_active


# ---------------------------------------------------------------- 2a: system user


def test_2a_system_user_is_unmanageable(alm, mp, chairman):
    system = access.system_user()
    assert access.can_manage_user(mp, system) is False and access.can_manage_user(mp, system, setup=True) is False
    for call in (lambda: access.issue_service_token(None, system, expires_at=timezone.now() + datetime.timedelta(days=1)),
                 lambda: access.grant_access(None, system, alm, "viewer"),
                 lambda: access.enroll_totp(None, system),
                 lambda: access.unlock_user(None, system)):
        with pytest.raises(RuleError):
            call()
    with pytest.raises(AccessDenied):
        access.deactivate_user(chairman, system)
    system.username = "renamed"
    with pytest.raises(BilingualError):
        system.save()
    system.refresh_from_db()
    system.set_password("x" * 20)
    with pytest.raises(BilingualError):
        system.save()
    assert not User.objects.get(username="system").has_usable_password()


def test_2a_no_user_can_take_the_system_name(make_user):
    u = make_user("someone")
    u.username = "system"
    with pytest.raises(BilingualError):
        u.save()


def test_2a_admin_cannot_change_system_user_even_in_setup_mode(mp):
    system = access.system_user()
    with override_settings(ERP_ADMIN_SETUP_MODE=True):
        mp.is_superuser = True
        mp.save()
        assert user_admin().has_change_permission(req(mp), system) is False


# ---------------------------------------------------------------- 2b: append-only editor records


def test_2b_editor_records_refuse_bulk_update_and_delete(alm, accountant):
    from ledger import services
    from ledger.models import JournalEntryEditor, LedgerError

    e = services.create_entry(alm, date(2027, 1, 10), "Draft", LINES, user=accountant)
    for call in (lambda: JournalEntryEditor.objects.filter(entry=e).update(action="edit"),
                 lambda: JournalEntryEditor.objects.all().delete(),
                 lambda: e.editors.get().delete()):
        with pytest.raises(LedgerError):
            call()
    e.delete()  # deleting the draft still removes its editor rows
    assert not JournalEntryEditor.objects.exists()


def test_2b_request_editors_refuse_bulk_update_and_delete(alm, accountant, make_payment):
    from approvals import services as approvals
    from approvals.models import RequestEditor

    r = approvals.submit(make_payment(accountant, alm), accountant)
    assert RequestEditor.objects.filter(approvalrequest=r).count() == 1
    for call in (lambda: RequestEditor.objects.filter(approvalrequest=r).update(user=accountant),
                 lambda: RequestEditor.objects.all().delete(),
                 lambda: r.document_editors.clear(),
                 lambda: r.document_editors.remove(accountant)):
        from django.db import transaction

        with pytest.raises(BilingualError), transaction.atomic():  # M2M helpers use atomic(savepoint=False)
            call()
    assert list(r.document_editors.all()) == [accountant]
