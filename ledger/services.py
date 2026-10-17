"""The only supported way to post, reverse and report on the ledger."""
from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from core.models import Entity

from .models import Account, EntrySequence, FiscalPeriod, JournalEntry, JournalLine, LedgerError

ZERO = Decimal("0")


def _is_service_user(user):
    """AI agents and integrations are drafts-only (D8, R18, D17)."""
    return getattr(user, "user_type", "human") in ("service_agent", "service_integration")


def _is_system_context(user):
    """Review C: system entries come from module code with no user, or the ERP system user only;
    never from a human (who would bypass approval) and never from an AI agent or integration (D17)."""
    if user is None:
        return True
    from access.services import SYSTEM_USERNAME

    return user.username == SYSTEM_USERNAME and getattr(user, "user_type", "") == "service_integration"


def _audit(user, action, entry, before=None, extra=None):
    """R19 / D7: audit ledger actions. Calls without a user are recorded as the system user."""
    from access.services import audit, system_user

    after = {"status": entry.status, "number": entry.number, "source": entry.source,
             "total_debit": str(sum((l.debit for l in entry.lines.all()), ZERO))}
    if extra:
        after.update(extra)
    audit(user or system_user(), action, entry, entity=entry.entity, before=before, after=after)


def _has_perm(user, entity, perm):
    from access.services import has_perm

    return has_perm(user, entity, perm)


def record_editor(entry, user, action="edit"):
    """Remember who created/edited a draft; they cannot approve it (R8, review A)."""
    from .models import JournalEntryEditor

    if user is None or not getattr(user, "pk", None):
        return
    JournalEntryEditor.objects.create(entity_id=entry.entity_id, entry=entry, user=user, action=action)


def _find_open_period(entity, date):
    period = FiscalPeriod.objects.filter(entity=entity, start_date__lte=date, end_date__gte=date).first()
    if period is None:
        raise LedgerError(f"No fiscal period covers {date} for {entity.code}.")
    if period.status != FiscalPeriod.Status.OPEN:
        raise LedgerError(f"Period {period.name} is closed for {entity.code}.")
    return period


def _next_number(entity, year):
    seq, _ = EntrySequence.objects.select_for_update().get_or_create(entity=entity, year=year)
    seq.last_number += 1
    seq.save(update_fields=["last_number"])
    return f"{entity.code}-JE-{year}-{seq.last_number:06d}"


def validate_entry(entry):
    """Return a list of problems; empty means the entry can be posted."""
    problems = []
    lines = list(entry.lines.select_related("account", "account__entity"))
    if len(lines) < 2:
        problems.append("An entry needs at least two lines.")
    debit = sum((l.debit for l in lines), ZERO)
    credit = sum((l.credit for l in lines), ZERO)
    if debit != credit:
        problems.append(f"Entry does not balance: debits {debit} vs credits {credit}.")
    if debit == ZERO:
        problems.append("Entry total is zero.")
    if not entry.entity.is_active:
        problems.append(f"Entity {entry.entity.code} is inactive.")
    for line in lines:
        acc = line.account
        if acc.entity_id != entry.entity_id:
            problems.append(f"Account {acc.code} belongs to another entity.")
        if not acc.is_postable or not acc.is_active:
            problems.append(f"Account {acc.code} is not open for posting.")
        if acc.sharia_prohibited and entry.entity.sharia_enforced:
            problems.append(f"Account {acc.code} ({acc.name_en}) is Sharia-prohibited.")
        if acc.is_intercompany and line.counterparty_entity_id is None:
            problems.append(f"Intercompany account {acc.code} needs a counterparty entity.")
        if line.counterparty_entity_id == entry.entity_id:
            problems.append("Counterparty cannot be the same entity.")
    return problems


