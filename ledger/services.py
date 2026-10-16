"""The only supported way to post, reverse and report on the ledger.

Multi-currency (docs/specs/ledger-fx.md): debit/credit are functional-currency
amounts; each line also carries its transaction currency, amount and rate.
"""
import csv
import io
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date as date_type
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection, models, transaction
from django.db.models import Case, F, Q, Sum, When
from django.utils import timezone

from core import access
from core.access import catalogue
from core.access.models import AccessLog, Kind
from core.access import services as access_services
from core.access.services import kind_of
from core.access.services import log as access_log
from core.models import Currency, Entity

from . import documents, fx
from .fx import ONE, error
from .models import (
    Account,
    EntrySequence,
    ExchangeRate,
    FiscalPeriod,
    JournalEntry,
    JournalLine,
    LedgerError,
    RevaluationRun,
)

ZERO = Decimal("0")
CLEARANCE_PERMISSION = "invoicing.post_on_clearance"
APPROVE_SALES_PERMISSION = "invoicing.approve_sales_document"
BOOKED = (JournalEntry.Status.POSTED, JournalEntry.Status.REVERSED)
Kind_ = JournalLine.LineKind
Src = JournalLine.RateSource

# FX accounts from the chart template (ledger-fx.md 5.0, PROPOSED, OQ-6).
FX_GAIN_REALISED = "4700"
FX_GAIN_UNREALISED = "4710"
FX_LOSS_REALISED = "5750"
FX_LOSS_UNREALISED = "5760"
FX_ROUNDING = "5770"
ACTUAL_BANK_RATE = "Actual bank rate / سعر البنك الفعلي"


def _find_open_period(entity, date, *, lock=False):
    """The open period of ``entity`` covering ``date``, else LedgerError.

    With ``lock`` (posting) the period row is locked FOR UPDATE until the
    posting commits, the same row close_period locks, so a period cannot be
    closed between this check and the posting (rule 4). Lock order in both
    paths: entry (posting only), then period.
    """
    qs = FiscalPeriod.objects.filter(entity=entity, start_date__lte=date, end_date__gte=date)
    if lock:
        qs = qs.select_for_update(of=("self",))
    period = qs.first()
    if period is None:
        raise LedgerError(f"No fiscal period covers {date} for {entity.code}. / لا توجد فترة مالية تغطي {date} للكيان {entity.code}.")
    if period.status != FiscalPeriod.Status.OPEN:
        raise LedgerError(f"Period {period.name} is closed for {entity.code}. / الفترة {period.name} مقفلة للكيان {entity.code}.")
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
    problems += _currency_problems(entry, lines)
    return problems


def _is_revaluation_entry(entry):
    """R10: entries created by revalue_period, or the automatic reversal of one."""
    ids = [entry.pk] + ([entry.reversal_of_id] if entry.reversal_of_id else [])
    return RevaluationRun.objects.filter(entry_id__in=ids).exists()


def _currency_problems(entry, lines):
    """R6 to R10, R15, R16, R18, R27, R40: checks that need the entry's functional currency."""
    problems = []
    entity = entry.entity
    functional = entity.base_currency_id
    is_reversal = entry.reversal_of_id is not None
    revaluation_origin = None
    consumed = defaultdict(lambda: [ZERO, ZERO])  # settlements of one item earlier in this entry
    for line in lines:
        cur = Currency.objects.get(code=line.currency_id)
        acc = line.account
        amount = line.debit + line.credit
        where = f"Line {acc.code} ({cur.code})"
        if not cur.is_active and not is_reversal:  # a reversal copies the original currency (F11)
            problems.append(f"{where}: currency {cur.code} is inactive. / العملة {cur.code} غير نشطة.")
        if cur.minor_units > fx.MAX_MINOR_UNITS:
            problems.append(f"{where}: currency {cur.code} cannot be used (more than 3 minor units). / لا يمكن استخدام العملة {cur.code}.")
        elif not fx.has_places(line.txn_amount, cur.minor_units):
            problems.append(
                f"{where}: {cur.code} allows {cur.minor_units} decimals; {line.txn_amount} has more. / "
                f"العملة {cur.code} تقبل {cur.minor_units} خانات عشرية؛ المبلغ {line.txn_amount} يتجاوز ذلك."
            )
        if acc.currency_id and acc.currency_id != cur.code:
            problems.append(f"{where}: account {acc.code} only takes {acc.currency_id}. / الحساب {acc.code} يقبل {acc.currency_id} فقط.")
        if line.exchange_rate_id and line.exchange_rate.entity_id not in (None, entity.pk):
            problems.append(f"{where}: the rate row belongs to another entity. / سجل السعر يخص كيانًا آخر.")
        if line.line_kind == Kind_.REVALUATION:
            if revaluation_origin is None:
                revaluation_origin = _is_revaluation_entry(entry)
            if not revaluation_origin:
                problems.append(
                    f"{where}: revaluation lines are created only by revalue_period. / "
                    "بنود إعادة التقييم تُنشأ فقط من خلال إعادة تقييم الفترة."
                )
            if cur.code == functional:
                problems.append(f"{where}: a revaluation line is never in the functional currency. / بند إعادة التقييم لا يكون بالعملة الوظيفية.")
            continue
        if line.txn_amount == ZERO:
            problems.append(f"{where}: transaction amount is zero. / مبلغ المعاملة صفر.")
        if line.line_kind in (Kind_.REALISED_FX, Kind_.ROUNDING) and cur.code != functional:
            problems.append(f"{where}: FX and rounding lines are in the functional currency. / بنود فروقات العملة والتقريب بالعملة الوظيفية.")
        if line.fx_rate is None:
            problems.append(f"{where}: no exchange rate. / لا يوجد سعر صرف.")
            continue
        if cur.code == functional:
            if line.fx_rate != ONE or line.txn_amount != amount:  # R6
                problems.append(
                    f"{where}: a functional-currency line has rate 1 and transaction amount = debit + credit. / "
                    "بند العملة الوظيفية سعره 1 ومبلغ معاملته يساوي المدين + الدائن."
                )
        elif line.rate_source == Src.FUNCTIONAL:
            problems.append(f"{where}: a foreign-currency line needs a rate source. / بند العملة الأجنبية يحتاج إلى مصدر سعر.")
        if line.rate_source == Src.MANUAL and not line.manual_rate_reason.strip():  # R16
            problems.append(f"{where}: a manual rate needs a reason. / السعر اليدوي يحتاج إلى سبب.")
        if line.rate_source == Src.TABLE:
            if line.exchange_rate_id is None:
                problems.append(f"{where}: a table rate needs its rate row. / سعر الجدول يحتاج إلى سجل السعر.")
            elif not is_reversal:  # R15: a reversal copies the original rate
                current = _lookup_rate(entity, cur.code, functional, entry.date)
                if current is None or current.row.pk != line.exchange_rate_id or current.rate != line.fx_rate:
                    problems.append(
                        f"{where}: rate does not match entry date {entry.date}; re-rate the draft. / "
                        f"السعر لا يطابق تاريخ القيد {entry.date}؛ أعد تسعير المسودة."
                    )
        if line.line_kind == Kind_.SETTLEMENT or line.rate_source == Src.CARRYING or line.settles_line_id:
            problems += _settlement_problems(entry, line, where, is_reversal, consumed)
        elif cur.code != functional and amount != fx.convert(line.txn_amount, line.fx_rate):  # R7
            problems.append(
                f"{where}: functional amount {amount} should be {fx.convert(line.txn_amount, line.fx_rate)} "
                f"({line.txn_amount} x {line.fx_rate}). / المبلغ بالعملة الوظيفية {amount} يجب أن يكون "
                f"{fx.convert(line.txn_amount, line.fx_rate)}."
            )
    return problems


def _open_item(item, exclude_entry_id=None):
    """Open (transaction, functional) amounts of ``item``, oriented to its own side (R18).

    The item plus every booked line that settles it (and the reversals of those).
    """
    sign = 1 if item.debit > 0 else -1
    txn = item.txn_amount
    func = item.debit + item.credit
    settlements = JournalLine.objects.filter(settles_line=item, entry__status__in=BOOKED)
    if exclude_entry_id:
        settlements = settlements.exclude(entry_id=exclude_entry_id)
    for s in settlements:
        side = 1 if s.debit > 0 else -1
        txn += sign * side * s.txn_amount
        func += sign * (s.debit - s.credit)
    return txn, func


