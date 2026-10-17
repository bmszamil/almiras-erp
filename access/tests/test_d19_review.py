"""Review of fix/approvals-followups (17 Oct 2026): D19 bypasses closed, Chairman admin access,
system user bulk guards, editor table constraint name, identity fields, D20 role data."""
import pytest
from django.contrib.admin.sites import site
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.test import RequestFactory, override_settings

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


@pytest.fixture
def user_admin_role(db):
    role = Role.objects.create(code="user_admin_test", name_en="User admin", name_ar="مدير مستخدمين")
    role.permissions.add(*Permission.objects.filter(code__in=["access.user.manage", "access.entity_access.manage",
                                                               "core.entity.view"]))
    return role


@pytest.fixture
def user_manager(make_user, alm, user_admin_role):
    return make_user("ua", (alm, "user_admin_test"))


@pytest.fixture
def accomplice(make_user, alm):
    return make_user("accomplice", (alm, "viewer"))


@pytest.fixture
def admin_for(api):
    def make(user):
        user.is_staff = True
        user.save()
        return api(user)  # password + verified TOTP session (D14)

    return make


def change_url(user):
    return f"/admin/access/user/{user.pk}/change/"


def form_data(user, **overrides):
    data = {
        "username": user.username, "full_name_en": user.full_name_en, "full_name_ar": user.full_name_ar,
        "email": user.email, "language": user.language, "default_entity": "", "keycloak_id": "",
        "is_active": "on", "_save": "Save",
    }
    if user.is_staff:
        data["is_staff"] = "on"
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


# ---------------------------------------------------------------- 1. self-grants and the Chairman role


@pytest.mark.parametrize("who", ["mp", "chairman", "user_manager", "accountant"])
def test_nobody_grants_or_revokes_own_roles(request, alm, who):
    actor = request.getfixturevalue(who)
    for role in ("viewer", "chairman"):
        with pytest.raises(AccessDenied) as e:
            access.grant_access(actor, actor, alm, role)
        assert e.value.code == "self_grant" and e.value.message_ar
        with pytest.raises(AccessDenied):
            access.grant_access(actor, actor, alm, role, setup=True)  # not even in setup mode
    grant = EntityAccess.objects.filter(user=actor).first()
    with pytest.raises(AccessDenied) as e:
        access.revoke_access(actor, grant)
    assert e.value.code == "self_grant"
    assert EntityAccess.objects.get(pk=grant.pk).revoked_at is None
    assert access.has_role(actor, alm, "chairman") is (who == "chairman")


def test_non_mp_self_grants_chairman_then_cannot_manage_mp(alm, mp, user_manager):
    with pytest.raises(AccessDenied):
        access.grant_access(user_manager, user_manager, alm, "chairman")
    assert not access.has_role(user_manager, alm, "chairman")
    assert access.can_manage_user(user_manager, mp) is False
    with pytest.raises(AccessDenied) as e:
        access.deactivate_user(user_manager, mp)
    assert e.value.code == "mp_protected"
    assert User.objects.get(pk=mp.pk).is_active


@pytest.mark.parametrize("who", ["mp", "user_manager"])
def test_granting_chairman_to_an_accomplice_is_refused(request, alm, mp, accomplice, make_user, who):
    actor = mp if who == "mp" else request.getfixturevalue(who)
    with pytest.raises(AccessDenied) as e:
        access.grant_access(actor, accomplice, alm, "chairman")
    assert e.value.code == "chairman_role_protected" and e.value.message_ar
    assert not access.has_role(accomplice, alm, "chairman")
    with pytest.raises(AccessDenied):
        access.deactivate_user(accomplice, mp)
    assert User.objects.get(pk=mp.pk).is_active
    assert not AuditLog.objects.filter(action="access.grant", actor=actor).exists()


