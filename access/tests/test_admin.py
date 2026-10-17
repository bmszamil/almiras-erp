"""Django admin review item 5 and items 2+3: entity scoping, gated post/reverse actions, system drafts
locked, periods cannot be reopened, user/role changes audited, policies through services."""
from datetime import date
from decimal import Decimal

import pytest
from django.contrib.admin.sites import site
from django.test import RequestFactory, override_settings

from access.models import AuditLog, Permission, Role, User
from approvals.models import ApprovableType, ApprovalPolicy
from ledger import services
from ledger.models import FiscalPeriod, JournalEntry

pytestmark = pytest.mark.django_db
LINES = [{"account": "5200", "debit": 100}, {"account": "2300", "credit": 100}]
CHANGELIST = "/admin/ledger/journalentry/"


@pytest.fixture
def admin_for(api):
    def make(user):
        user.is_staff = True
        user.save()
        return api(user)  # password + verified TOTP session (D14)

    return make


def system_draft(entity):
    return services.create_entry(entity, date(2027, 1, 10), "System draft", LINES, system=True)


def system_posted(entity):
    return services.create_entry(entity, date(2027, 1, 10), "System posted", LINES, system=True, post=True)


def run_action(client, action, entries, **extra):
    return client.post(CHANGELIST, {"action": action, "_selected_action": [e.pk for e in entries], **extra})


def model_admin(model):
    return site._registry[model]


def req(user):
    r = RequestFactory().get("/admin/")
    r.user = user
    r.META["REMOTE_ADDR"] = "127.0.0.1"
    return r


# ---------------------------------------------------------------- scoping


def test_admin_changelist_scoped_by_entity_access(alm, sub, make_user, admin_for):
    acc = make_user("acc", (alm, "accountant"))
    alm_entry, sub_entry = system_posted(alm), system_posted(sub)
    c = admin_for(acc)
    html = c.get(CHANGELIST).content.decode()
    assert f"/admin/ledger/journalentry/{alm_entry.pk}/change/" in html
    assert f"/admin/ledger/journalentry/{sub_entry.pk}/change/" not in html
    assert c.get(f"/admin/ledger/journalentry/{sub_entry.pk}/change/").status_code == 302  # not visible
    qs = model_admin(JournalEntry).get_queryset(req(acc))
    assert list(qs) == [alm_entry]


def test_superuser_is_scoped_unless_setup_mode(alm, sub, make_user):
    su = make_user("root", (alm, "viewer"))
    su.is_superuser = su.is_staff = True
    su.save()
    system_posted(alm), system_posted(sub)
    assert model_admin(JournalEntry).get_queryset(req(su)).count() == 1
    with override_settings(ERP_ADMIN_SETUP_MODE=True):
        assert model_admin(JournalEntry).get_queryset(req(su)).count() == 2


def test_audit_admin_scoped_and_hides_ip_without_view_security(alm, chairman, mp):
    from access.services import Context, audit

    row = audit(mp, "test.row", alm, ctx=Context(channel="web", ip="10.9.9.9"))
    ma = model_admin(AuditLog)
    assert "ip" in ma.get_fields(req(mp), row)
    assert "ip" not in ma.get_fields(req(chairman), row)


# ---------------------------------------------------------------- items 2+3: post_selected


def test_post_selected_only_system_entries_and_only_with_post_permission(alm, mp, make_user, admin_for):
    acc = make_user("acc", (alm, "accountant"))
    sys_entry = system_draft(alm)
    manual = services.create_entry(alm, date(2027, 1, 10), "Manual", LINES, user=acc)
    run_action(admin_for(acc), "post_selected", [sys_entry])
    assert JournalEntry.objects.get(pk=sys_entry.pk).status == "draft"  # accountant lacks ledger.entry.post
    run_action(admin_for(mp), "post_selected", [sys_entry, manual])
    assert JournalEntry.objects.get(pk=sys_entry.pk).status == "posted"
    assert JournalEntry.objects.get(pk=manual.pk).status == "draft"  # manual entries post only by approval