def _settlement_problems(entry, line, where, is_reversal, consumed):
    """R7 / R18: a settlement clears its open item at the carrying rate, never more than is open."""
    problems = []
    item = line.settles_line
    if item is None:
        return [f"{where}: a settlement needs the line it settles. / التسوية تحتاج إلى البند الذي تسويه."]
    if (
        item.entry.entity_id != entry.entity_id
        or item.account_id != line.account_id
        or item.currency_id != line.currency_id
        or item.line_kind != Kind_.NORMAL
    ):
        problems.append(
            f"{where}: a settlement clears a normal line of the same entity, account and currency. / "
            "التسوية تكون لبند عادي من نفس الكيان والحساب والعملة."
        )
    if line.line_kind != Kind_.SETTLEMENT or line.rate_source != Src.CARRYING or line.fx_rate != item.fx_rate:
        problems.append(f"{where}: a settlement uses the carrying rate of the item it settles. / التسوية تستخدم السعر الدفتري للبند.")
    if is_reversal or problems:
        return problems
    if item.entry.status != JournalEntry.Status.POSTED:
        return problems + [f"{where}: the item being settled is not posted. / البند المراد تسويته غير مرحّل."]
    if (line.debit > 0) == (item.debit > 0):
        return problems + [f"{where}: a settlement is on the other side of its item. / التسوية في الجانب المقابل للبند."]
    open_txn, open_func = _open_item(item, exclude_entry_id=entry.pk)
    open_txn -= consumed[item.pk][0]
    open_func -= consumed[item.pk][1]
    amount = line.debit + line.credit
    if line.txn_amount > open_txn:
        return problems + [
            f"{where}: settles {line.txn_amount} but only {open_txn} is open. / يسوي {line.txn_amount} والمفتوح {open_txn} فقط."
        ]
    if line.txn_amount == open_txn:  # the final settlement clears exactly what is left: no residue (R18)
        if amount != open_func:
            problems.append(
                f"{where}: this settlement clears the item, so its amount must be the remaining balance {open_func}, "
                f"not {amount}. / هذه التسوية تُقفل البند، فيجب أن يكون مبلغها الرصيد المتبقي {open_func} وليس {amount}."
            )
    elif amount != fx.convert(line.txn_amount, line.fx_rate):
        problems.append(
            f"{where}: settlement amount {amount} should be {fx.convert(line.txn_amount, line.fx_rate)} at the carrying "
            f"rate. / مبلغ التسوية {amount} يجب أن يكون {fx.convert(line.txn_amount, line.fx_rate)} بالسعر الدفتري."
        )
    consumed[item.pk][0] += line.txn_amount
    consumed[item.pk][1] += amount
    return problems


def _deny_grant(grant, entity, code):
    access.deny(getattr(grant, "user", None) if getattr(getattr(grant, "user", None), "pk", None) else None,
                entity, getattr(grant, "permission", ""), code)


def _authorise_draft_post(entry, grant, posted_by):
    """R23 / R30: posting an existing draft needs a ``ledger.post_entry`` grant issued for that very draft.

    The grant's user may not be the draft's creator, submitter or last
    modifier (A permission, R14; the ledger has no self-approval switch).
    Sub-ledger grants never post an existing draft: they post only the entry
    created for their document (create_entry(post=True, document=...)).
    The grant is checked here and used inside the posting transaction.
    """
    if not isinstance(grant, access.Grant):
        _deny_grant(grant, entry.entity, "grant_missing")
    user = access.verify_grant(grant, entry.entity, catalogue.DRAFT_POST_PERMISSIONS, document=entry, use=False)
    if entry.modified_at is not None and entry.modified_at > grant.checked_at:  # approved before the last change
        access.deny(user, entry.entity, grant.permission, "grant_document_changed")
    if posted_by is not None and posted_by != user:
        raise LedgerError("posted_by is set from the grant; only posting on clearance records another approver.")
    return user


def _resolve_document(entity, grant, document):
    """(DocumentType, stored document) for a sub-ledger posting, else AccessDenied (R23, R30).

    The document must be a saved model instance of a type registered in
    ledger.documents, exist in the database and belong to ``entity``. The
    stored row is returned, so segregation is checked on what the database
    holds, not on attributes of the object the caller passed.
    """
    if not isinstance(document, models.Model) or document.pk is None:
        _deny_grant(grant, entity, "grant_needs_document")
    doc_type = documents.get(document._meta.label_lower)
    if doc_type is None:
        _deny_grant(grant, entity, "document_not_registered")
    stored = doc_type.model._default_manager.filter(pk=document.pk).first()
    if stored is None:
        _deny_grant(grant, entity, "document_not_found")
    if doc_type.entity_id_of(stored) != entity.pk:
        _deny_grant(grant, entity, "document_other_entity")
    return doc_type, stored


def _authorise_document_post(entity, grant, posted_by, document, created_by):
    """R23 / R30: create_entry(post=True) needs a sub-ledger P grant issued for ``document``.

    ``document`` is a registered, stored document of ``entity`` (see
    :func:`_resolve_document`) whose type lists the grant's permission. Its
    creator, submitter and last modifier cannot post it (access R14). A
    document books once: a second (non-reversal) entry for it is refused.
    Only ``invoicing.post_on_clearance`` records a different ``posted_by``:
    the approver whose used approval for the document is recorded in the
    access log (zatca D-8; see :func:`_clearance_approver`).
    Returns (posted_by, stored document).
    """
    if not isinstance(grant, access.Grant):
        _deny_grant(grant, entity, "grant_missing")
    if grant.permission in catalogue.DRAFT_POST_PERMISSIONS:  # a manual journal: draft first, then post_entry
        _deny_grant(grant, entity, "grant_needs_draft")
    if grant.permission in catalogue.REVERSE_PERMISSIONS:  # reversal grants only reverse
        _deny_grant(grant, entity, "grant_wrong_permission")
    doc_type, stored = _resolve_document(entity, grant, document)
    user = access.verify_grant(grant, entity, doc_type.post_permissions, document=stored, use=False)
    if user.pk in access_services._document_people(stored):
        access.deny(user, entity, grant.permission, "self_approval")
    if _document_booked(entity, stored):
        raise error(
            f"{doc_type.label} {stored.pk} is already booked; a document posts once (reverse it to correct it).",
            f"المستند {doc_type.label} {stored.pk} مرحّل مسبقًا؛ يُرحَّل المستند مرة واحدة (اعكسه للتصحيح).",
            code="document_booked",
        )
    if grant.permission == CLEARANCE_PERMISSION:
        return _clearance_approver(entity, user, grant, posted_by, stored, doc_type, created_by), stored
    if posted_by is not None and posted_by != user:
        raise error("posted_by is set from the grant; only posting on clearance records another approver.",
                    "يُحدَّد المرحِّل من التفويض؛ الترحيل عند الاعتماد من الهيئة وحده يسجل معتمدًا آخر.")
    return user, stored


def _document_booked(entity, document):
    return JournalEntry.objects.filter(
        entity=entity, document_type=document._meta.label_lower, document_id=document.pk, reversal_of__isnull=True,
    ).exists()


def _clearance_approver(entity, user, grant, posted_by, document, doc_type, created_by):
    """zatca D-8: ``posted_by`` is a human whose approval of ``document`` was used and is still current.

    Counted approvals are ``invoicing.approve_sales_document`` grants issued
    for this document, actually used (GrantUse), and issued after the
    document's last change. The approver need not still be active: the right
    was checked when they approved (owner-facing default).
    """
    if posted_by is None:
        raise error("Posting on clearance needs the approver recorded on the document (posted_by).",
                    "الترحيل عند الاعتماد من الهيئة يحتاج إلى المعتمد المسجل على المستند (posted_by).")
    approvals = access_services.recorded_approvers(
        entity, APPROVE_SALES_PERMISSION, document, used=True, since=doc_type.changed_at_of(document),
    )
    approver_ok = (
        getattr(posted_by, "pk", None) is not None
        and kind_of(posted_by) == Kind.HUMAN
        and posted_by.pk != getattr(created_by, "pk", None)
        and posted_by.pk not in access_services._document_people(document)
        and posted_by.pk in approvals
    )
    if not approver_ok:
        access.deny(user, entity, grant.permission, "posted_by_not_approver")
    return posted_by


def _purification_flags(entry, lines):
    """CLAUDE.md rule 5, invoicing R6: income lines to purify are flagged at posting.

    A credit on an account flagged ``requires_purification`` (4900), or on a
    Sharia-prohibited income account (which can only post in an entity that
    does not enforce Sharia), is non-compliant income. Debits on those
    accounts (the purification transfer E14, reversals) are not flagged.
    """
    return [
        line.pk for line in lines
        if line.credit > ZERO and line.line_kind == Kind_.NORMAL and (
            line.account.requires_purification
            or (line.account.sharia_prohibited and line.account.type == Account.Type.INCOME)
        )
    ]


