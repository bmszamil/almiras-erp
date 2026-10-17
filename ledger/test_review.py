"""Review fixes (17 Oct 2026): guarded querysets (1), safe default for approval (2), D15 reversals (6),
periods cannot be reopened by default, D17 integrations drafts-only (8)."""
from datetime import date
from decimal import Decimal

import pytest

from access.models import AuditLog
from approvals import services as approvals
from ledger import services
from ledger.models import Account, FiscalPeriod, JournalEntry, JournalLine, LedgerError

pytestmark = pytest.mark.django_db
D = Decimal
S = JournalEntry.Status
LINES = [{"account": "5200", "debit": 100}, {"account": "2300", "credit": 100}]


def posted(entity, user=None):
    return services.create_entry(entity, date(2027, 1, 10), "System entry", LINES, user=user, post=True, system=True)


def draft(entity, user=None):
    return services.create_entry(entity, date(2027, 1, 10), "Manual draft", LINES, user=user)


# ---------------------------------------------------------------- 1. guarded querysets


def test_bulk_update_of_posted_entries_refused(alm):
    e = posted(alm)
    with pytest.raises(LedgerError):
        JournalEntry.objects.filter(pk=e.pk).update(description="changed")
    assert JournalEntry.objects.get(pk=e.pk).description == "System entry"


def test_bulk_delete_of_posted_entries_refused(alm):
    posted(alm)
    draft(alm)
    with pytest.raises(LedgerError):
        JournalEntry.objects.all().delete()
    assert JournalEntry.objects.count() == 2


def test_bulk_status_change_refused_even_on_drafts(alm):
    e = draft(alm)
    for field, value in [("status", "posted"), ("requires_approval", False), ("number", "X-1"), ("source", "human")]:
        with pytest.raises(LedgerError):
            JournalEntry.objects.filter(pk=e.pk).update(**{field: value})
    assert JournalEntry.objects.get(pk=e.pk).status == S.DRAFT


def test_bulk_update_and_delete_allowed_on_drafts(alm):
    e = draft(alm)
    assert JournalEntry.objects.filter(pk=e.pk).update(description="fixed") == 1
    JournalLine.objects.filter(entry=e).update(description="line note")
    JournalEntry.objects.filter(pk=e.pk).delete()
    assert not JournalEntry.objects.exists()


def test_bulk_update_and_delete_of_locked_lines_refused(alm, accountant):
    e = posted(alm)
    with pytest.raises(LedgerError):
        JournalLine.objects.filter(entry=e).update(debit=D("1"))
    with pytest.raises(LedgerError):
        JournalLine.objects.filter(entry=e).delete()
    pending = draft(alm, accountant)
    approvals.submit(pending, accountant)
    with pytest.raises(LedgerError):
        JournalLine.objects.filter(entry=pending).update(description="x")


def test_lines_cannot_be_moved_between_entries(alm):
    a, b = draft(alm), draft(alm)
    with pytest.raises(LedgerError):
        JournalLine.objects.filter(entry=a).update(entry=b)


def test_bulk_create_cannot_create_posted_or_system_entries(alm):
    with pytest.raises(LedgerError):
        JournalEntry.objects.bulk_create([JournalEntry(entity=alm, date=date(2027, 1, 1), description="x", status=S.POSTED)])
    with pytest.raises(LedgerError):
        JournalEntry.objects.bulk_create([JournalEntry(entity=alm, date=date(2027, 1, 1), description="x", requires_approval=False)])


def test_bulk_create_lines_on_posted_entry_refused(alm):
    e = posted(alm)
    with pytest.raises(LedgerError):
        JournalLine.objects.bulk_create([JournalLine(entry=e, account=Account.objects.get(entity=alm, code="1100"), debit=1)])


def test_period_status_cannot_change_in_bulk_or_directly(alm):
    with pytest.raises(LedgerError):
        FiscalPeriod.objects.filter(entity=alm).update(status="closed")
    p = FiscalPeriod.objects.get(entity=alm, name="2027-03")
    p.status = "closed"
    with pytest.raises(LedgerError):
        p.save()
    assert FiscalPeriod.objects.get(pk=p.pk).status == "open"


# ---------------------------------------------------------------- 2. approval is the default


def test_create_entry_by_human_requires_approval_by_default(alm, mp):
    e = draft(alm, mp)
    assert e.requires_approval is True
    with pytest.raises(LedgerError):
        services.create_entry(alm, date(2027, 1, 10), "x", LINES, user=mp, post=True)
    with pytest.raises(LedgerError, match="needs approval"):
        services.post_entry(e, user=mp)


def test_create_entry_without_user_requires_approval_too(alm):
    assert draft(alm).requires_approval is True


def test_system_flag_skips_approval_for_module_services(alm):
    from access.services import system_user

    for user in (None, system_user()):
        e = posted(alm, user)
        assert e.requires_approval is False and e.status == S.POSTED


@pytest.mark.parametrize("role", ["managing_partner", "accountant"])
def test_review_c_system_flag_refused_for_human_users(alm, make_user, role):
    u = make_user(f"h-{role}", (alm, role))
    with pytest.raises(LedgerError, match="module services"):
        services.create_entry(alm, date(2027, 1, 10), "x", LINES, user=u, system=True)
    with pytest.raises(LedgerError, match="module services"):
        services.reverse_entry(posted(alm), user=u, reason="x", system=True)
    assert not JournalEntry.objects.filter(requires_approval=False, created_by=u).exists()