@transaction.atomic
def post_entry(entry, user=None):
    """Post an entry. Entries that require approval must be approved first (approvals engine, D11)."""
    if _is_service_user(user) and not _is_system_context(user):
        raise LedgerError("Service users cannot post entries. / لا يمكن لمستخدمي الخدمة ترحيل القيود.")
    entry = JournalEntry.objects.select_for_update().get(pk=entry.pk)
    if entry.requires_approval:
        if entry.status in (JournalEntry.Status.DRAFT, JournalEntry.Status.PENDING_APPROVAL):
            raise LedgerError("This entry needs approval before posting. / يحتاج هذا القيد إلى اعتماد قبل الترحيل.")
        if entry.status != JournalEntry.Status.APPROVED:
            raise LedgerError("Only approved entries can be posted.")
    elif entry.status != JournalEntry.Status.DRAFT:
        raise LedgerError("Only draft entries can be posted.")
    problems = validate_entry(entry)
    if problems:
        raise LedgerError(problems)
    entry.period = _find_open_period(entry.entity, entry.date)
    entry.number = _next_number(entry.entity, entry.date.year)
    entry.status = JournalEntry.Status.POSTED
    entry.posted_by = user
    entry.posted_at = timezone.now()
    before = {"status": JournalEntry.objects.filter(pk=entry.pk).values_list("status", flat=True).first()}
    entry._posting = True  # the one allowed transition into "posted"
    entry.save(update_fields=sorted(JournalEntry.POSTING_FIELDS))
    _audit(user, "entry.post", entry, before=before)
    return entry


@transaction.atomic
def reverse_entry(entry, date=None, user=None, reason="", system=False):
    """Reverse a posted entry (D15): no approval needed, but the user must hold ledger.entry.reverse
    or ledger.entry.post in the entry's entity, and a reason is mandatory. Every reversal is audited.

    system=True is for module services correcting their own system entries (no user check).
    """
    reason = (reason or "").strip()
    if not reason:
        raise LedgerError("A reason is required to reverse an entry. / سبب العكس مطلوب.")
    if system and not _is_system_context(user):
        raise LedgerError("system=True is for module services only (no user). / للعكس الآلي من خدمات الوحدات فقط.")
    if _is_service_user(user) and not _is_system_context(user):
        raise LedgerError("Service users cannot reverse entries. / لا يمكن لمستخدمي الخدمة عكس القيود.")
    entry = JournalEntry.objects.select_for_update().get(pk=entry.pk)
    if not system:
        if user is None or not getattr(user, "is_active", False) or not (
            _has_perm(user, entry.entity, "ledger.entry.reverse") or _has_perm(user, entry.entity, "ledger.entry.post")
        ):
            raise LedgerError(
                f"You need permission to reverse entries in {entry.entity.code}. / تحتاج صلاحية عكس القيود."
            )
    if entry.status != JournalEntry.Status.POSTED:
        raise LedgerError("Only posted entries can be reversed.")
    date = date or entry.date
    reversal = JournalEntry.objects.create(
        entity=entry.entity,
        date=date,
        description=f"Reversal of {entry.number}: {reason}"[:500],
        reference=entry.number,
        reversal_of=entry,
        created_by=user,
        source=getattr(user, "source", "human") if user is not None else "human",
        requires_approval=False,  # D15: reversals need no approval
    )
    for line in entry.lines.all():
        JournalLine.objects.create(
            entry=reversal,
            account=line.account,
            description=line.description,
            debit=line.credit,
            credit=line.debit,
            counterparty_entity=line.counterparty_entity,
        )
    post_entry(reversal, user=user)
    entry.status = JournalEntry.Status.REVERSED
    entry.save(update_fields=["status"])
    _audit(user, "entry.reverse", entry, before={"status": JournalEntry.Status.POSTED},
           extra={"reason": reason, "reversal_id": reversal.pk, "reversal_number": reversal.number,
                  "system": system})
    reversal.refresh_from_db()
    return reversal


@transaction.atomic
def create_entry(entity, date, description, lines, user=None, reference="", post=False, system=False, source=None):
    """lines: iterable of dicts with account (code or Account), debit, credit, description, counterparty.

    Default (safe): a manual draft with requires_approval=True. It is posted only by final approval
    (approvals.services.submit -> approve, D11), never here.
    system=True: only for module services creating entries from already-approved documents (e.g. an
    approved bill). Such entries skip approval and may be posted here with post=True. Service users
    (AI agents, integrations) can never create system entries (D17).
    """
    if system and not _is_system_context(user):
        raise LedgerError("Only module services (no user, or the ERP system user) may create system entries. "
                          "/ قيود النظام لخدمات الوحدات فقط.")
    if post and not system:
        raise LedgerError("Manual entries are posted by approval, not directly. / تُرحّل القيود اليدوية بالاعتماد.")
    if source is None:
        source = getattr(user, "source", "human") if user is not None else "human"
    entry = JournalEntry.objects.create(
        entity=entity, date=date, description=description, reference=reference, created_by=user,
        source=source, requires_approval=not system,
    )
    for spec in lines:
        account = spec["account"]
        if not isinstance(account, Account):
            account = Account.objects.get(entity=entity, code=account)
        counterparty = spec.get("counterparty")
        if counterparty is not None and not isinstance(counterparty, Entity):
            counterparty = Entity.objects.get(code=counterparty)
        JournalLine.objects.create(
            entry=entry,
            account=account,
            debit=Decimal(str(spec.get("debit", 0))),
            credit=Decimal(str(spec.get("credit", 0))),
            description=spec.get("description", ""),
            counterparty_entity=counterparty,
        )
    if user is not None:
        record_editor(entry, user, "create")
    _audit(user, "entry.create", entry)
    if post:
        entry = post_entry(entry, user=user)
    return entry