def _lock_for_posting(entry):
    """Locks taken before an entry is validated and posted (rules 1 and 4, R11, R18).

    Order: the entry (by the caller), its period, its own lines, the lines it
    settles (sorted by pk), then the rate rows its lines use. The period lock
    serialises postings into one period with close_period; the settled-item
    locks serialise two settlements of one item, so they cannot together
    clear more than is open; the rate rows are held FOR SHARE so a rate used
    here cannot be changed before this posting commits.
    """
    period = _find_open_period(entry.entity, entry.date, lock=True)
    lines = list(JournalLine.objects.select_for_update(of=("self",)).filter(entry=entry).order_by("pk"))
    settled = sorted({l.settles_line_id for l in lines if l.settles_line_id})
    if settled:
        list(JournalLine.objects.select_for_update(of=("self",)).filter(pk__in=settled).order_by("pk"))
    rates = sorted({l.exchange_rate_id for l in lines if l.exchange_rate_id})
    if rates:
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SELECT id FROM ledger_exchangerate WHERE id = ANY(%s) ORDER BY id FOR SHARE", [rates])
        else:
            list(ExchangeRate.objects.select_for_update().filter(pk__in=rates).order_by("pk"))
    return period


@transaction.atomic
def _post(entry, posted_by):
    entry = JournalEntry.objects.select_for_update(of=("self",)).select_related("entity").get(pk=entry.pk)
    if entry.status != JournalEntry.Status.DRAFT:
        raise error("Only draft entries can be posted.", "يمكن ترحيل المسودات فقط.")
    period = _lock_for_posting(entry)
    problems = validate_entry(entry)
    if problems:
        raise LedgerError(problems)
    entry.period = period
    flagged = _purification_flags(entry, list(entry.lines.select_related("account")))
    if flagged:  # still a draft here, so the line lock allows it
        JournalLine.objects.filter(pk__in=flagged).update(requires_purification=True)
    entry.number = _next_number(entry.entity, entry.date.year)
    entry.status = JournalEntry.Status.POSTED
    entry.posted_by = posted_by
    entry.posted_at = timezone.now()
    entry._allow_transition(JournalEntry.Status.DRAFT, JournalEntry.Status.POSTED)
    entry.save()  # stored status is still draft, so the lock allows this one transition
    return entry


def post_entry(entry, grant=None, *, posted_by=None):
    """Post a draft with ``require(approver, entity, "ledger.post_entry", document=entry)`` (R23, R30).

    The grant is checked before any write, so a refusal changes nothing; it is
    used (once) in the posting transaction, so a posting refused by a ledger
    rule leaves it unused. Sharia, period, balance and entity rules apply
    whatever the grant (R7).
    """
    entry = JournalEntry.objects.select_related("entity").get(pk=entry.pk)
    user = _authorise_draft_post(entry, grant, posted_by)
    with transaction.atomic():
        access.use_grant(grant, target=entry)
        return _post(entry, user)


def reverse_entry(entry, grant=None, date=None, reason=""):
    """Reverse a posted entry (R23, R30).

    ``grant`` is ``require(user, entity, "ledger.reverse_entry", document=entry)``,
    or a reversal permission registered for the entry's document type (e.g.
    ``invoicing.reverse_document``), issued for that document.
    """
    entry = JournalEntry.objects.select_related("entity").get(pk=entry.pk)
    if not isinstance(grant, access.Grant):
        _deny_grant(grant, entry.entity, "grant_missing")
    if grant.permission == "ledger.reverse_entry":
        document = entry
    elif grant.permission in catalogue.REVERSE_PERMISSIONS:
        if entry.document_ref is None:
            _deny_grant(grant, entry.entity, "grant_wrong_document")
        doc_type = documents.get(entry.document_type)
        if doc_type is None:
            _deny_grant(grant, entry.entity, "document_not_registered")
        if grant.permission not in doc_type.reverse_permissions:
            _deny_grant(grant, entry.entity, "grant_wrong_permission")
        document = doc_type.model._default_manager.filter(pk=entry.document_id).first()
        if document is None:
            _deny_grant(grant, entry.entity, "document_not_found")
    else:
        _deny_grant(grant, entry.entity, "grant_wrong_permission")
    user = access.verify_grant(grant, entry.entity, catalogue.REVERSE_PERMISSIONS, document=document, use=False)
    with transaction.atomic():
        access.use_grant(grant, target=entry)
        return _reverse(entry, user, date, reason)


@transaction.atomic
def _reverse(entry, user, date, reason):
    entry = JournalEntry.objects.select_for_update(of=("self",)).get(pk=entry.pk)
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
        source=_source_for(user),
        document_type=entry.document_type,  # derived from the same document (R30)
        document_id=entry.document_id,
    )
    for line in entry.lines.all():
        JournalLine.objects.create(
            entry=reversal,
            account=line.account,
            description=line.description,
            debit=line.credit,
            credit=line.debit,
            counterparty_entity=line.counterparty_entity,
            # F11: the reversal keeps the original currency, amount and rate.
            currency_id=line.currency_id,
            txn_amount=line.txn_amount,
            fx_rate=line.fx_rate,
            rate_source=line.rate_source,
            exchange_rate_id=line.exchange_rate_id,
            settles_line_id=line.settles_line_id,
            manual_rate_reason=line.manual_rate_reason,
            line_kind=line.line_kind,
        )
    _post(reversal, user)  # authorised by the reversal grant checked in reverse_entry
    entry.status = JournalEntry.Status.REVERSED
    entry._allow_transition(JournalEntry.Status.POSTED, JournalEntry.Status.REVERSED)
    entry.save(update_fields=["status"])
    reversal.refresh_from_db()
    return reversal


def _source_for(user, requested=None):
    """JournalEntry.source from the user's kind (gateway rule 4); a person may mark an entry as an import."""
    kind = kind_of(user) if getattr(user, "pk", None) else Kind.HUMAN
    by_kind = {Kind.AGENT: JournalEntry.Source.AGENT, Kind.SYSTEM: JournalEntry.Source.SYSTEM}.get(kind, JournalEntry.Source.HUMAN)
    if requested in (None, by_kind):
        return by_kind
    if requested == JournalEntry.Source.IMPORT and by_kind == JournalEntry.Source.HUMAN:
        return JournalEntry.Source.IMPORT
    raise error(f"Source {requested!r} does not match the user; it is set from the user.",
                f"المصدر {requested!r} لا يطابق المستخدم؛ يُحدَّد من المستخدم.", code="source_mismatch")


def create_entry(entity, date, description, lines, user=None, reference="", post=False, grant=None, posted_by=None,
                 document=None, source=None):
    """lines: iterable of dicts with account (code or Account), debit, credit, description, counterparty.

    Multi-currency keys (ledger-fx.md): ``currency`` (code or Currency, default
    the functional currency), ``txn_debit`` / ``txn_credit`` (transaction
    amount and side), ``rate`` + ``rate_reason`` (manual rate, needs
    ``ledger.use_manual_rate``), ``settles_line`` (clears that line at its
    carrying rate) and ``line_kind="realised_fx"``. ``debit`` / ``credit`` are
    functional amounts; for foreign lines they are computed when omitted (R7).
    A foreign line without a rate on ``date`` raises and nothing is created (R14).
    A rounding line on 5770 is added when the lines balance per currency but
    their rounded functional amounts differ by a few cents (R17).

    Without ``post``, ``user`` needs ``ledger.create_entry`` in ``entity``.

    With ``post=True`` (a sub-ledger document is approved and posts at once,
    access A1) ``grant`` is a sub-ledger P grant issued for ``document``, which
    the entry records (R30). ``document`` is a saved instance of a type
    registered in :mod:`ledger.documents` for the grant's permission, in
    ``entity``; it books once, and its creator, submitter and last modifier
    cannot post it. ``user`` is the grant's user or holds
    ``ledger.create_entry`` in ``entity``. Everything is checked before
    anything is written (R23), and the grant is used once. A
    ``ledger.post_entry`` grant cannot be used here: manual journals are
    drafted first and posted by an approver who is not the creator, through
    ``post_entry``. ``source`` may be ``"import"`` for a person's import;
    otherwise it follows the user's kind.
    """
    entry_source = _source_for(user, source)
    ref = None
    if post:
        posted_by, document = _authorise_document_post(entity, grant, posted_by, document, user)
        if not (getattr(user, "pk", None) is not None and user == grant.user):
            access.require(user, entity, "ledger.create_entry")
        ref = (document._meta.label_lower, document.pk)
    else:
        if grant is not None or posted_by is not None or document is not None:
            raise error("A grant, posted_by or document is only used with post=True.",
                        "يُستخدم التفويض أو المرحِّل أو المستند مع الترحيل المباشر فقط.")
        # A draft is still a business record in ``entity``: ledger.create_entry there (access.md 3.6).
        access.require(user, entity, "ledger.create_entry")
    with transaction.atomic():
        if post:
            access.use_grant(grant, target=document)
        entry = _create(entity, date, description, lines, user, reference, post, posted_by, entry_source, ref)
        if post and grant.permission == CLEARANCE_PERMISSION:  # zatca D-8: who posted for whom, logged
            access_log(
                AccessLog.Event.PERMISSION_GRANTED, actor=grant.user, entity=entity, permission=CLEARANCE_PERMISSION,
                details={"action": "post_on_clearance", "posted_document": list(ref), "entry": entry.pk,
                         "posted_by": posted_by.pk},
            )
        return entry