def test_review_c_system_username_is_reserved():
    from access.models import User

    u = User.objects.get(username="system")
    assert u.user_type == "service_integration" and not u.has_usable_password()


# ---------------------------------------------------------------- 8. D17 integrations


@pytest.fixture
def integration(make_user, alm):
    return make_user("bank-feed", (alm, "agent_drafter"), user_type="service_integration")


def test_d17_integration_creates_drafts_only(alm, integration):
    e = draft(alm, integration)
    assert e.requires_approval and e.source == "import"
    with pytest.raises(LedgerError):
        services.create_entry(alm, date(2027, 1, 10), "x", LINES, user=integration, system=True)
    with pytest.raises(LedgerError):
        services.post_entry(e, user=integration)


def test_d17_agent_cannot_create_system_entry(alm, agent):
    with pytest.raises(LedgerError):
        services.create_entry(alm, date(2027, 1, 10), "x", LINES, user=agent, system=True, post=True)


# ---------------------------------------------------------------- 6. D15 reversals


def test_d15_accountant_reverses_without_approval_and_audit_has_reason(alm, accountant):
    e = posted(alm)
    rev = services.reverse_entry(e, user=accountant, reason="Duplicate of ALM-JE-2027-000009")
    assert rev.status == S.POSTED and rev.requires_approval is False
    row = AuditLog.objects.get(action="entry.reverse", object_id=str(e.pk))
    assert row.actor == accountant and row.after["reason"] == "Duplicate of ALM-JE-2027-000009"
    assert row.after["reversal_id"] == rev.pk


def test_d15_reason_is_mandatory(alm, accountant):
    e = posted(alm)
    with pytest.raises(LedgerError, match="reason"):
        services.reverse_entry(e, user=accountant, reason="   ")
    assert JournalEntry.objects.get(pk=e.pk).status == S.POSTED


@pytest.mark.parametrize("role", ["viewer", "auditor", "pa_drafter", "business_analyst", "cfo"])
def test_d15_users_without_reverse_or_post_permission_refused(alm, make_user, role):
    u = make_user(f"u-{role}", (alm, role))
    e = posted(alm)
    with pytest.raises(LedgerError, match="permission"):
        services.reverse_entry(e, user=u, reason="test")
    assert JournalEntry.objects.get(pk=e.pk).status == S.POSTED


def test_d15_permission_must_be_in_the_entrys_entity(alm, sub, make_user):
    u = make_user("subacc", (sub, "accountant"))
    with pytest.raises(LedgerError):
        services.reverse_entry(posted(alm), user=u, reason="wrong entity")


def test_d15_post_permission_also_allows_reversal(alm, mp):
    assert services.reverse_entry(posted(alm), user=mp, reason="Correction").status == S.POSTED


def test_d15_no_user_needs_system_flag(alm):
    with pytest.raises(LedgerError):
        services.reverse_entry(posted(alm), reason="no user")


def test_d15_service_users_cannot_reverse(alm, agent):
    with pytest.raises(LedgerError):
        services.reverse_entry(posted(alm), user=agent, reason="agent", system=True)


# ---------------------------------------------------------------- periods


def test_close_period_needs_permission_and_is_audited(alm, accountant, mp):
    p = FiscalPeriod.objects.get(entity=alm, name="2027-02")
    with pytest.raises(LedgerError):
        services.close_period(p, user=accountant)
    services.close_period(p, user=mp)
    assert AuditLog.objects.filter(action="period.close", object_id=str(p.pk), actor=mp).exists()


def test_close_period_refuses_drafts_dated_in_period(alm, accountant):
    draft(alm, accountant)  # dated 2027-01-10, no period assigned yet
    with pytest.raises(LedgerError, match="Draft"):
        services.close_period(FiscalPeriod.objects.get(entity=alm, name="2027-01"))


def test_reopen_is_denied_by_default_even_for_mp(alm, mp):
    p = services.close_period(FiscalPeriod.objects.get(entity=alm, name="2027-02"), user=mp)
    with pytest.raises(LedgerError):
        services.reopen_period(p, mp, reason="late invoice")
    assert FiscalPeriod.objects.get(pk=p.pk).status == "closed"


def test_reopen_with_explicit_permission_needs_reason_and_is_audited(alm, mp):
    from access.models import Permission, Role

    role = Role.objects.create(code="period_reopener", name_en="Period reopener", name_ar="معيد فتح الفترات")
    role.permissions.add(Permission.objects.get(code="ledger.period.reopen"))
    from access import services as access

    access.grant_access(mp, mp, alm, role)
    p = services.close_period(FiscalPeriod.objects.get(entity=alm, name="2027-02"), user=mp)
    with pytest.raises(LedgerError):
        services.reopen_period(p, mp, reason="")
    services.reopen_period(p, mp, reason="Late supplier invoice")
    assert FiscalPeriod.objects.get(pk=p.pk).status == "open"
    assert AuditLog.objects.get(action="period.reopen").after["reason"] == "Late supplier invoice"