def test_system_draft_header_and_lines_not_editable_in_admin(alm, mp, make_user):
    acc = make_user("acc", (alm, "accountant"))
    sys_entry = system_draft(alm)
    manual = services.create_entry(alm, date(2027, 1, 10), "Manual", LINES, user=acc)
    ma = model_admin(JournalEntry)
    inline = ma.get_inline_instances(req(mp), sys_entry)
    for user in (mp, acc):
        assert ma.has_change_permission(req(user), sys_entry) is False
        assert all(not i.has_change_permission(req(user), sys_entry) and not i.has_add_permission(req(user), sys_entry)
                   and not i.has_delete_permission(req(user), sys_entry) for i in inline)
    assert ma.has_change_permission(req(acc), manual) is True


def test_system_draft_change_post_is_refused(alm, mp, admin_for):
    sys_entry = system_draft(alm)
    resp = admin_for(mp).post(f"/admin/ledger/journalentry/{sys_entry.pk}/change/", {"description": "hacked"})
    assert resp.status_code == 403  # view-only object: POST refused
    assert JournalEntry.objects.get(pk=sys_entry.pk).description == "System draft"


# ---------------------------------------------------------------- D15: reverse_selected


def test_reverse_selected_asks_for_reason_then_reverses_with_audit(alm, make_user, admin_for):
    acc = make_user("acc", (alm, "accountant"))
    e = system_posted(alm)
    c = admin_for(acc)
    resp = run_action(c, "reverse_selected", [e])
    assert resp.status_code == 200 and b"name=\"reason\"" in resp.content
    assert JournalEntry.objects.get(pk=e.pk).status == "posted"
    run_action(c, "reverse_selected", [e], reason_confirmed="1", reason="")
    assert JournalEntry.objects.get(pk=e.pk).status == "posted"
    run_action(c, "reverse_selected", [e], reason_confirmed="1", reason="Booked twice")
    assert JournalEntry.objects.get(pk=e.pk).status == "reversed"
    row = AuditLog.objects.get(action="entry.reverse", object_id=str(e.pk))
    assert row.actor == acc and row.after["reason"] == "Booked twice"


def test_reverse_selected_refused_without_permission(alm, make_user, admin_for):
    viewer = make_user("viewer", (alm, "viewer"))
    e = system_posted(alm)
    run_action(admin_for(viewer), "reverse_selected", [e], reason_confirmed="1", reason="try")
    assert JournalEntry.objects.get(pk=e.pk).status == "posted"


# ---------------------------------------------------------------- periods


def test_period_admin_cannot_reopen_or_edit(alm, mp, admin_for):
    p = services.close_period(FiscalPeriod.objects.get(entity=alm, name="2027-02"), user=mp)
    ma = model_admin(FiscalPeriod)
    assert ma.has_change_permission(req(mp), p) is False
    assert "status" in ma.get_readonly_fields(req(mp), p)
    admin_for(mp).post(f"/admin/ledger/fiscalperiod/{p.pk}/change/", {"status": "open", "name": p.name})
    assert FiscalPeriod.objects.get(pk=p.pk).status == "closed"


def test_period_admin_close_action_uses_service(alm, mp, make_user, admin_for):
    acc = make_user("acc", (alm, "accountant"))
    p = FiscalPeriod.objects.get(entity=alm, name="2027-03")
    admin_for(acc).post("/admin/ledger/fiscalperiod/", {"action": "close_selected", "_selected_action": [p.pk]})
    assert FiscalPeriod.objects.get(pk=p.pk).status == "open"
    admin_for(mp).post("/admin/ledger/fiscalperiod/", {"action": "close_selected", "_selected_action": [p.pk]})
    assert FiscalPeriod.objects.get(pk=p.pk).status == "closed"
    assert AuditLog.objects.filter(action="period.close", actor=mp).exists()


