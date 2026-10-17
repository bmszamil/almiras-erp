"""Re-review fixes (17 Oct 2026): A editors and SoD, B user management scope, D bulk-change locking,
E lockout counting and unlock, F line accounts, H posting failure under ATOMIC_REQUESTS."""
import uuid
from datetime import date

import pytest
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.db import connection
from django.test import RequestFactory

from access import services as access
from access.errors import AccessDenied
from access.models import AuditLog, Permission, Role, User
from approvals import services as approvals
from approvals.models import Notification
from ledger import services
from ledger.models import Account, JournalEntry, JournalEntryEditor, LedgerError

pytestmark = pytest.mark.django_db
LINES = [{"account": "5200", "debit": 100}, {"account": "2300", "credit": 100}]


def req(user):
    r = RequestFactory().get("/admin/")
    r.user = user
    return r


def model_admin(model):
    from django.contrib.admin.sites import site

    return site._registry[model]


def draft(entity, user):
    return services.create_entry(entity, date(2027, 1, 10), "Draft", LINES, user=user)


def approve(r, user):
    r.refresh_from_db()
    return approvals.act(r, user, "approve", document_version=r.document_version)


# ---------------------------------------------------------------- A


def test_a_only_the_creator_can_edit_a_draft_in_admin(alm, mp, make_user):
    faisal = make_user("faisal", (alm, "pa_drafter"))
    e = draft(alm, faisal)
    ma = model_admin(JournalEntry)
    assert ma.has_change_permission(req(faisal), e) is True
    assert ma.has_change_permission(req(mp), e) is False
    assert ma.has_delete_permission(req(mp), e) is False


def test_a_creator_and_admin_edits_are_recorded(alm, make_user):
    faisal = make_user("faisal", (alm, "pa_drafter"))
    e = draft(alm, faisal)
    ma = model_admin(JournalEntry)
    request = req(faisal)
    request.META["REMOTE_ADDR"] = "127.0.0.1"
    e.description = "Edited"
    ma.save_model(request, e, form=None, change=True)
    ma.save_related(request, type("F", (), {"instance": e, "save_m2m": lambda self: None})(), [], True)
    assert list(JournalEntryEditor.objects.filter(entry=e).values_list("user__username", "action")) == [
        ("faisal", "create"), ("faisal", "edit")]


def test_a_editor_cannot_approve_and_mp_editor_is_logged_as_self_approval(alm, mp, make_user):
    faisal = make_user("faisal", (alm, "pa_drafter"))
    e = draft(alm, faisal)
    JournalEntryEditor.objects.create(entity=alm, entry=e, user=mp, action="edit")  # MP touched the draft
    r = approvals.submit(e, faisal)
    assert set(r.document_editors.values_list("username", flat=True)) == {"faisal", "mp"}
    r = approve(r, mp)  # MP is the only approver: allowed only as a logged D10 override
    a = r.actions.get(action="approve")
    assert a.self_approval_override is True
    assert AuditLog.objects.filter(action="approval.self_approval_override", object_id=str(r.pk)).exists()


def test_a_editor_refused_when_another_approver_exists(alm, mp, make_user):
    other = make_user("mp2", (alm, "managing_partner"))
    faisal = make_user("faisal", (alm, "pa_drafter"))
    e = draft(alm, faisal)
    JournalEntryEditor.objects.create(entity=alm, entry=e, user=mp, action="edit")
    r = approvals.submit(e, faisal)
    with pytest.raises(AccessDenied):
        approve(r, mp)
    assert approve(r, other).status == "approved"


def test_a_editor_records_are_append_only(alm, make_user):
    e = draft(alm, make_user("faisal", (alm, "pa_drafter")))
    row = e.editors.get()
    row.action = "edit"
    with pytest.raises(LedgerError):
        row.save()


# ---------------------------------------------------------------- B


@pytest.fixture
def alm_user_admin(make_user, alm):
    role = Role.objects.create(code="user_admin_test", name_en="User admin", name_ar="مدير مستخدمين")
    role.permissions.add(*Permission.objects.filter(code__in=["access.user.manage", "core.entity.view"]))
    return make_user("ua", (alm, "user_admin_test"))


