"""Plugs manual journal entries into the approvals engine (spec §3.3, D9, D11).

draft --submit--> pending_approval --final approve--> approved --post_entry (same transaction)--> posted
                                   --reject--> rejected (locked, never posted)
                                   --return / cancel / expire--> draft

On final approval the entry is posted through ledger.services.post_entry, so every ledger rule
(balance, period, Sharia, entity, intercompany) still applies. If posting fails, the approvals
engine rolls the whole approval back (D11).
"""
from decimal import Decimal

from access.errors import RuleError
from approvals.registry import ApprovalAdapter

from . import services
from .models import JournalEntry

CODE = "ledger.journal_entry"
S = JournalEntry.Status


def _move(entry, status):
    """The only place entry approval status changes (the model refuses it without this flag)."""
    entry.status = status
    entry._workflow = True
    try:
        entry.save(update_fields=["status"])
    finally:
        entry._workflow = False


class JournalEntryApproval(ApprovalAdapter):
    model = JournalEntry
    submit_permission = "ledger.entry.create"
    fingerprint_exclude = {"status", "number", "period", "posted_by", "posted_at", "created_at"}

    def is_submittable(self, entry):
        return entry.status == S.DRAFT and entry.requires_approval

    def approval_amount(self, entry):
        total = sum((line.debit for line in entry.lines.all()), Decimal("0"))
        return total, entry.entity.base_currency

    def approval_summary(self, entry):
        ref = f" ({entry.reference})" if entry.reference else ""
        return (
            f"Manual journal entry {entry.date}: {entry.description}{ref}",
            f"قيد يومية يدوي {entry.date}: {entry.description}{ref}",
        )

    def fingerprint_data(self, entry):
        data = super().fingerprint_data(entry)
        data["lines"] = [
            [l.account_id, str(l.debit), str(l.credit), l.counterparty_entity_id, l.description]
            for l in entry.lines.order_by("id")
        ]
        return data

    def snapshot(self, entry):
        return {"status": entry.status, "number": entry.number}

    def on_submitted(self, entry, request):
        problems = [p for p in services.validate_entry(entry) if "balance" in p or "two lines" in p or "zero" in p]
        if problems:
            raise RuleError("entry_invalid", "The entry cannot be submitted: " + " ".join(problems),
                            "لا يمكن تقديم القيد: " + " ".join(problems), detail=problems)
        _move(entry, S.PENDING_APPROVAL)

    def on_approved(self, entry, request, actor):
        _move(entry, S.APPROVED)
        services.post_entry(entry, user=actor)

    def on_rejected(self, entry, request):
        _move(entry, S.REJECTED)

    def on_returned(self, entry, request):
        _move(entry, S.DRAFT)
