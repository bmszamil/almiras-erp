"""Manual journal entries through approvals: D8, D11, R11, R12, R20, T17."""
from datetime import date
from decimal import Decimal

import pytest

from access.errors import AccessDenied, Conflict
from access.models import AuditLog
from approvals import services as approvals
from approvals.models import ApprovalRequest, Notification
from approvals.services import PostingFailed
from ledger import services
from ledger.models import Account, FiscalPeriod, JournalEntry, JournalLine, LedgerError

pytestmark = pytest.mark.django_db
D = Decimal
S = JournalEntry.Status


def manual(entity, user, amount="100", day=15):
    return services.create_entry(
        entity, date(2027, 1, day), "Office supplies accrual",
        [{"account": "5200", "debit": amount}, {"account": "2300", "credit": amount}],
        user=user,
    )


def approve(r, user):
    r.refresh_from_db()
    return approvals.act(r, user, "approve", document_version=r.document_version)


def test_manual_entry_has_source_and_needs_approval(alm, accountant):
    e = manual(alm, accountant)
    assert e.source == "human" and e.requires_approval and e.status == S.DRAFT
    with pytest.raises(LedgerError, match="needs approval"):
        services.post_entry(e, user=accountant)


def test_manual_entry_cannot_be_created_already_posted(alm, accountant):
    with pytest.raises(LedgerError):
        services.create_entry(alm, date(2027, 1, 2), "x", [{"account": "1100", "debit": 1}, {"account": "3100", "credit": 1}],
                              user=accountant, post=True)


def test_status_cannot_be_set_directly(alm, accountant):
    e = manual(alm, accountant)
    for status in (S.APPROVED, S.POSTED, S.PENDING_APPROVAL):
        e.status = status
        with pytest.raises(LedgerError):
            e.save()
    assert JournalEntry.objects.get(pk=e.pk).status == S.DRAFT


def test_d11_final_approval_posts_in_same_transaction(alm, mp, accountant):
    e = manual(alm, accountant)
    r = approvals.submit(e, accountant)
    e.refresh_from_db()
    assert e.status == S.PENDING_APPROVAL and r.amount == D("100.00")
    r = approve(r, mp)
    e.refresh_from_db()
    assert r.status == "approved" and e.status == S.POSTED and e.number == "ALM-JE-2027-000001"
    assert e.posted_by == mp


def test_r11_pending_entry_and_lines_locked(alm, accountant):
    e = manual(alm, accountant)
    approvals.submit(e, accountant)
    e.refresh_from_db()
    e.description = "changed"
    with pytest.raises(LedgerError):
        e.save()
    line = e.lines.first()
    line.debit = D("1")
    with pytest.raises(LedgerError):
        line.save()
    with pytest.raises(LedgerError):
        JournalLine.objects.create(entry=e, account=Account.objects.get(entity=alm, code="1100"), debit=D("5"))
    with pytest.raises(LedgerError):
        e.delete()


def test_t17_posting_failure_rolls_back_whole_approval(alm, mp, accountant):
    e = manual(alm, accountant)
    r = approvals.submit(e, accountant)
    # A ledger rule now fails at posting time (account closed after submission); content hash unchanged.
    Account.objects.filter(entity=alm, code="5200").update(is_active=False)
    with pytest.raises(PostingFailed) as exc:
        approve(r, mp)
    assert "not open for posting" in exc.value.message_en and exc.value.message_ar
    r.refresh_from_db()
    e.refresh_from_db()
    assert r.status == "pending" and not r.actions.filter(action="approve").exists()
    assert e.status == S.PENDING_APPROVAL and e.number == ""
    assert not AuditLog.objects.filter(action="approval.approve", object_id=str(r.pk)).exists()
    assert AuditLog.objects.filter(action="approval.posting_failed", object_id=str(r.pk)).exists()
    assert Notification.objects.filter(user=mp, alert_code="posting_failed").exists()
    # Once the account is reopened, the same approval succeeds.
    Account.objects.filter(entity=alm, code="5200").update(is_active=True)
    assert approve(r, mp).status == "approved"


def test_t17_sharia_rule_still_enforced_after_approval(alm, mp, accountant):
    e = manual(alm, accountant)
    r = approvals.submit(e, accountant)
    Account.objects.filter(entity=alm, code="5200").update(sharia_prohibited=True)
    with pytest.raises(PostingFailed, match="Sharia"):
        approve(r, mp)