def test_b_cannot_manage_user_with_grants_outside_own_entities(alm, sub, make_user, alm_user_admin):
    both = make_user("both", (alm, "accountant"), (sub, "accountant"))
    only_alm = make_user("onlyalm", (alm, "accountant"))
    assert access.can_manage_user(alm_user_admin, only_alm) is True
    assert access.can_manage_user(alm_user_admin, both) is False
    with pytest.raises(AccessDenied):
        access.deactivate_user(alm_user_admin, both)
    assert User.objects.get(pk=both.pk).is_active is True
    ma = model_admin(User)
    assert ma.has_change_permission(req(alm_user_admin), both) is False
    assert {"is_active", "is_staff"} <= set(ma.get_readonly_fields(req(alm_user_admin), both))
    both.is_active = False
    with pytest.raises(PermissionDenied):
        ma.save_model(req(alm_user_admin), both, form=None, change=True)
    with pytest.raises(PermissionDenied):
        ma.user_change_password(req(alm_user_admin), str(both.pk))


def test_b_nobody_changes_own_privileges(alm, mp):
    ma = model_admin(User)
    assert {"is_active", "is_staff", "is_superuser", "user_type"} <= set(ma.get_readonly_fields(req(mp), mp))
    mp.is_staff = True
    with pytest.raises(PermissionDenied):
        ma.save_model(req(mp), mp, form=None, change=True)
    with pytest.raises(AccessDenied):
        access.deactivate_user(mp, mp)
    assert access.can_manage_user(mp, mp) is False


def test_b_own_profile_still_editable(alm, mp):
    ma = model_admin(User)
    assert ma.has_change_permission(req(mp), mp) is True
    mp.full_name_en = "Managing Partner"
    request = req(mp)
    request.META["REMOTE_ADDR"] = "127.0.0.1"
    ma.save_model(request, mp, form=None, change=True)
    assert User.objects.get(pk=mp.pk).full_name_en == "Managing Partner"


def test_b_superuser_target_needs_setup_mode(alm, mp, make_user):
    root = make_user("root", (alm, "viewer"))
    root.is_superuser = True
    root.save()
    assert access.can_manage_user(mp, root) is False


# ---------------------------------------------------------------- D


def test_d_mixed_queryset_refused_and_nothing_changes(alm, accountant):
    d1 = draft(alm, accountant)
    p = services.create_entry(alm, date(2027, 1, 11), "Sys", LINES, system=True, post=True)
    with pytest.raises(LedgerError):
        JournalEntry.objects.filter(pk__in=[d1.pk, p.pk]).update(description="bulk")
    assert set(JournalEntry.objects.values_list("description", flat=True)) == {"Draft", "Sys"}
    with pytest.raises(LedgerError):
        JournalEntry.objects.filter(pk__in=[d1.pk, p.pk]).delete()
    assert JournalEntry.objects.count() == 2


def test_d_update_applies_only_to_locked_draft_rows(alm, accountant, monkeypatch):
    """If a row stops being a draft between selection and update, the counts differ and nothing changes."""
    d1, d2 = draft(alm, accountant), draft(alm, accountant)
    from ledger.models import JournalEntryQuerySet

    real = JournalEntryQuerySet._locked_drafts

    def racing(self, what):
        with connection.cursor() as cur:  # simulate a concurrent post of d2 just before locking
            cur.execute("UPDATE ledger_journalentry SET status='posted' WHERE id=%s", [d2.pk])
        return real(self, what)

    monkeypatch.setattr(JournalEntryQuerySet, "_locked_drafts", racing)
    with pytest.raises(LedgerError):
        JournalEntry.objects.filter(pk__in=[d1.pk, d2.pk]).update(description="bulk")
    assert JournalEntry.objects.get(pk=d1.pk).description == "Draft"


# ---------------------------------------------------------------- E