def _entries_in(period):
    return JournalEntry.objects.filter(entity=period.entity, date__gte=period.start_date, date__lte=period.end_date)


@transaction.atomic
def close_period(period, user=None):
    """Close a period. With a user, needs ledger.period.manage in the entity. Always audited."""
    from access.services import audit, system_user

    if user is not None and not _has_perm(user, period.entity, "ledger.period.manage"):
        raise LedgerError("You need permission to close periods. / تحتاج صلاحية إقفال الفترات.")
    period = FiscalPeriod.objects.select_for_update().get(pk=period.pk)
    if _entries_in(period).filter(status=JournalEntry.Status.DRAFT).exists():
        raise LedgerError("Draft entries exist in this period.")
    if _entries_in(period).filter(
        status__in=[JournalEntry.Status.PENDING_APPROVAL, JournalEntry.Status.APPROVED]
    ).exists():
        raise LedgerError("Entries in this period are still in approval.")
    before = {"status": period.status}
    period.status = FiscalPeriod.Status.CLOSED
    period._via_service = True
    period.save(update_fields=["status"])
    audit(user or system_user(), "period.close", period, before=before, after={"status": period.status})
    return period


@transaction.atomic
def reopen_period(period, user, reason=""):
    """Reopen a closed period. Default deny: needs ledger.period.reopen, which no role holds by default.
    Reason mandatory; audited."""
    from access.services import audit

    reason = (reason or "").strip()
    if not reason:
        raise LedgerError("A reason is required to reopen a period. / سبب إعادة الفتح مطلوب.")
    if user is None or _is_service_user(user) or not _has_perm(user, period.entity, "ledger.period.reopen"):
        raise LedgerError("You cannot reopen closed periods. / لا يمكنك إعادة فتح الفترات المقفلة.")
    period = FiscalPeriod.objects.select_for_update().get(pk=period.pk)
    if period.status != FiscalPeriod.Status.CLOSED:
        raise LedgerError("The period is not closed.")
    period.status = FiscalPeriod.Status.OPEN
    period._via_service = True
    period.save(update_fields=["status"])
    audit(user, "period.reopen", period, before={"status": "closed"}, after={"status": "open", "reason": reason})
    return period


def trial_balance(entity, as_of):
    """Balances of posted (and reversed) entries up to a date. Reversals net to zero by design."""
    rows = (
        JournalLine.objects.filter(
            entry__entity=entity,
            entry__date__lte=as_of,
            entry__status__in=[JournalEntry.Status.POSTED, JournalEntry.Status.REVERSED],
        )
        .values("account__code", "account__name_en", "account__name_ar", "account__type")
        .annotate(debit=Sum("debit"), credit=Sum("credit"))
        .order_by("account__code")
    )
    result = []
    for r in rows:
        net = (r["debit"] or ZERO) - (r["credit"] or ZERO)
        if net == ZERO:
            continue
        result.append(
            {
                "code": r["account__code"],
                "name_en": r["account__name_en"],
                "name_ar": r["account__name_ar"],
                "type": r["account__type"],
                "debit": net if net > 0 else ZERO,
                "credit": -net if net < 0 else ZERO,
            }
        )
    return result


def intercompany_balances(as_of):
    """Net intercompany position by (entity, counterparty); pairs should mirror for elimination."""
    rows = (
        JournalLine.objects.filter(
            account__is_intercompany=True,
            entry__date__lte=as_of,
            entry__status__in=[JournalEntry.Status.POSTED, JournalEntry.Status.REVERSED],
        )
        .values("entry__entity__code", "counterparty_entity__code")
        .annotate(debit=Sum("debit"), credit=Sum("credit"))
    )
    out = defaultdict(lambda: ZERO)
    for r in rows:
        out[(r["entry__entity__code"], r["counterparty_entity__code"])] += (r["debit"] or ZERO) - (r["credit"] or ZERO)
    return dict(out)