@transaction.atomic
def _create(entity, date, description, lines, user, reference, post, posted_by, source=JournalEntry.Source.HUMAN,
            document_ref=None):
    built = _build_lines(entity, date, lines, user)
    entry = JournalEntry.objects.create(
        entity=entity, date=date, description=description, reference=reference, created_by=user, source=source,
        document_type=document_ref[0] if document_ref else "", document_id=document_ref[1] if document_ref else None,
    )
    for line in built:
        line.entry = entry
        line.save()
    if kind_of(user) == Kind.AGENT:  # gateway rule: every agent action is logged (access.md R21)
        access_log(
            AccessLog.Event.PERMISSION_GRANTED, actor=user, entity=entity, permission="ledger.create_entry",
            details={"document": ["ledger.journalentry", entry.pk], "action": "create_draft"},
        )
    if post:
        entry = _post(entry, posted_by)
    return entry


def _build_lines(entity, date, lines, user):
    built = [_build_line(entity, date, spec, user) for spec in lines]
    consumed = defaultdict(lambda: [ZERO, ZERO])
    for line in built:
        _price_settlement(line, consumed)
    rounding = _rounding_line(entity, built)
    if rounding is not None:
        built.append(rounding)
    return built


def update_draft(entry, *, user, date=None, description=None, reference=None, lines=None):
    """Change a draft's header and, with ``lines``, replace its lines (same line dicts as create_entry).

    ``user`` needs ``ledger.create_entry`` in the entry's entity and becomes
    the draft's last modifier, so they cannot approve it (access.md R14).
    Posted entries are never changed (rule 1).
    """
    entry = JournalEntry.objects.select_related("entity").get(pk=entry.pk)
    access.require(user, entry.entity, "ledger.create_entry")
    return _update_draft(entry, user, date, description, reference, lines)


@transaction.atomic
def _update_draft(entry, user, date, description, reference, lines):
    entry = JournalEntry.objects.select_for_update(of=("self",)).select_related("entity").get(pk=entry.pk)
    if entry.status != JournalEntry.Status.DRAFT:
        raise LedgerError("Posted entries cannot be edited. Reverse the entry instead. / لا تُعدَّل القيود المرحّلة؛ اعكس القيد.")
    if _is_revaluation_entry(entry):
        raise error("A revaluation draft is changed by discarding and re-running it.", "تُعدَّل مسودة إعادة التقييم بإلغائها وإعادة تشغيلها.")
    for name, value in (("date", date), ("description", description), ("reference", reference)):
        if value is not None:
            setattr(entry, name, value)
    if lines is not None:
        built = _build_lines(entry.entity, entry.date, lines, user)
        entry.lines.all().delete()
        for line in built:
            line.entry = entry
            line.save()
    entry.modified_by = user
    entry.modified_at = timezone.now()
    entry.save()
    return entry


def submit_entry(entry, *, user):
    """Record that ``user`` submits the draft for approval; the submitter cannot approve it (access.md R14)."""
    entry = JournalEntry.objects.select_related("entity").get(pk=entry.pk)
    access.require(user, entry.entity, "ledger.create_entry")
    with transaction.atomic():
        entry = JournalEntry.objects.select_for_update(of=("self",)).get(pk=entry.pk)
        if entry.status != JournalEntry.Status.DRAFT:
            raise LedgerError("Only draft entries can be submitted. / يمكن تقديم المسودات فقط.")
        entry.submitted_by = user
        entry.submitted_at = timezone.now()
        entry.save(update_fields=["submitted_by", "submitted_at"])
    return entry


def _currency(value, *, for_new_line=True):
    """A Currency from a code or instance. New lines and rates may not use inactive or >3-dp currencies (R9, R40)."""
    code = value.code if isinstance(value, Currency) else str(value).strip().upper()
    cur = Currency.objects.filter(code=code).first()
    if cur is None:
        raise error(f"Unknown currency {code}.", f"العملة {code} غير معروفة.", code="unknown_currency")
    if for_new_line and not cur.is_active:
        raise error(f"Currency {code} is inactive.", f"العملة {code} غير نشطة.", code="inactive_currency")
    if cur.minor_units > fx.MAX_MINOR_UNITS:
        raise error(
            f"Currency {code} has {cur.minor_units} minor units; at most 3 can be stored.",
            f"للعملة {code} عدد {cur.minor_units} خانات عشرية؛ الحد الأقصى للتخزين 3.",
            code="too_many_minor_units",
        )
    return cur


def _side_amount(spec, debit_key, credit_key, what):
    """(side, amount) from a pair of keys; side is "debit", "credit" or None when neither is set."""
    debit = spec.get(debit_key)
    credit = spec.get(credit_key)
    debit = fx.to_decimal(debit, what) if debit not in (None, "") else ZERO
    credit = fx.to_decimal(credit, what) if credit not in (None, "") else ZERO
    if debit < ZERO or credit < ZERO:
        raise error(f"Amounts are not negative ({what}); use the other side.", f"المبالغ لا تكون سالبة ({what})؛ استخدم الجانب الآخر.")
    if debit and credit:
        raise error(f"A line is debit or credit, not both ({what}).", f"البند مدين أو دائن وليس كليهما ({what}).")
    if debit:
        return "debit", debit
    if credit:
        return "credit", credit
    return None, ZERO


def _build_line(entity, on, spec, user):
    """An unsaved JournalLine from a line dict (R1, R7, R8, R14, R16, R18)."""
    account = spec["account"]
    if not isinstance(account, Account):
        account = Account.objects.get(entity=entity, code=account)
    elif account.entity_id != entity.pk:  # rule 4: never another entity's account
        raise error(f"Account {account.code} belongs to another entity.", f"الحساب {account.code} يخص كيانًا آخر.")
    counterparty = spec.get("counterparty")
    if counterparty is not None and not isinstance(counterparty, Entity):
        counterparty = Entity.objects.get(code=counterparty)
    functional = entity.base_currency_id
    cur = _currency(spec.get("currency") or functional)
    f_side, f_amount = _side_amount(spec, "debit", "credit", "functional amount")
    t_side, t_amount = _side_amount(spec, "txn_debit", "txn_credit", "transaction amount")
    if f_side and t_side and f_side != t_side:
        raise error("Transaction and functional amounts are on different sides.", "مبلغ المعاملة والمبلغ الوظيفي في جانبين مختلفين.")
    side = f_side or t_side
    if f_side:
        fx.check_functional_decimals(f_amount)
    if t_side:
        fx.check_txn_decimals(t_amount, cur)
    kind = spec.get("line_kind") or Kind_.NORMAL
    if kind not in (Kind_.NORMAL, Kind_.REALISED_FX):
        raise error(
            f"Line kind {kind!r} is set by the ledger itself.", f"نوع البند {kind!r} يحدده دفتر الأستاذ نفسه.",
            code="line_kind_reserved",
        )
    line = JournalLine(
        account=account, description=spec.get("description", ""), counterparty_entity=counterparty,
        currency_id=cur.code, line_kind=kind,
    )
    settles = spec.get("settles_line")
    manual_rate = spec.get("rate")
    if settles is not None:
        item = settles if isinstance(settles, JournalLine) else JournalLine.objects.get(pk=settles)
        if item.entry.entity_id != entity.pk:
            raise error("The line to settle belongs to another entity.", "البند المراد تسويته يخص كيانًا آخر.")
        line.settles_line = item
        line.line_kind = Kind_.SETTLEMENT
        line.rate_source = Src.CARRYING
        line.fx_rate = item.fx_rate
    elif manual_rate not in (None, ""):
        access.require(user, entity, "ledger.use_manual_rate")  # R16; agents never hold it (table rates only)
        rate = fx.to_decimal(manual_rate, "rate")
        fx.check_rate(rate)
        reason = (spec.get("rate_reason") or "").strip()
        if not reason:
            raise error("A manual rate needs a reason.", "السعر اليدوي يحتاج إلى سبب.", code="manual_rate_reason")
        line.rate_source, line.fx_rate, line.manual_rate_reason = Src.MANUAL, rate, reason
    elif cur.code == functional:
        line.rate_source, line.fx_rate = Src.FUNCTIONAL, ONE
    else:
        found = _require_rate(entity, cur.code, functional, on)  # R14: no rate, no entry
        line.rate_source, line.fx_rate, line.exchange_rate = Src.TABLE, found.rate, found.row
    if side is None:
        raise error(f"Line {account.code} has no amount.", f"البند {account.code} بلا مبلغ.")
    if not t_side:
        if cur.code != functional:
            raise error(
                f"Line {account.code} in {cur.code} needs txn_debit or txn_credit.",
                f"البند {account.code} بعملة {cur.code} يحتاج إلى مبلغ المعاملة.",
            )
        t_amount = f_amount
    line.txn_amount = t_amount
    if f_side:
        amount = f_amount
    elif cur.code == functional and line.rate_source == Src.FUNCTIONAL:
        fx.check_functional_decimals(t_amount)
        amount = t_amount
    else:
        amount = None if line.settles_line_id else fx.convert(t_amount, line.fx_rate)
    line._side, line._given = side, amount  # settlement amounts are priced once all lines are known
    if amount is not None:
        _set_side(line, side, amount)
    return line