def test_no_single_person_grants_or_revokes_chairman(alm, sub, mp, chairman, accomplice, make_user, user_manager):
    """Superseded the 'Chairman of that entity grants Chairman' rule: D21 review puts it under CFO + MP dual control."""
    chair_sub = make_user("chairsub", (sub, "chairman"))
    for actor in (chair_sub, chairman, mp, user_manager):
        with pytest.raises(AccessDenied) as e:
            access.grant_access(actor, accomplice, alm, "chairman")
        assert e.value.code == "chairman_role_protected"
    grant = access.grant_access(None, accomplice, alm, "chairman")  # operator bootstrap still works
    for actor in (mp, user_manager, chair_sub, chairman):
        with pytest.raises(AccessDenied):
            access.revoke_access(actor, grant)
    assert access.has_role(accomplice, alm, "chairman")


def test_operator_command_and_setup_mode_may_grant_chairman(alm, mp, accomplice, make_user):
    access.grant_access(None, accomplice, alm, "chairman")  # `grant_access` command (bootstrap)
    assert access.has_role(accomplice, alm, "chairman")
    other = make_user("other", (alm, "viewer"))
    access.grant_access(mp, other, alm, "chairman", setup=True)  # setup mode (still needs entity_access.manage)
    assert access.has_role(other, alm, "chairman")


def test_other_roles_still_need_entity_access_manage(alm, accomplice, make_user, user_manager):
    acc = make_user("acc", (alm, "accountant"))
    with pytest.raises(AccessDenied):
        access.grant_access(acc, accomplice, alm, "accountant")
    access.grant_access(user_manager, accomplice, alm, "accountant")
    assert access.has_role(accomplice, alm, "accountant")


# ---------------------------------------------------------------- 2. role edits need Chairman over every MP holder


def test_role_edit_needs_chairman_in_every_mp_entity_of_holders(alm, sub, mp, make_user, user_admin_role):
    role_admin = site._registry[Role]
    viewer = Role.objects.get(code="viewer")
    mp_role = Role.objects.get(code="managing_partner")
    access.grant_access(None, mp, alm, "viewer")  # an MP holds viewer, so editing viewer changes his rights
    chair_sub = make_user("chairsub", (alm, "user_admin_test"), (sub, "chairman"))
    chair_alm = make_user("chairalm", (alm, "user_admin_test"), (alm, "chairman"))
    for role in (viewer, mp_role):
        assert access.can_edit_role(chair_sub, role) is False
        assert role_admin.has_change_permission(req(chair_sub), role) is False
        with pytest.raises(PermissionDenied):
            role_admin.save_model(req(chair_sub), role, form=None, change=True)
        assert role_admin.has_change_permission(req(chair_alm), role) is True
    # An MP in two entities: the Chairman of only one of them cannot edit the roles he holds.
    access.grant_access(None, mp, sub, "managing_partner")
    assert role_admin.has_change_permission(req(chair_alm), viewer) is False
    access.grant_access(None, chair_alm, sub, "chairman")
    assert role_admin.has_change_permission(req(chair_alm), viewer) is True
    # Roles held by no MP need only the normal role-edit right.
    assert role_admin.has_change_permission(req(chair_sub), Role.objects.get(code="auditor")) is True


# ---------------------------------------------------------------- 3. the Chairman uses the admin (HTTP)


