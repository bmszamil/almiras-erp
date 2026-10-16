"""The only supported way to post, reverse and report on the ledger."""
from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from core.models import Entity

from .models import Account, EntrySequence, FiscalPeriod, JournalEntry, JournalLine, LedgerError

ZERO = Decimal("0")


def _is_service_agent(user):
    return getattr(user, "user_type", None) == "service_agent"


def _audit(user, action, entry, before=None):
    """R19 / D7: audit ledger actions made by a user (system calls without a user are not audited here)."""
    if user is None:
        return
    from access.services import audit

    after = {"status": entry.status, "number": entry.number, "source": entry.source,
             "total_debit": str(sum((l.debit for l in entry.lines.all()), ZERO))}
    audit(user, action, entry, entity=entry.entity, before=before, after=after)


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
    if _is_service_agent(user):
        raise LedgerError("AI agents cannot post entries. / لا يمكن لوكلاء الذكاء الاصطناعي ترحيل القيود.")
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
def reverse_entry(entry, date=None, user=None, reason=""):
    entry = JournalEntry.objects.select_for_update().get(pk=entry.pk)
    if entry.status != JournalEntry.Status.POSTED:
        raise LedgerError("Only posted entries can be reversed.")
    date = date or entry.date
    reversal = JournalEntry.objects.create(
        entity=entry.entity,
        date=date,
        description=f"Reversal of {entry.number}" + (f": {reason}" if reason else ""),
        reference=entry.number,
        reversal_of=entry,
        created_by=user,
        source=getattr(user, "source", "human") if user is not None else "human",
        requires_approval=False,  # correction of an already approved/posted entry (see Build notes)
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
    _audit(user, "entry.reverse", entry, before={"status": JournalEntry.Status.POSTED})
    reversal.refresh_from_db()
    return reversal


@transaction.atomic
def create_entry(entity, date, description, lines, user=None, reference="", post=False, manual=False, source=None):
    """lines: iterable of dicts with account (code or Account), debit, credit, description, counterparty.

    manual=False: system entry made by a module service (e.g. from an approved bill); may post directly.
    manual=True: a person's or agent's manual entry; it is a draft that must go through approvals
    (approvals.services.submit) and cannot be posted here.
    """
    if _is_service_agent(user):
        manual = True  # agents create drafts only (D8, R18)
    if manual and post:
        raise LedgerError("Manual entries are posted by approval, not directly. / تُرحّل القيود اليدوية بالاعتماد.")
    if source is None:
        source = getattr(user, "source", "human") if user is not None else "human"
    entry = JournalEntry.objects.create(
        entity=entity, date=date, description=description, reference=reference, created_by=user,
        source=source, requires_approval=manual,
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
    _audit(user, "entry.create", entry)
    if post:
        entry = post_entry(entry, user=user)
    return entry


def close_period(period):
    if JournalEntry.objects.filter(period=period, status=JournalEntry.Status.DRAFT).exists():
        raise LedgerError("Draft entries exist in this period.")
    in_approval = JournalEntry.objects.filter(
        entity=period.entity, date__gte=period.start_date, date__lte=period.end_date,
        status__in=[JournalEntry.Status.PENDING_APPROVAL, JournalEntry.Status.APPROVED],
    )
    if in_approval.exists():
        raise LedgerError("Entries in this period are still in approval.")
    period.status = FiscalPeriod.Status.CLOSED
    period.save(update_fields=["status"])
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