def _set_side(line, side, amount):
    line.debit, line.credit = (amount, ZERO) if side == "debit" else (ZERO, amount)


def _price_settlement(line, consumed):
    """R18: partial settlements clear round(txn x carrying rate); the final one clears exactly what is left."""
    if line.settles_line_id is None or line._given is not None:
        return
    item = line.settles_line
    open_txn, open_func = _open_item(item)
    open_txn -= consumed[item.pk][0]
    open_func -= consumed[item.pk][1]
    if line.txn_amount == open_txn:
        amount = open_func
    else:
        amount = fx.convert(line.txn_amount, line.fx_rate)
    consumed[item.pk][0] += line.txn_amount
    consumed[item.pk][1] += amount
    _set_side(line, line._side, amount)


def _signed_txn(line):
    return line.txn_amount if line.debit > 0 else -line.txn_amount


def _rounding_line(entity, lines):
    """R17: one 5770 line when lines balance per currency but rounded functional amounts differ slightly."""
    foreign = [l for l in lines if l.currency_id != entity.base_currency_id]
    if not foreign:
        return None
    per_currency = defaultdict(lambda: ZERO)
    for l in lines:
        per_currency[l.currency_id] += _signed_txn(l)
    if any(total != ZERO for total in per_currency.values()):
        return None  # cross-currency entries are balanced by FX lines, never by rounding
    diff = sum((l.debit - l.credit for l in lines), ZERO)
    if diff == ZERO or abs(diff) > fx.CENT * len(foreign):
        return None  # a larger difference is not auto-fixed; post_entry rejects the entry
    account = Account.objects.filter(entity=entity, code=FX_ROUNDING).first()
    if account is None:
        raise error(
            f"Account {FX_ROUNDING} (currency rounding) is missing; install the chart template.",
            f"الحساب {FX_ROUNDING} (فروقات التقريب) غير موجود؛ ثبّت دليل الحسابات.",
        )
    line = JournalLine(
        account=account, description="Currency rounding / فروقات تقريب العملات", currency_id=entity.base_currency_id,
        txn_amount=abs(diff), fx_rate=ONE, rate_source=Src.FUNCTIONAL, line_kind=Kind_.ROUNDING,
    )
    _set_side(line, "credit" if diff > 0 else "debit", abs(diff))
    return line


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


def trial_balance(entity, as_of, *, user, by_currency=False):
    """Trial balance of ``entity`` for ``user``, who needs ``ledger.view`` there (access.md 3.6, AT-3).

    Amounts are functional. With ``by_currency`` each row also gets
    ``by_currency``: {currency code: net transaction amount (debit positive)}.
    """
    access.require(user, entity, "ledger.view")
    rows = _trial_balance(entity, as_of)
    if by_currency:
        per = _txn_balances(entity, as_of)
        for row in rows:
            row["by_currency"] = per.get(row["code"], {})
    return rows


def _signed_txn_expr():
    """Transaction amount signed by side: debit positive, credit negative (revaluation lines are 0)."""
    return Sum(Case(When(debit__gt=0, then=F("txn_amount")), default=-F("txn_amount")))


def _txn_balances(entity, as_of):
    rows = (
        JournalLine.objects.filter(entry__entity=entity, entry__date__lte=as_of, entry__status__in=BOOKED)
        .values("account__code", "currency_id")
        .annotate(txn=_signed_txn_expr())
    )
    out = defaultdict(dict)
    for r in rows:
        if r["txn"]:
            out[r["account__code"]][r["currency_id"]] = r["txn"]
    return out


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


def counterparty_choices(user, entity=None):
    """Entities that may be the counterparty on lines of ``entity`` (rule 6): the other entities of its group.

    Without ``entity`` (a new entry in admin): the groups of the entities ``user`` drafts in.
    Choices only; posting still checks every line (validate_entry).
    """
    bases = [entity] if entity is not None else list(access.entities_for(user, "ledger.create_entry"))
    ids = set()
    for e in bases:
        ids |= _group_tree(access_services.group_parent_of(e))
    if entity is not None:
        ids.discard(entity.pk)
    return Entity.objects.filter(pk__in=ids)


def intercompany_balances(as_of, *, user, group, by_currency=False):
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
    return _intercompany_balances(as_of, visible, by_currency=by_currency)


def _intercompany_balances(as_of, entities, by_currency=False):
    """Net intercompany position by (entity, counterparty) for ``entities``; pairs should mirror for elimination.

    With ``by_currency`` the key is (entity, counterparty, currency) and the value is in
    that transaction currency (R29): functional amounts of entities with different
    functional currencies are not expected to mirror.
    No rights check: internal callers pass the entities they have checked.
    """
    if by_currency:
        rows = (
            JournalLine.objects.filter(
                entry__entity__in=entities, account__is_intercompany=True, entry__date__lte=as_of,
                entry__status__in=BOOKED,
            )
            .values("entry__entity__code", "counterparty_entity__code", "currency_id")
            .annotate(txn=_signed_txn_expr())
        )
        out = defaultdict(lambda: ZERO)
        for r in rows:
            out[(r["entry__entity__code"], r["counterparty_entity__code"], r["currency_id"])] += r["txn"] or ZERO
        return dict(out)
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


# --- exchange rates (ledger-fx.md R11 to R16, R27, R28) ------------------------

@dataclass(frozen=True)
class Rate:
    """A resolved rate: ``to`` units per 1 ``from`` unit, the row it came from, and whether it was inverted."""

    rate: Decimal
    row: ExchangeRate | None
    inverted: bool = False


def _max_rate_age():
    return timedelta(days=getattr(settings, "FX_MAX_RATE_AGE_DAYS", 5))  # OQ-3 default


def _source_rank(source):
    """Between sources on one date: manual first, then the configured order (OQ-2), derived last."""
    order = list(getattr(settings, "FX_SOURCE_PRECEDENCE", ("manual", "file", "sama", "ecb", "cbe", "cbuae", "pboc")))
    return order.index(source) if source in order else len(order)


def _lookup_rate(entity, from_code, to_code, on, rate_type=ExchangeRate.RateType.SPOT):
    """R12: entity override first, then group-wide; latest date within the age limit; direct before inverse.

    Returns a Rate or None. No rights check (internal).
    """
    if from_code == to_code:
        return Rate(ONE, None)
    base = ExchangeRate.objects.filter(
        rate_type=rate_type, date__lte=on, date__gte=on - _max_rate_age(),
        from_currency__is_active=True, to_currency__is_active=True,
    )
    for scope_q in (Q(entity=entity), Q(entity__isnull=True)):
        for f, t, inverted in ((from_code, to_code, False), (to_code, from_code, True)):
            rows = list(base.filter(scope_q, from_currency_id=f, to_currency_id=t))
            if rows:
                best = min(rows, key=lambda r: (-r.date.toordinal(), _source_rank(r.source), -r.pk))
                return Rate(fx.inverse(best.rate) if inverted else best.rate, best, inverted)
    return None


def _require_rate(entity, from_code, to_code, on, rate_type=ExchangeRate.RateType.SPOT):
    found = _lookup_rate(entity, from_code, to_code, on, rate_type)
    if found is None:
        days = _max_rate_age().days
        raise error(
            f"No {rate_type} exchange rate {from_code}->{to_code} for {on} (or up to {days} days before) for {entity.code}.",
            f"لا يوجد سعر صرف {from_code}->{to_code} بتاريخ {on} (أو قبله بما لا يزيد عن {days} أيام) للكيان {entity.code}.",
            code="missing_rate",
        )
    return found