def test_chairman_sees_and_changes_mp_in_admin(alm, mp, chairman, accountant, admin_for):
    c = admin_for(chairman)
    resp = c.get("/admin/access/user/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert f"/admin/access/user/{mp.pk}/change/" in html
    assert f"/admin/access/user/{accountant.pk}/change/" not in html  # not an MP: not the Chairman's to manage
    assert c.get(change_url(mp)).status_code == 200
    assert c.get(f"/admin/access/user/{mp.pk}/password/").status_code == 200
    resp = c.post(change_url(mp), form_data(mp, is_active=None, full_name_en="Mp Renamed"))
    assert resp.status_code == 302, resp.content.decode()[:2000]
    mp.refresh_from_db()
    assert mp.is_active is False and mp.full_name_en == "Mp Renamed"
    row = AuditLog.objects.get(action="access.user.privilege_change", actor=chairman, object_id=str(mp.pk))
    assert row.before["is_active"] is True and row.after["is_active"] is False and row.channel == "web"
    assert c.get(change_url(accountant)).status_code != 200


def test_chairman_password_change_for_mp_is_audited(alm, mp, chairman, admin_for):
    c = admin_for(chairman)
    resp = c.post(f"/admin/access/user/{mp.pk}/password/",
                  {"password1": "N3w-long-passphrase!", "password2": "N3w-long-passphrase!", "usable_password": "true"})
    assert resp.status_code == 302
    assert AuditLog.objects.filter(action="access.user.password_change", actor=chairman, object_id=str(mp.pk)).exists()


def test_chairman_of_other_entity_cannot_reach_mp_in_admin(alm, sub, mp, make_user, admin_for):
    chair_sub = make_user("chairsub", (sub, "chairman"))
    c = admin_for(chair_sub)
    assert c.get("/admin/access/user/").status_code == 200
    assert f"/admin/access/user/{mp.pk}/change/" not in c.get("/admin/access/user/").content.decode()
    assert c.get(change_url(mp)).status_code != 200
    c.post(change_url(mp), form_data(mp, is_active=None))
    assert User.objects.get(pk=mp.pk).is_active


@pytest.mark.parametrize("who", ["mp2", "user_manager"])
def test_other_mp_or_user_manager_cannot_change_mp_over_http(request, alm, mp, make_user, admin_for, who):
    actor = make_user("mp2", (alm, "managing_partner")) if who == "mp2" else request.getfixturevalue(who)
    c = admin_for(actor)
    resp = c.post(change_url(mp), form_data(mp, is_active=None))
    assert resp.status_code == 403
    assert c.post(f"/admin/access/user/{mp.pk}/password/",
                  {"password1": "N3w-long-passphrase!", "password2": "N3w-long-passphrase!"}).status_code == 403
    mp.refresh_from_db()
    assert mp.is_active and not AuditLog.objects.filter(object_id=str(mp.pk), actor=actor).exists()


def test_admin_requires_otp_verified_session(alm, mp, chairman):
    from django.test import Client

    chairman.is_staff = True
    chairman.save()
    c = Client()
    c.force_login(chairman)  # password session without a verified second factor
    resp = c.get(change_url(mp))
    assert resp.status_code == 302 and "/login/" in resp["Location"]


# ---------------------------------------------------------------- 4. bulk changes and the system user


def test_bulk_update_and_delete_refused_when_rows_include_system_user(make_user):
    system = access.system_user()
    u = make_user("someone")
    for call in (lambda: User.objects.all().update(is_active=False),
                 lambda: User.objects.filter(username="system").update(email="x@example.test"),
                 lambda: User.objects.filter(pk=u.pk).update(username="system"),
                 lambda: User.objects.all().delete(),
                 lambda: User.objects.bulk_update([system], ["is_active"]),
                 lambda: system.delete()):
        with pytest.raises(BilingualError) as e:
            call()
        assert e.value.code == "system_user"
    u.username = "system"
    with pytest.raises(BilingualError):
        User.objects.bulk_update([u], ["username"])
    system.refresh_from_db()
    assert system.is_active and User.objects.filter(username="system").count() == 1
    assert User.objects.get(pk=u.pk).username == "someone"
    assert User.objects.filter(pk=u.pk).update(language="ar") == 1  # other rows still work


# ---------------------------------------------------------------- 5. editor table constraint name


def test_request_editor_unique_constraint_name_matches_migration_state():
    from approvals.models import RequestEditor

    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(cursor, RequestEditor._meta.db_table)
    pair = [n for n, c in constraints.items() if c["unique"] and c["columns"] == ["approvalrequest_id", "user_id"]]
    assert pair == ["unique_request_editor"]
    assert [c.name for c in RequestEditor._meta.constraints] == pair


@pytest.mark.django_db(transaction=True)
def test_request_editor_migration_is_reversible():
    from django.db.migrations.executor import MigrationExecutor

    def pair_names():
        with connection.cursor() as cursor:
            cons = connection.introspection.get_constraints(cursor, "approvals_approvalrequest_document_editors")
        return [n for n, c in cons.items() if c["unique"] and c["columns"] == ["approvalrequest_id", "user_id"]]

    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes("approvals")
    executor.migrate([("approvals", "0003_review_a_editors")])
    assert pair_names() and pair_names() != ["unique_request_editor"]
    executor = MigrationExecutor(connection)
    executor.migrate(latest)
    assert pair_names() == ["unique_request_editor"]


# ---------------------------------------------------------------- 6. own username / Keycloak ID


def test_users_cannot_change_own_username_or_keycloak_id(alm, mp, admin_for):
    import uuid

    ma = site._registry[User]
    assert {"username", "keycloak_id"} <= set(ma.get_readonly_fields(req(mp), mp))
    for field, value in (("username", "mp-new"), ("keycloak_id", uuid.uuid4())):
        me = User.objects.get(pk=mp.pk)
        setattr(me, field, value)
        with pytest.raises(PermissionDenied):
            ma.save_model(req(mp), me, form=None, change=True)
    c = admin_for(mp)
    resp = c.post(change_url(mp), form_data(User.objects.get(pk=mp.pk), username="mp-new",
                                            keycloak_id=str(uuid.uuid4()), full_name_en="Mp Own Edit"))
    assert resp.status_code == 302
    mp.refresh_from_db()
    assert mp.username == "mp" and mp.keycloak_id is None and mp.full_name_en == "Mp Own Edit"


def test_setup_mode_may_change_own_username(alm, mp):
    ma = site._registry[User]
    with override_settings(ERP_ADMIN_SETUP_MODE=True):
        mp.is_superuser = True
        mp.save()
        assert "username" not in ma.get_readonly_fields(req(mp), mp)
        mp.username = "mp-renamed"
        ma.save_model(req(mp), mp, form=None, change=True)
    assert User.objects.filter(username="mp-renamed").exists()


def test_manager_may_still_change_another_users_username(alm, user_manager, make_user):
    ma = site._registry[User]
    acc = make_user("acc", (alm, "accountant"))
    assert "username" not in ma.get_readonly_fields(req(user_manager), acc)
    acc.username = "acc-renamed"
    ma.save_model(req(user_manager), acc, form=None, change=True)
    assert User.objects.filter(username="acc-renamed").exists()


# ---------------------------------------------------------------- 7. bulk_update on editor records


def test_editor_records_refuse_bulk_update(alm, accountant, make_payment):
    from datetime import date

    from approvals import services as approvals
    from approvals.models import RequestEditor
    from ledger import services
    from ledger.models import JournalEntryEditor, LedgerError

    e = services.create_entry(alm, date(2027, 1, 10), "Draft", LINES, user=accountant)
    row = e.editors.get()
    row.action = "edit"
    with pytest.raises(LedgerError):
        JournalEntryEditor.objects.bulk_update([row], ["action"])
    assert e.editors.get().action == "create"
    r = approvals.submit(make_payment(accountant, alm), accountant)
    editor = RequestEditor.objects.get(approvalrequest=r)
    editor.user = access.system_user()
    with pytest.raises(BilingualError):
        RequestEditor.objects.bulk_update([editor], ["user"])
    assert list(r.document_editors.all()) == [accountant]


# ---------------------------------------------------------------- 8. D20 role data


def test_d20_service_users_cannot_be_given_submitting_roles(alm, agent, mp):
    assert "approvals.request.submit" in access.SERVICE_FORBIDDEN
    assert not access.has_perm(agent, alm, "approvals.request.submit")
    with pytest.raises(RuleError):
        access.grant_access(mp, agent, alm, "pa_drafter")
    access.grant_access(mp, agent, alm, "agent_drafter")  # drafts-only role is still allowed
