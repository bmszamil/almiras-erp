"""The only supported way to post, reverse and report on the ledger."""
from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from core import access
from core.access import catalogue
from core.models import Entity

from .models import Account, EntrySequence, FiscalPeriod, JournalEntry, JournalLine, LedgerError

ZERO = Decimal("0")
CLEARANCE_PERMISSION = "invoicing.post_on_clearance"


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


def _authorise_post(entity, grant, posted_by, document=None):
    """R23: a Grant for a P permission in the entry's own entity, checked again now.

    A ``ledger.post_entry`` grant must have been issued for this very draft and
    never covers the entry's creator (A permission, R14; the ledger has no
    self-approval switch). Only ``invoicing.post_on_clearance`` may record a
    different ``posted_by`` (the approver on the document, zatca D-8).
    """
    if getattr(grant, "permission", None) == "ledger.post_entry":
        if document is None:  # create_entry(post=True): there is no draft yet to approve
            access.deny(grant.user, entity, grant.permission, "grant_needs_draft")
        user = access.verify_grant(grant, entity, catalogue.POST_PERMISSIONS, document=document)
    else:
        user = access.verify_grant(grant, entity, catalogue.POST_PERMISSIONS)
    if grant.permission == CLEARANCE_PERMISSION:
        if posted_by is None:
            raise LedgerError("Posting on clearance needs the approver recorded on the document (posted_by).")
        return posted_by
    if posted_by is not None and posted_by != user:
        raise LedgerError("posted_by is set from the grant; only posting on clearance records another approver.")
    return user


@transaction.atomic
def _post(entry, posted_by):
    entry = JournalEntry.objects.select_for_update().get(pk=entry.pk)
    if entry.status != JournalEntry.Status.DRAFT:
        raise LedgerError("Only draft entries can be posted.")
    problems = validate_entry(entry)
    if problems:
        raise LedgerError(problems)
    entry.period = _find_open_period(entry.entity, entry.date)
    entry.number = _next_number(entry.entity, entry.date.year)
    entry.status = JournalEntry.Status.POSTED
    entry.posted_by = posted_by
    entry.posted_at = timezone.now()
    entry.save()  # stored status is still draft, so the lock allows this one transition
    return entry


def post_entry(entry, grant=None, *, posted_by=None):
    """Post a draft. ``grant`` comes from ``core.access.require`` for the entry's entity (R23).

    The grant is checked before any write, so a refusal changes nothing.
    Sharia, period, balance and entity rules apply whatever the grant (R7).
    """
    entry = JournalEntry.objects.select_related("entity").get(pk=entry.pk)
    user = _authorise_post(entry.entity, grant, posted_by, document=entry)
    return _post(entry, user)


def reverse_entry(entry, grant=None, date=None, reason=""):
    """Reverse a posted entry with a ``ledger.reverse_entry`` or ``invoicing.reverse_document`` grant (R23)."""
    entry = JournalEntry.objects.select_related("entity").get(pk=entry.pk)
    user = access.verify_grant(grant, entry.entity, catalogue.REVERSE_PERMISSIONS)
    return _reverse(entry, user, date, reason)


@transaction.atomic
def _reverse(entry, user, date, reason):
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
    _post(reversal, user)  # authorised by the reversal grant checked in reverse_entry
    entry.status = JournalEntry.Status.REVERSED
    entry.save(update_fields=["status"])
    reversal.refresh_from_db()
    return reversal


def create_entry(entity, date, description, lines, user=None, reference="", post=False, grant=None, posted_by=None):
    """lines: iterable of dicts with account (code or Account), debit, credit, description, counterparty.

    Without ``post``, ``user`` needs ``ledger.create_entry`` in ``entity``.
    With ``post=True`` a P grant for ``entity`` is required and checked before
    anything is written (R23). A ``ledger.post_entry`` grant cannot be used
    here: manual journals are drafted first and posted by an approver who is
    not the creator, through ``post_entry``.
    """
    if post:
        posted_by = _authorise_post(entity, grant, posted_by)
    else:
        # A draft is still a business record in ``entity``: ledger.create_entry there (access.md 3.6).
        access.require(user, entity, "ledger.create_entry")
    return _create(entity, date, description, lines, user, reference, post, posted_by)


@transaction.atomic
def _create(entity, date, description, lines, user, reference, post, posted_by):
    entry = JournalEntry.objects.create(
        entity=entity, date=date, description=description, reference=reference, created_by=user
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
    if post:
        entry = _post(entry, posted_by)
    return entry


@transaction.atomic
def close_period(period, *, user):
    """Close a fiscal period. ``user`` needs ``ledger.close_period`` in the period's entity (access.md 3.6)."""
    # Check rights before taking any lock, and lock only the period row: a lock
    # on the entity row would block the access-log connection's FK check, so a
    # refusal could not be logged outside this transaction.
    entity = Entity.objects.get(pk=period.entity_id)
    access.require(user, entity, "ledger.close_period")
    period = FiscalPeriod.objects.select_for_update(of=("self",)).get(pk=period.pk)
    # Drafts get their period only when posted, so match them by entity and date.
    drafts = JournalEntry.objects.filter(
        entity=entity, status=JournalEntry.Status.DRAFT,
        date__gte=period.start_date, date__lte=period.end_date,
    )
    if drafts.exists():
        raise LedgerError("Draft entries exist in this period.")
    period.status = FiscalPeriod.Status.CLOSED
    period.save(update_fields=["status"])
    return period


def trial_balance(entity, as_of, *, user):
    """Trial balance of ``entity`` for ``user``, who needs ``ledger.view`` there (access.md 3.6, AT-3)."""
    access.require(user, entity, "ledger.view")
    return _trial_balance(entity, as_of)


def _trial_balance(entity, as_of):
    """Balances of posted (and reversed) entries up to a date. Reversals net to zero by design.

    No rights check: for internal callers (closing, consolidation) that have
    already checked the user. User-facing code calls :func:`trial_balance`.
    """
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


def _group_tree(group):
    """``group`` and every entity below it through ``parent`` links, whatever the consolidation."""
    ids, frontier = {group.pk}, [group.pk]
    while frontier:
        frontier = list(Entity.objects.filter(parent_id__in=frontier).exclude(pk__in=ids).values_list("pk", flat=True))
        ids.update(frontier)
    return ids


def intercompany_balances(as_of, *, user, group):
    """Intercompany positions in ``group`` (a parent entity) that ``user`` may see.

    ``user`` needs ``ledger.view`` on the group parent (a group report is
    checked against the group parent, access.md R6). Rows are then limited to
    the group entities in which the user holds ``ledger.view`` (R2): a group
    role covers full subsidiaries; associates and minority holdings need their
    own membership (OQ-3).
    """
    if group.consolidation != Entity.Consolidation.PARENT:
        raise LedgerError(f"{group.code} is not a group parent entity.")
    access.require(user, group, "ledger.view")
    visible = access.entities_for(user, "ledger.view").filter(pk__in=_group_tree(group))
    return _intercompany_balances(as_of, visible)


def _intercompany_balances(as_of, entities):
    """Net intercompany position by (entity, counterparty) for ``entities``; pairs should mirror for elimination.

    No rights check: internal callers pass the entities they have checked.
    """
    rows = (
        JournalLine.objects.filter(
            entry__entity__in=entities,
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