def test_t17_unbalanced_change_is_caught_by_version_check(alm, mp, accountant):
    e = manual(alm, accountant)
    r = approvals.submit(e, accountant)
    # The ORM refuses to change lines of a pending entry (guarded queryset) ...
    with pytest.raises(LedgerError):
        JournalLine.objects.filter(entry=e, credit__gt=0).update(credit=D("90"))
    # ... so simulate tampering below the ORM (raw SQL); the version check must still catch it.
    from django.db import connection

    with connection.cursor() as cur:
        cur.execute("UPDATE ledger_journalline SET credit = 90 WHERE entry_id = %s AND credit > 0", [e.pk])
    with pytest.raises(Conflict):
        approve(r, mp)
    r.refresh_from_db()
    e.refresh_from_db()
    assert r.status == "returned" and e.status == S.DRAFT
    with pytest.raises(Exception, match="balance"):
        approvals.submit(e, accountant)  # unbalanced draft cannot be resubmitted
    with pytest.raises(LedgerError):
        services.post_entry(e, user=mp)


def test_unbalanced_draft_cannot_be_submitted(alm, accountant):
    e = services.create_entry(alm, date(2027, 1, 5), "bad", [{"account": "1100", "debit": 100}, {"account": "3100", "credit": 90}],
                              user=accountant)
    with pytest.raises(Exception, match="balance"):
        approvals.submit(e, accountant)
    assert not ApprovalRequest.objects.exists()
    assert JournalEntry.objects.get(pk=e.pk).status == S.DRAFT


def test_rejected_entry_is_locked_and_never_posts(alm, mp, accountant):
    e = manual(alm, accountant)
    r = approvals.submit(e, accountant)
    approvals.act(r, mp, "reject", comment="Not needed", document_version=r.document_version)
    e.refresh_from_db()
    assert e.status == S.REJECTED
    with pytest.raises(LedgerError):
        services.post_entry(e, user=mp)
    e.description = "retry"
    with pytest.raises(LedgerError):
        e.save()


def test_d8_agent_entry_is_draft_with_agent_source_and_agent_cannot_post(alm, agent, mp):
    e = services.create_entry(alm, date(2027, 1, 3), "Drafted by agent",
                              [{"account": "5200", "debit": 10}, {"account": "2300", "credit": 10}], user=agent)
    assert e.source == "agent:invoice-reader" and e.requires_approval
    with pytest.raises(LedgerError):
        services.post_entry(e, user=agent)
    r = approvals.submit(e, agent)
    assert r.source == "agent:invoice-reader"
    with pytest.raises(AccessDenied):
        approve(r, agent)
    assert approve(r, mp).status == "approved"


def test_mp_self_approved_manual_entry_posts_with_override(alm, mp):
    e = manual(alm, mp)
    r = approve(approvals.submit(e, mp), mp)
    assert r.actions.get(action="approve").self_approval_override
    e.refresh_from_db()
    assert e.status == S.POSTED


def test_pa_drafter_creates_and_submits_but_cannot_approve(alm, mp, make_user):
    faisal = make_user("faisal", (alm, "pa_drafter"))
    e = manual(alm, faisal)
    r = approvals.submit(e, faisal)
    with pytest.raises(AccessDenied):
        approve(r, faisal)


def test_business_analyst_cannot_submit_journal_entry(alm, make_user):
    analyst = make_user("analyst", (alm, "business_analyst"))
    e = manual(alm, analyst)
    with pytest.raises(AccessDenied):
        approvals.submit(e, analyst)


def test_close_period_refuses_entries_in_approval(alm, accountant):
    approvals.submit(manual(alm, accountant), accountant)
    with pytest.raises(LedgerError, match="approval"):
        services.close_period(FiscalPeriod.objects.get(entity=alm, name="2027-01"))


def test_reversal_of_approved_entry_needs_no_second_approval(alm, mp, accountant):
    e = manual(alm, accountant)
    approve(approvals.submit(e, accountant), mp)
    rev = services.reverse_entry(e, user=mp, reason="duplicate")
    assert rev.status == S.POSTED and rev.requires_approval is False


def test_r19_ledger_create_and_post_are_audited(alm, mp, accountant):
    e = manual(alm, accountant)
    approve(approvals.submit(e, accountant), mp)
    actions = set(AuditLog.objects.filter(object_type="ledger.journalentry", object_id=str(e.pk)).values_list("action", flat=True))
    assert {"entry.create", "entry.post"} <= actions
    post_row = AuditLog.objects.get(object_type="ledger.journalentry", object_id=str(e.pk), action="entry.post")
    assert post_row.before == {"status": "approved"} and post_row.after["status"] == "posted" and post_row.actor == mp