def get_rate(entity, from_currency, to_currency, on, rate_type=ExchangeRate.RateType.SPOT, *, user):
    """R12 lookup for ``user`` (``ledger.view_rates`` in ``entity``). Raises LedgerError when no rate exists."""
    access.require(user, entity, "ledger.view_rates")
    return _require_rate(entity, _currency(from_currency).code, _currency(to_currency).code, on, rate_type)


def _group_parent(group):
    """The group parent entity global rates are checked against (access.md R6)."""
    if group is None:
        parents = list(Entity.objects.filter(consolidation=Entity.Consolidation.PARENT, parent__isnull=True)[:2])
        if len(parents) != 1:
            raise error(
                "Pass the group parent entity (group=...) for a group-wide rate.",
                "حدّد الكيان الأم للمجموعة (group=...) لسعر المجموعة.",
            )
        group = parents[0]
    if group.consolidation != Entity.Consolidation.PARENT:
        raise error(f"{group.code} is not a group parent entity.", f"{group.code} ليس الكيان الأم للمجموعة.")
    return group


def _authorise_rate_write(user, entity, group):
    """R28: entity rates need ledger.maintain_entity_rate there; global rates a group-level right."""
    if entity is not None:
        return access.require(user, entity, "ledger.maintain_entity_rate")
    group = _group_parent(group)
    permission = "ledger.import_global_rate" if kind_of(user) == Kind.SYSTEM else "ledger.maintain_global_rate"
    return access.require(user, group, permission)


def _new_rate(*, user, entity, from_currency, to_currency, on, rate, rate_type, source, source_reference="",
              period_start=None, period_end=None, import_batch=""):
    if rate_type not in ExchangeRate.RateType.values:  # R36: no forward (or other) rate type exists
        raise error(
            f"Rate type {rate_type!r} is not allowed; only spot and average rates exist.",
            f"نوع السعر {rate_type!r} غير مسموح؛ توجد الأسعار الفورية والمتوسطة فقط.",
            code="rate_type",
        )
    if source not in ExchangeRate.Source.values:
        raise error(f"Unknown rate source {source!r}.", f"مصدر السعر {source!r} غير معروف.")
    rate = fx.to_decimal(rate, "rate")
    fx.check_rate(rate)
    row = ExchangeRate(
        entity=entity, date=on, from_currency=_currency(from_currency), to_currency=_currency(to_currency),
        rate=rate, rate_type=rate_type, source=source, source_reference=source_reference,
        period_start=period_start, period_end=period_end, created_by=user, import_batch=import_batch,
    )
    try:
        row.full_clean(exclude=["created_by"])
    except ValidationError as exc:
        raise LedgerError(exc.messages) from None
    row.save()
    return row


@transaction.atomic
def create_rate(*, user, from_currency, to_currency, on, rate, rate_type=ExchangeRate.RateType.SPOT,
                source=ExchangeRate.Source.MANUAL, entity=None, group=None, source_reference="",
                period_start=None, period_end=None):
    """Enter one rate (R11). ``entity`` set: that entity's override; None: a group-wide rate."""
    _authorise_rate_write(user, entity, group)
    return _new_rate(
        user=user, entity=entity, from_currency=from_currency, to_currency=to_currency, on=on, rate=rate,
        rate_type=rate_type, source=source, source_reference=source_reference,
        period_start=period_start, period_end=period_end,
    )


IMPORT_COLUMNS = ("date", "from_currency", "to_currency", "rate")
IMPORT_OPTIONAL = ("rate_type", "period_start", "period_end", "source_reference")


def _iso_date(value, line_no):
    try:
        if len(value) != 10:
            raise ValueError(value)
        return date_type.fromisoformat(value)
    except (TypeError, ValueError):
        raise error(
            f"Row {line_no}: {value!r} is not a date (YYYY-MM-DD).", f"السطر {line_no}: {value!r} ليس تاريخًا (YYYY-MM-DD).",
        ) from None


def import_rates(file, *, user, source=ExchangeRate.Source.FILE, entity=None, group=None):
    """Import rates from a CSV file (path or text file object): date, from_currency, to_currency, rate
    [, rate_type, period_start, period_end, source_reference].

    All or nothing. A row that already exists with the same rate is skipped, so a
    second import of the same file creates nothing; a row that exists with a
    different rate fails the import (stored rates are never overwritten, R11).
    Automatic sources (SAMA, ECB, CBE, CBUAE, PBOC) and derived cross rates (R13)
    are not built: their sources and precedence are open (OQ-2).
    """
    if source != ExchangeRate.Source.FILE:
        raise error(
            f"Import from {source!r} is not enabled; only CSV files can be imported (OQ-2).",
            f"الاستيراد من {source!r} غير مفعّل؛ يمكن استيراد ملفات CSV فقط (OQ-2).",
            code="source_not_enabled",
        )
    _authorise_rate_write(user, entity, group)
    if isinstance(file, (str, Path)):
        with open(file, encoding="utf-8", newline="") as fh:
            text = fh.read()
        reference = Path(file).name
    else:
        text = file.read()
        reference = getattr(file, "name", "") or "upload"
    return _import_rows(text, user=user, entity=entity, reference=str(reference)[:200])