# ---------------------------------------------------------------- users and roles audited


def _fake_form(obj):
    return type("F", (), {"instance": obj, "save_m2m": lambda self: None})()


def test_user_privilege_change_is_audited_and_superuser_readonly(alm, mp, make_user):
    target = make_user("target", (alm, "viewer"))
    ma = model_admin(User)
    assert {"is_superuser", "user_type"} <= set(ma.get_readonly_fields(req(mp), target))
    request = req(mp)
    target.is_staff = True
    ma.save_model(request, target, form=None, change=True)
    ma.save_related(request, _fake_form(target), [], True)
    row = AuditLog.objects.get(action="access.user.privilege_change", object_id=str(target.pk))
    assert row.before["is_staff"] is False and row.after["is_staff"] is True


def test_user_type_change_only_in_setup_mode(alm, mp, make_user):
    from django.core.exceptions import PermissionDenied

    target = make_user("target", (alm, "viewer"))
    ma = model_admin(User)
    target.user_type = "service_integration"
    with pytest.raises(PermissionDenied):
        ma.save_model(req(mp), target, form=None, change=True)
    assert User.objects.get(pk=target.pk).user_type == "human"
    with override_settings(ERP_ADMIN_SETUP_MODE=True):
        mp.is_superuser = True
        mp.save()
        ma.save_model(req(mp), target, form=None, change=True)
    target.refresh_from_db()
    assert target.user_type == "service_integration" and target.is_staff is False


def test_role_permission_edit_is_audited(alm, mp, admin_for):
    role = Role.objects.get(code="viewer")
    perm = Permission.objects.get(code="reports.report.view")
    current = list(role.permissions.values_list("pk", flat=True))
    resp = admin_for(mp).post(f"/admin/access/role/{role.pk}/change/",
                              {"name_en": role.name_en, "name_ar": role.name_ar, "permissions": current + [perm.pk]})
    assert resp.status_code == 302, resp.content[:500]
    row = AuditLog.objects.get(action="access.role.change", object_id=str(role.pk))
    assert "reports.report.view" not in row.before["permissions"] and "reports.report.view" in row.after["permissions"]


def test_role_edit_needs_user_manage_in_root_entity(alm, sub, make_user):
    sub_mp = make_user("submp", (sub, "managing_partner"))
    assert model_admin(Role).has_change_permission(req(sub_mp)) is False


def test_groups_and_totp_devices_not_in_admin():
    from django.contrib.auth.models import Group
    from django_otp.plugins.otp_totp.models import TOTPDevice

    assert not site.is_registered(Group) and not site.is_registered(TOTPDevice)


# ---------------------------------------------------------------- policies through services


def test_policy_admin_checks_permission_and_audits(alm, sub, mp, make_user):
    from access.errors import AccessDenied  # noqa: F401
    from django.core.exceptions import PermissionDenied

    acc = make_user("acc", (alm, "accountant"))
    ma = model_admin(ApprovalPolicy)
    assert ma.has_add_permission(req(acc)) is False and ma.has_add_permission(req(mp)) is True
    t = ApprovableType.objects.get(code="ledger.journal_entry")
    p = ApprovalPolicy(entity=sub, document_type=t, currency="SAR", priority=4, name_en="x", name_ar="x")
    with pytest.raises(PermissionDenied):
        ma.save_model(req(mp), p, form=None, change=False)  # MP has no rights in LZM (D12)
    p.entity = alm
    request = req(mp)
    ma.save_model(request, p, form=None, change=False)
    ma.save_related(request, _fake_form(p), [], False)
    assert AuditLog.objects.filter(action="policy.create", object_id=str(p.pk), actor=mp).exists()
    p.currency = "USD"
    with pytest.raises(PermissionDenied):
        ma.save_model(req(mp), p, form=None, change=True)
    assert ApprovalPolicy.objects.get(pk=p.pk).currency == "SAR"
    assert "entity" in ma.get_readonly_fields(req(mp), p)