def _fail(username, n, ip="198.51.100.1"):
    from django.contrib.auth import authenticate

    rf = RequestFactory()
    for _ in range(n):
        authenticate(rf.post("/admin/login/", REMOTE_ADDR=ip), username=username, password="wrong")


def _ok(user, ip="198.51.100.1"):
    from django.contrib.auth import authenticate

    return authenticate(RequestFactory().post("/admin/login/", REMOTE_ADDR=ip), username=user.username,
                        password="a-Long-test-pass-91")


@pytest.fixture
def pw_user(make_user, alm):
    u = make_user("pwuser", (alm, "viewer"))
    u.set_password("a-Long-test-pass-91")
    u.save()
    return u


def test_e_attempts_while_locked_are_not_counted(pw_user):
    _fail("pwuser", 5)
    _fail("pwuser", 3)
    assert AuditLog.objects.filter(action="access.sign_in_failed").count() == 5
    assert AuditLog.objects.filter(action="access.sign_in_locked").count() == 3


def test_e_unlock_command_is_audited_and_clears_lock(pw_user):
    _fail("pwuser", 5)
    assert _ok(pw_user) is None
    call_command("unlock_user", "pwuser", stdout=open("/dev/null", "w"))
    assert AuditLog.objects.filter(action="access.unlock", object_id=str(pw_user.pk)).exists()
    assert _ok(pw_user) == pw_user


def test_e_admin_unlock_needs_user_manage(alm, pw_user, accountant, mp):
    with pytest.raises(AccessDenied):
        access.unlock_user(accountant, pw_user)
    access.unlock_user(mp, pw_user)
    assert AuditLog.objects.get(action="access.unlock").actor == mp
    assert "unlock_selected" in model_admin(User).get_actions(req(mp))


# ---------------------------------------------------------------- F


def test_f_line_accounts_limited_to_entry_entity(alm, sub, make_user):
    u = make_user("both", (alm, "accountant"), (sub, "accountant"))
    e = draft(alm, u)
    inline = model_admin(JournalEntry).get_inline_instances(req(u), e)[0]
    qs = inline.get_formset(req(u), e).form.base_fields["account"].queryset
    assert qs.exists() and set(qs.values_list("entity_id", flat=True)) == {alm.pk}
    # Add form: choices span entities, but the formset refuses a line from another entity.
    FormSet = inline.get_formset(req(u), None)
    data = {"lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "0", "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000", "lines-0-account": str(Account.objects.get(entity=sub, code="5200").pk),
            "lines-0-debit": "1", "lines-0-credit": "0"}
    fs = FormSet(data, instance=JournalEntry(entity=alm, date=date(2027, 1, 1), description="x"), prefix="lines")
    assert not fs.is_valid() and fs.non_form_errors()


# ---------------------------------------------------------------- H


def test_h_posting_failure_via_api_with_atomic_requests(alm, mp, accountant, api, monkeypatch):
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", True)
    e = draft(alm, accountant)
    r = approvals.submit(e, accountant)
    Account.objects.filter(entity=alm, code="5200").update(is_active=False)
    c = api(mp)
    key = str(uuid.uuid4())
    url = f"/api/v1/approvals/requests/{r.pk}/approve"
    resp = c.post(url, {"document_version": r.document_version}, format="json", HTTP_IDEMPOTENCY_KEY=key)
    assert resp.status_code == 422 and resp.json()["code"] == "posting_failed"
    assert resp.json()["message_ar"]
    r.refresh_from_db()
    e.refresh_from_db()
    assert r.status == "pending" and e.status == "pending_approval" and e.number == ""
    assert not r.actions.filter(action="approve").exists()
    assert AuditLog.objects.filter(action="approval.posting_failed", object_id=str(r.pk)).exists()
    assert Notification.objects.filter(alert_code="posting_failed", user=mp).exists()
    again = c.post(url, {"document_version": r.document_version}, format="json", HTTP_IDEMPOTENCY_KEY=key)
    assert again.status_code == 422 and again.json() == resp.json()