@transaction.atomic
def _import_rows(text, *, user, entity, reference):
    reader = csv.DictReader(io.StringIO(text))
    missing = [c for c in IMPORT_COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise error(f"Missing columns: {', '.join(missing)}.", f"أعمدة ناقصة: {', '.join(missing)}.")
    batch = uuid.uuid4().hex[:16]
    created = unchanged = 0
    seen = set()
    for line_no, row in enumerate(reader, start=2):
        rate_type = (row.get("rate_type") or ExchangeRate.RateType.SPOT).strip()
        on = _iso_date((row["date"] or "").strip(), line_no)
        start = _iso_date(row["period_start"].strip(), line_no) if (row.get("period_start") or "").strip() else None
        end = _iso_date(row["period_end"].strip(), line_no) if (row.get("period_end") or "").strip() else None
        f, t = (row["from_currency"] or "").strip().upper(), (row["to_currency"] or "").strip().upper()
        rate = fx.to_decimal((row["rate"] or "").strip(), "rate")
        key = (on, f, t, rate_type)
        if key in seen:
            raise error(f"Row {line_no}: duplicate rate in the file.", f"السطر {line_no}: سعر مكرر في الملف.")
        seen.add(key)
        existing = ExchangeRate.objects.filter(
            entity=entity, date=on, from_currency_id=f, to_currency_id=t, rate_type=rate_type,
            source=ExchangeRate.Source.FILE,
        ).first()
        if existing is not None:
            if existing.rate != rate:
                raise error(
                    f"Row {line_no}: a {f}->{t} rate for {on} already exists with {existing.rate}; add a manual "
                    "correction instead of re-importing.",
                    f"السطر {line_no}: يوجد سعر {f}->{t} بتاريخ {on} قيمته {existing.rate}؛ أضف تصحيحًا يدويًا.",
                    code="rate_conflict",
                )
            unchanged += 1
            continue
        try:
            _new_rate(
                user=user, entity=entity, from_currency=f, to_currency=t, on=on, rate=rate, rate_type=rate_type,
                source=ExchangeRate.Source.FILE, source_reference=(row.get("source_reference") or reference)[:200],
                period_start=start, period_end=end, import_batch=batch,
            )
        except LedgerError as exc:
            raise LedgerError([f"Row {line_no} / السطر {line_no}: {m}" for m in exc.messages]) from None
        created += 1
    return {"created": created, "unchanged": unchanged, "batch": batch if created else ""}


def rates_visible_to(user):
    """R28: rate rows ``user`` may see: their entities' overrides, plus global rows for any ledger user."""
    entities = access.entities_for(user, "ledger.view_rates")
    q = Q(entity__in=entities)
    if entities.exists():
        q |= Q(entity__isnull=True)
    return ExchangeRate.objects.filter(q)


@transaction.atomic
def rerate_entry(entry, *, user):
    """R15: give a draft's table-rate lines the rate for its current date, and redo its rounding line."""
    entry = JournalEntry.objects.select_for_update(of=("self",)).select_related("entity").get(pk=entry.pk)
    access.require(user, entry.entity, "ledger.create_entry")
    if entry.status != JournalEntry.Status.DRAFT:
        raise error("Only draft entries can be re-rated.", "يمكن إعادة تسعير المسودات فقط.")
    functional = entry.entity.base_currency_id
    lines = list(entry.lines.all())
    for line in lines:
        if line.line_kind == Kind_.ROUNDING:
            line.delete()
            continue
        if line.rate_source == Src.TABLE and line.line_kind != Kind_.REVALUATION:
            found = _require_rate(entry.entity, line.currency_id, functional, entry.date)
            line.fx_rate, line.exchange_rate = found.rate, found.row
            _set_side(line, "debit" if line.debit > 0 else "credit", fx.convert(line.txn_amount, found.rate))
            line.save()
    rounding = _rounding_line(entry.entity, [l for l in lines if l.line_kind != Kind_.ROUNDING])
    if rounding is not None:
        rounding.entry = entry
        rounding.save()
    entry.modified_by, entry.modified_at = user, timezone.now()  # a re-rater cannot approve the draft (R14)
    entry.save(update_fields=["modified_by", "modified_at"])
    return entry


def fx_exceptions(entity, *, user, date_from=None, date_to=None):
    """R16: booked lines with a manual rate, with the table rate of that date and the deviation (OQ-3: no limit)."""
    access.require(user, entity, "ledger.view")
    qs = JournalLine.objects.filter(
        entry__entity=entity, entry__status__in=BOOKED, rate_source=Src.MANUAL, entry__reversal_of__isnull=True,
    ).select_related("entry", "account")
    if date_from:
        qs = qs.filter(entry__date__gte=date_from)
    if date_to:
        qs = qs.filter(entry__date__lte=date_to)
    out = []
    for line in qs.order_by("entry__date", "pk"):
        table = _lookup_rate(entity, line.currency_id, entity.base_currency_id, line.entry.date)
        deviation = None
        if table is not None and table.rate:
            deviation = ((line.fx_rate - table.rate) / table.rate * 100).quantize(Decimal("0.0001"))
        out.append({
            "entry": line.entry.number, "date": line.entry.date, "account": line.account.code,
            "currency": line.currency_id, "rate": line.fx_rate, "table_rate": table.rate if table else None,
            "deviation_pct": deviation, "reason": line.manual_rate_reason,
        })
    return out


# --- settlement and currency exchange (R18, R20, R34, R35) -------------------------

def fx_settlement_lines(entity, on, settles, txn_amount, counter_account, *, user, counter_currency=None,
                        counter_amount=None, counter_rate=None, counter_rate_reason="", description=""):
    """Line dicts for create_entry that settle ``txn_amount`` of the open item ``settles`` (R18).

    The item is cleared at its carrying rate (the final settlement clears exactly
    what is left), the counter line (cash, or another item) is rated on ``on``
    (spot of the settlement day, R34), and the functional difference goes to
    4700 (gain) or 5750 (loss) as a ``realised_fx`` line. ``counter_amount`` is
    needed when the counter currency is neither the item's nor the functional
    currency. Writes nothing; ``user`` needs ``ledger.view`` in ``entity``.
    """
    access.require(user, entity, "ledger.view")
    item = settles if isinstance(settles, JournalLine) else JournalLine.objects.get(pk=settles)
    if item.entry.entity_id != entity.pk:
        raise error("The line to settle belongs to another entity.", "البند المراد تسويته يخص كيانًا آخر.")
    if item.entry.status != JournalEntry.Status.POSTED or item.line_kind != Kind_.NORMAL:
        raise error("Only a posted normal line can be settled.", "لا يُسوّى إلا بند عادي مرحّل.")
    functional = entity.base_currency_id
    item_cur = _currency(item.currency_id, for_new_line=False)
    amount = fx.to_decimal(txn_amount, "transaction amount")
    fx.check_txn_decimals(amount, item_cur)
    open_txn, open_func = _open_item(item)
    if amount <= ZERO or amount > open_txn:
        raise error(f"Settle between 0 and the open {open_txn} {item_cur.code}.", f"المبلغ يجب أن يكون بين 0 والمفتوح {open_txn}.")
    settled_func = open_func if amount == open_txn else fx.convert(amount, item.fx_rate)

    counter_cur = _currency(counter_currency or item_cur.code)
    if counter_amount not in (None, ""):
        c_amount = fx.to_decimal(counter_amount, "counter amount")
    elif counter_cur.code == item_cur.code:
        c_amount = amount
    elif counter_cur.code == functional:
        c_amount = fx.convert(amount, _require_rate(entity, item_cur.code, functional, on).rate)
    else:
        raise error("Give the amount paid in the counter currency.", "حدّد المبلغ المدفوع بعملة الطرف المقابل.")
    fx.check_txn_decimals(c_amount, counter_cur)
    if counter_cur.code == functional:
        counter_func = c_amount
    elif counter_rate not in (None, ""):
        counter_func = fx.convert(c_amount, fx.to_decimal(counter_rate, "rate"))
    else:
        counter_func = fx.convert(c_amount, _require_rate(entity, counter_cur.code, functional, on).rate)

    item_side, other_side = ("debit", "credit") if item.debit > 0 else ("credit", "debit")
    settle = {
        "account": item.account, "currency": item_cur.code, f"txn_{other_side}": amount, "settles_line": item,
        "counterparty": item.counterparty_entity, "description": description,
    }
    counter = {"account": counter_account, "currency": counter_cur.code, f"txn_{item_side}": c_amount, "description": description}
    if counter_rate not in (None, "") and counter_cur.code != functional:
        counter.update(rate=counter_rate, rate_reason=counter_rate_reason)
    lines = [counter, settle]
    net_debit = (counter_func - settled_func) * (1 if item_side == "debit" else -1)
    if net_debit > 0:
        lines.append({"account": FX_GAIN_REALISED, "credit": net_debit, "line_kind": Kind_.REALISED_FX,
                      "description": "Realised FX gain / أرباح فروقات العملة المحققة"})
    elif net_debit < 0:
        lines.append({"account": FX_LOSS_REALISED, "debit": -net_debit, "line_kind": Kind_.REALISED_FX,
                      "description": "Realised FX loss / خسائر فروقات العملة المحققة"})
    return lines


def _account(entity, value):
    return value if isinstance(value, Account) else Account.objects.get(entity=entity, code=value)


def record_currency_exchange(entity, on, from_account, from_amount, to_account, to_amount, *, user,
                             from_currency=None, to_currency=None, to_date=None, description=""):
    """R20 / R35: a spot currency exchange as one draft entry with both legs on ``on``.

    The foreign leg carries the actual bank rate (functional paid / foreign
    received, ``rate_source=manual``), so no FX gain or loss arises. Legs on
    two dates (deferred exchange) are refused (R35, R36).
    """
    access.require(user, entity, "ledger.create_entry")  # before reading any of the entity's data
    if to_date is not None and to_date != on:
        raise error(
            "Both legs of a currency exchange are on the same date (spot only).",
            "طرفا عملية الصرف في التاريخ نفسه (صرف فوري فقط).", code="exchange_not_spot",
        )
    functional = entity.base_currency_id
    from_acc, to_acc = _account(entity, from_account), _account(entity, to_account)
    f_cur = _currency(from_currency or from_acc.currency_id or functional)
    t_cur = _currency(to_currency or to_acc.currency_id or functional)
    if f_cur.code == t_cur.code:
        raise error("An exchange is between two different currencies.", "الصرف يكون بين عملتين مختلفتين.")
    f_amount, t_amount = fx.to_decimal(from_amount, "from amount"), fx.to_decimal(to_amount, "to amount")
    if f_amount <= ZERO or t_amount <= ZERO:
        raise error("Both amounts must be above zero.", "يجب أن يكون المبلغان أكبر من صفر.")
    fx.check_txn_decimals(f_amount, f_cur)
    fx.check_txn_decimals(t_amount, t_cur)
    to_leg = {"account": to_acc, "currency": t_cur.code, "txn_debit": t_amount, "description": description}
    from_leg = {"account": from_acc, "currency": f_cur.code, "txn_credit": f_amount, "description": description}
    if t_cur.code == functional:
        value = t_amount
        from_leg.update(rate=fx.implied_rate(value, f_amount), rate_reason=ACTUAL_BANK_RATE, credit=value)
    else:
        if f_cur.code == functional:
            value = f_amount
        else:  # neither leg functional: the sold leg at the day's table rate
            value = fx.convert(f_amount, _require_rate(entity, f_cur.code, functional, on).rate)
            from_leg["credit"] = value
        to_leg.update(rate=fx.implied_rate(value, t_amount), rate_reason=ACTUAL_BANK_RATE, debit=value)
    for leg in (from_leg, to_leg):
        if "rate" in leg and fx.convert(leg[f"txn_{'credit' if leg is from_leg else 'debit'}"], leg["rate"]) != value:
            raise error(
                "The amounts are too large to express as one rate with 10 decimals; split the exchange.",
                "المبالغ أكبر من أن يُعبَّر عنها بسعر واحد بعشر خانات؛ قسّم العملية.",
            )
    return create_entry(
        entity, on, description or f"Currency exchange {f_cur.code}->{t_cur.code} / صرف عملة {f_cur.code}->{t_cur.code}",
        [to_leg, from_leg], user=user,
    )


# --- period-end revaluation (R19, R21 to R26) -----------------------------------

def revalue_period(entity, period, *, user):
    """Revalue open foreign balances on ``is_monetary`` accounts at the period-end spot rate (R21).

    Run by a human with ``ledger.revalue`` (R26). Creates the revaluation entry
    as a draft dated the period end, linked to a RevaluationRun; an approver
    posts it with :func:`post_revaluation`, which in the same transaction posts
    its automatic reversal on the first day of the next period (R23; access.md
    A1 / OQ-5: the CFO approves, posting follows). A missing closing rate, a
    closed or missing period, or a second run for the period fails the whole
    run and writes nothing (R22, R24, R25).
    """
    access.require(user, entity, "ledger.revalue")
    if kind_of(user) != Kind.HUMAN:  # R26; the catalogue already keeps ledger.revalue from agents and systems
        access.deny(user, entity, "ledger.revalue", "agent_forbidden" if kind_of(user) == Kind.AGENT else "system_forbidden")
    return _revalue(entity, period, user)


@transaction.atomic
def _revalue(entity, period, user):
    period = FiscalPeriod.objects.select_for_update().get(pk=period.pk)
    if period.entity_id != entity.pk:
        raise error("The period belongs to another entity.", "الفترة تخص كيانًا آخر.")
    if RevaluationRun.objects.filter(entity=entity, period=period).exists():
        raise error(
            f"{entity.code} {period.name} is already revalued.", f"تمت إعادة تقييم {period.name} للكيان {entity.code} مسبقًا.",
            code="already_revalued",
        )
    _find_open_period(entity, period.end_date)
    _find_open_period(entity, period.end_date + timedelta(days=1))
    functional = entity.base_currency_id
    balances = (
        JournalLine.objects.filter(
            entry__entity=entity, entry__status__in=BOOKED, entry__date__lte=period.end_date,
            account__is_monetary=True,
        )
        .exclude(currency_id=functional)
        .values("account_id", "currency_id", "counterparty_entity_id")
        .annotate(txn=_signed_txn_expr(), debit=Sum("debit"), credit=Sum("credit"))
        .order_by("account_id", "currency_id", "counterparty_entity_id")
    )
    lines, gains, losses = [], ZERO, ZERO
    for b in balances:
        foreign = b["txn"] or ZERO
        carried = (b["debit"] or ZERO) - (b["credit"] or ZERO)
        row = None
        if foreign != ZERO:
            closing = _require_rate(entity, b["currency_id"], functional, period.end_date)  # R22
            target, row = fx.convert(foreign, closing.rate), closing.row
        else:
            target = ZERO
        diff = target - carried
        if diff == ZERO:
            continue
        line = JournalLine(
            account_id=b["account_id"], currency_id=b["currency_id"], counterparty_entity_id=b["counterparty_entity_id"],
            txn_amount=ZERO, fx_rate=None, rate_source=Src.TABLE, exchange_rate=row, line_kind=Kind_.REVALUATION,
            description=f"FX revaluation {period.name} / إعادة تقييم العملات {period.name}",
        )
        _set_side(line, "debit" if diff > 0 else "credit", abs(diff))
        lines.append(line)
        if diff > 0:
            gains += diff
        else:
            losses -= diff
    run = RevaluationRun(entity=entity, period=period, rate_date=period.end_date, run_by=user)
    if not lines:
        run.save()
        return run
    for code, side, amount in ((FX_GAIN_UNREALISED, "credit", gains), (FX_LOSS_UNREALISED, "debit", losses)):
        if amount:
            line = JournalLine(
                account=Account.objects.get(entity=entity, code=code), currency_id=functional, txn_amount=amount,
                fx_rate=ONE, rate_source=Src.FUNCTIONAL, line_kind=Kind_.NORMAL,
                description=f"Unrealised FX {period.name} / فروقات عملة غير محققة {period.name}",
            )
            _set_side(line, side, amount)
            lines.append(line)
    entry = JournalEntry.objects.create(
        entity=entity, date=period.end_date, created_by=user, reference=f"FXREV-{period.name}",
        description=f"FX revaluation {period.name} / إعادة تقييم العملات {period.name}",
    )
    for line in lines:
        line.entry = entry
        line.save()
    run.entry = entry
    run.save()
    problems = validate_entry(entry)
    if problems:
        raise LedgerError(problems)
    return run


def post_revaluation(run, grant):
    """Post a revaluation draft and, in the same transaction, its automatic reversal (R23).

    ``grant`` is ``require(approver, entity, "ledger.post_entry", document=run.entry)``;
    the person who ran the revaluation cannot approve it (access.md R14).
    """
    run = RevaluationRun.objects.select_related("entity", "period", "entry").get(pk=run.pk)
    if getattr(grant, "permission", None) != "ledger.post_entry":
        access.deny(getattr(grant, "user", None), run.entity, getattr(grant, "permission", ""), "grant_wrong_permission")
    if run.entry is None:
        raise error("This revaluation found no differences; nothing to post.", "لم تظهر فروقات في إعادة التقييم؛ لا شيء للترحيل.")
    user = _authorise_draft_post(run.entry, grant, None)
    with transaction.atomic():
        access.use_grant(grant, target=run.entry)
        return _post_revaluation(run, user)


@transaction.atomic
def _post_revaluation(run, user):
    run = RevaluationRun.objects.select_for_update(of=("self",)).select_related("entity", "period").get(pk=run.pk)
    if run.reversal_id is not None:
        raise error("This revaluation is already posted.", "إعادة التقييم هذه مرحّلة مسبقًا.")
    next_start = run.period.end_date + timedelta(days=1)
    _find_open_period(run.entity, next_start, lock=True)
    entry = _post(run.entry, user)
    reversal = _reverse(
        entry, user, next_start, "Automatic reversal of FX revaluation / عكس تلقائي لإعادة تقييم العملات",
    )
    run.reversal = reversal
    run.save(update_fields=["reversal"])
    return run


@transaction.atomic
def discard_revaluation(run, *, user):
    """Remove a revaluation run whose entry is still a draft (or that found nothing), so it can be run again.

    A posted revaluation cannot be re-run (R24, OQ-9 default): correct it with a manual journal before closing.
    """
    run = RevaluationRun.objects.select_for_update(of=("self",)).select_related("entity", "entry").get(pk=run.pk)
    access.require(user, run.entity, "ledger.revalue")
    if run.entry is not None and run.entry.status != JournalEntry.Status.DRAFT:
        raise error(
            "A posted revaluation cannot be discarded; correct it with a manual journal (OQ-9).",
            "لا يمكن إلغاء إعادة تقييم مرحّلة؛ صحّحها بقيد يدوي (OQ-9).",
        )
    entry = run.entry
    run.delete()
    if entry is not None:
        entry.delete()


# --- translation and functional currency (R4, R5, R30, R31) ------------------------

def translate_trial_balance(entity, as_of, period_from, target_currency, *, user):
    """Report-only translation of ``entity``'s trial balance into ``target_currency`` (R30). Never posts.

    An entity whose functional currency is the target is returned unchanged (R31).
    Translating a foreign-functional entity is not built: the method (average
    vs transaction-date rates, source of historical equity rates) is open, OQ-5.
    """
    access.require(user, entity, "ledger.view_translation")
    target = _currency(target_currency, for_new_line=False)
    if entity.base_currency_id == target.code:
        return _trial_balance(entity, as_of)
    raise NotImplementedError(
        f"Translation of {entity.code} ({entity.base_currency_id}) into {target.code} is not built: the method is "
        "open (ledger-fx.md OQ-5). / ترجمة القوائم لكيان بعملة وظيفية أجنبية غير مبنية: الطريقة لم تُحدد (OQ-5)."
    )


@transaction.atomic
def set_functional_currency(entity, currency, *, user):
    """Change ``entity``'s functional currency before its first posting (R4, R5); Managing Partner only (3.6)."""
    access.require(user, entity, "ledger.change_functional_currency")
    entity = Entity.objects.select_for_update().get(pk=entity.pk)
    before = entity.base_currency_id
    entity.base_currency = _currency(currency)
    try:
        entity.save(update_fields=["base_currency"])
    except ValidationError as exc:
        raise LedgerError(exc.messages) from None
    if before != entity.base_currency_id:  # access.md R32: logged with before / after
        access_log(
            AccessLog.Event.ENTITY_SETTINGS_CHANGED, actor=user, entity=entity,
            permission="ledger.change_functional_currency",
            details={"before": {"base_currency": before}, "after": {"base_currency": entity.base_currency_id}},
        )
    return entity
