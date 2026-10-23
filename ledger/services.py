"""The only supported way to post, reverse and report on the ledger.

Multi-currency (docs/specs/ledger-fx.md): debit/credit are functional-currency
amounts; each line also carries its transaction currency, amount and rate.
"""
import csv
import io
import uuid
from collections import defaultdict
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, models, transaction
from django.db.models import Case, F, Q, Sum, When
from django.utils import timezone

from core import access
from core.access import catalogue
from core.access import engine as approval_engine
from core.access.models import AccessLog, Kind
from core.access import services as access_services
from core.access.services import kind_of
from core.access.services import log as access_log
from core.models import Currency, Entity

from . import control_accounts, documents, fx, restricted
from .fx import ONE, error
from .models import (
    Account,
    EntrySequence,
    ExchangeRate,
    FiscalPeriod,
    JournalEntry,
    JournalLine,
    LedgerError,
    NonPublicationDay,
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
TRANSLATION_RESERVE = "3400"
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


def _bi(en, ar):
    """One problem text in both languages, as fx.error formats it (ledger-fx R37)."""
    return f"{en} / {ar}"


def validate_entry(entry):
    """Return a list of problems ("English / العربية"); empty means the entry can be posted."""
    problems = []
    lines = list(entry.lines.select_related("account", "account__entity"))
    if len(lines) < 2:
        problems.append(_bi("An entry needs at least two lines.", "يحتاج القيد إلى بندين على الأقل."))
    debit = sum((l.debit for l in lines), ZERO)
    credit = sum((l.credit for l in lines), ZERO)
    if debit != credit:
        problems.append(_bi(f"Entry does not balance: debits {debit} vs credits {credit}.",
                            f"القيد غير متوازن: المدين {debit} والدائن {credit}."))
    if debit == ZERO:
        problems.append(_bi("Entry total is zero.", "مجموع القيد صفر."))
    if not entry.entity.is_active:
        problems.append(_bi(f"Entity {entry.entity.code} is inactive.", f"الكيان {entry.entity.code} غير نشط."))
    for line in lines:
        acc = line.account
        if acc.entity_id != entry.entity_id:
            problems.append(_bi(f"Account {acc.code} belongs to another entity.", f"الحساب {acc.code} يخص كيانًا آخر."))
        if not acc.is_postable or not acc.is_active:
            problems.append(_bi(f"Account {acc.code} is not open for posting.", f"الحساب {acc.code} غير مفتوح للترحيل."))
        # A reversal only undoes what was booked (e.g. before the entity enforced Sharia): never blocked here.
        if acc.sharia_prohibited and entry.entity.sharia_enforced and entry.reversal_of_id is None:
            problems.append(_bi(f"Account {acc.code} ({acc.name_en}) is Sharia-prohibited.",
                                f"الحساب {acc.code} ({acc.name_ar}) محظور شرعًا."))
        if acc.code == TRANSLATION_RESERVE:  # R46, OQ-6: report-only, in every entity
            problems.append(
                f"Account {TRANSLATION_RESERVE} (translation reserve) is used by the translation report only; "
                "nothing posts to it. / "
                f"الحساب {TRANSLATION_RESERVE} (احتياطي ترجمة العملات) للتقرير فقط؛ لا يُرحَّل إليه شيء."
            )
        if acc.is_intercompany and line.counterparty_entity_id is None:
            problems.append(_bi(f"Intercompany account {acc.code} needs a counterparty entity.",
                                f"حساب المعاملات بين الشركات {acc.code} يحتاج إلى كيان مقابل."))
        if line.counterparty_entity_id == entry.entity_id:
            problems.append(_bi("Counterparty cannot be the same entity.", "لا يكون الطرف المقابل هو الكيان نفسه."))
    if entry.reversal_of_id is not None:  # the Sharia and rate checks above trust a reversal: prove it is one
        problems += _reversal_problems(entry, lines)
    problems += _currency_problems(entry, lines)
    problems += _restricted_problems(entry, lines)
    problems += _control_problems(entry, lines)
    return problems


# Checks another app adds to the posting of the drafts it created (e.g. migration R16 and R22: an import draft whose
# lines changed, or an opening entry whose control accounts do not tie out to the open items loaded, never posts,
# whichever path posts it: the migration command or the approval engine's inbox). ``fn(entry)`` returns problems
# ("English / العربية"), empty when the entry is not the app's or passes; it runs inside the posting transaction,
# after the entry and its period are locked. Registered from ``AppConfig.ready()``.
_POST_CHECKS = []


def register_post_check(fn, *, code):
    """Register ``fn`` (idempotent); a posting it refuses raises LedgerError with ``code``."""
    if all(f is not fn for f, _ in _POST_CHECKS):
        _POST_CHECKS.append((fn, code))


def _post_check_problems(entry):
    out, code = [], None
    for fn, fn_code in _POST_CHECKS:
        found = list(fn(entry) or [])
        if found and code is None:
            code = fn_code
        out += found
    return out, code


def migration_opening_posted(entity, *, lock=False, booked=True):
    """True once ``entity``'s migration opening entry (a draft linked to an ``ImportedEntry`` of kind ``opening``,
    migration R14) is posted (``booked``: posted or reversed; else posted only). With ``lock`` the opening entries
    are locked first (``select_for_update``), so a sub-ledger load and the posting of the opening entry, which locks
    the same row, run one after the other (migration R22, R25). Only the live opening import counts: one superseded
    by a reload after its entry was reversed does not (migration R14, team default 2026-10-23), so the R25 gates
    apply to the new opening entry again. Internal: callers check their own rights."""
    qs = JournalEntry.objects.filter(entity=entity, legacy_import__kind="opening")
    if lock:
        list(qs.select_for_update(of=("self",)).order_by("pk").values_list("pk", flat=True))
    statuses = BOOKED if booked else (JournalEntry.Status.POSTED,)
    return qs.filter(status__in=statuses, legacy_import__superseded_at__isnull=True).exists()


# --- control accounts (invoicing R97, policy A, D32(a); D33(e); banking R32 note B11(b); migration M6, R8) -----

def _control_codes(lines):
    return sorted({line.account.code for line in lines} & control_accounts.SUBLEDGER_CONTROL_CODES)


def _control_account_refusal(account, *, rebooks=False):
    """The bilingual refusal of a line on sub-ledger control account ``account`` (an Account) that the entry may not
    post (R97). ``rebooks``: the entry reverses a reversal, i.e. books its root entry's lines again."""
    en = (f"Account {account.code} ({account.name_en}) is a control account: only the documents allowed on it post "
          "there (and the migration opening entry), never a manual journal (policy A).")
    ar = (f"الحساب {account.code} ({account.name_ar}) حساب مراقبة: لا يُرحَّل إليه إلا من المستندات المسموح لها "
          "بذلك (والقيد الافتتاحي للترحيل)، ولا يُرحَّل إليه بقيد يدوي (السياسة أ).")
    if rebooks:
        en += (" Reversing a reversal books the original lines again, which only a document allowed on the account "
               "may do; to restore a migration opening entry, run the migration load again.")
        ar += (" عكس قيد العكس يعيد ترحيل البنود الأصلية، ولا يجوز ذلك إلا لمستند مسموح له بالحساب؛ ولإعادة القيد "
               "الافتتاحي للترحيل أعد تشغيل تحميل الترحيل.")
    return _bi(en, ar)


def _reversal_chain(entry):
    """(depth, root) of ``entry`` in its reversal chain: depth 0 for an entry that reverses nothing, 1 for a reversal,
    2 for the reversal of a reversal, and so on; root is the first entry of the chain."""
    depth, current, seen = 0, entry, set()
    while current.reversal_of_id is not None and current.pk not in seen:
        seen.add(current.pk)
        found = JournalEntry.objects.filter(pk=current.reversal_of_id).first()
        if found is None:  # a broken chain proves nothing about what the entry undoes or books again
            raise error(f"Entry {current.number or current.pk} reverses an entry that does not exist; it cannot be "
                        "judged on a control account.",
                        f"القيد {current.number or current.pk} يعكس قيدًا غير موجود؛ لا يمكن الحكم عليه في حساب مراقبة.",
                        code="control_account")
        current = found
        depth += 1
    return depth, current


def _is_migration_opening(entry):
    """D33(e), migration M6 and R14: the migration opening entry (``source="import"``, no document, linked to an
    ``ImportedEntry`` of kind ``opening``) is the one manual journal allowed on control accounts. Its tie-out to the
    open items (migration R22, invoicing R115) is checked by migration's registered post check."""
    return (entry.source == JournalEntry.Source.IMPORT and not entry.document_type and entry.pk is not None
            and JournalEntry.objects.filter(pk=entry.pk, entity_id=entry.entity_id, legacy_import__kind="opening",
                                            legacy_import__entity_id=entry.entity_id,
                                            legacy_import__superseded_at__isnull=True).exists())


def _control_problems(entry, lines):
    """R97: a line on a sub-ledger control account posts only when

    - the entry reverses a posted entry that is not itself a reversal (it only undoes what was booked;
      ``_reversal_problems`` proves it mirrors its original line by line, rule 1). Deeper in a chain, an entry at an
      odd depth also only undoes the root's lines; one at an even depth (the reversal of a reversal) books the
      root's lines again and is judged as the root: allowed only when the root is a declared document posting (the
      document's ``reverse_guard`` applies in ``reverse_entry``), refused for the migration opening entry, a
      revaluation and any manual-origin entry (review of 3b4cb6e, item 1),
    - it is a revaluation run's entry and the line is a revaluation line (ledger-fx R21, policy C),
    - it is a registered document's entry whose type declared the account (``ledger.documents``, ``control_accounts``)
      and whose predicate accepts the stored document, or
    - it is the migration opening entry (D33(e)).

    Everything else, manual journals, replayed migration journals and imports not linked to an opening
    ``ImportedEntry`` included, is refused (``control_account``, en/ar)."""
    codes = _control_codes(lines)
    if not codes:
        return []
    rebooks = False
    if entry.reversal_of_id is not None:
        depth, root = _reversal_chain(entry)
        if depth % 2 == 1:
            return []
        rebooks = True
    refused = set(codes)
    judged = root if rebooks else entry
    if judged.document_type:
        doc_type = documents.get(judged.document_type)
        document = None
        if doc_type is not None and judged.document_id is not None:
            document = doc_type.model._default_manager.filter(pk=judged.document_id).first()
        if document is not None:
            refused = {code for code in codes if not doc_type.may_post_control(code, document)}
    elif rebooks:
        pass  # the opening entry, a revaluation or a manual journal booked again: refused
    elif entry.pk is not None and _is_revaluation_entry(entry):
        refused = {l.account.code for l in lines if l.account.code in refused and l.line_kind != Kind_.REVALUATION}
    elif _is_migration_opening(entry):
        refused = set()
    accounts = {}
    for line in lines:
        if line.account.code in refused:
            accounts.setdefault(line.account.code, line.account)
    return [_control_account_refusal(accounts[code], rebooks=rebooks) for code in sorted(accounts)]


def _refuse_control_draft(entity, lines, source):
    """R97: a manual draft never carries a line on a control account; an import draft may (the migration opening
    entry is linked to its ``ImportedEntry`` after the draft is created; every import draft is checked again when
    posted, and replayed journals are refused then)."""
    if source == JournalEntry.Source.IMPORT:
        return
    accounts = {}
    for line in lines:
        if line.account.code in control_accounts.SUBLEDGER_CONTROL_CODES:
            accounts.setdefault(line.account.code, line.account)
    if accounts:
        raise _coded_error([_control_account_refusal(accounts[code]) for code in sorted(accounts)], "control_account")


def _coded_error(problems, code):
    exc = LedgerError(problems)
    exc.code = code
    return exc


def _restricted_codes(lines):
    return sorted({line.account.code for line in lines} & restricted.codes())


def _restricted_problems(entry, lines):
    """Restricted accounts (ledger.restricted; invoicing R54 D31b): a line on one is posted only when a registered
    allowance accepts the entry (a reversal: the entry it reverses), and then only if the account's own check
    passes (e.g. the purification account never below zero)."""
    problems = []
    for code in _restricted_codes(lines):
        if not restricted.allowed(code, entry):
            problems.append(restricted.refusal(code))
            continue
        spec = restricted.get(code)
        if spec.check is not None:
            problems += [str(p) for p in (spec.check(entry, lines) or [])]
    return problems


def _lock_restricted(entry):
    """The restricted accounts' own locks (e.g. the purification lock), taken after the entry row and before the
    period lock and validation, so two postings on the account in one entity never pass its check together."""
    codes = _restricted_codes(JournalLine.objects.filter(entry=entry).select_related("account"))
    for code in codes:
        spec = restricted.get(code)
        if spec.lock is not None:
            spec.lock(entry.entity)


def _refuse_restricted_draft(entity, lines, source):
    """A manual draft never carries a line on a restricted account (only documents post to it); an import draft
    may (a migration opening entry, checked again when posted)."""
    if source == JournalEntry.Source.IMPORT:
        return
    codes = _restricted_codes(lines)
    if codes:
        exc = LedgerError([restricted.refusal(code) for code in codes])
        exc.code = "restricted_account"
        raise exc


def _mirrors(rev, orig):
    """True if line ``rev`` undoes line ``orig`` exactly (what _reverse writes)."""
    return (
        rev.account_id == orig.account_id
        and rev.debit == orig.credit
        and rev.credit == orig.debit
        and rev.currency_id == orig.currency_id
        and rev.txn_amount == orig.txn_amount
        and rev.fx_rate == orig.fx_rate
        and rev.counterparty_entity_id == orig.counterparty_entity_id
        and rev.rate_source == orig.rate_source
        and rev.exchange_rate_id == orig.exchange_rate_id
        and rev.settles_line_id == orig.settles_line_id
        and rev.line_kind == orig.line_kind
    )


def _reversal_problems(entry, lines):
    """A reversal undoes one posted entry of its entity and document, line by line (rules 1 and 5, R30).

    Reversals skip the Sharia, inactive-currency and rate-date checks
    because they only undo what was booked, so a draft that merely claims
    to be a reversal must mirror its original exactly.
    """
    original = JournalEntry.objects.filter(pk=entry.reversal_of_id).first()
    if (
        original is None
        or original.status != JournalEntry.Status.POSTED
        or original.entity_id != entry.entity_id
        or (original.document_type, original.document_id) != (entry.document_type, entry.document_id)
    ):
        return [_bi("A reversal undoes a posted entry of the same entity and document.",
                    "العكس يلغي قيدًا مرحّلًا للكيان نفسه والمستند نفسه.")]
    mine = sorted(lines, key=lambda l: l.pk)
    theirs = list(JournalLine.objects.filter(entry_id=original.pk).order_by("pk"))
    if len(mine) != len(theirs) or not all(_mirrors(r, o) for r, o in zip(mine, theirs)):
        return [_bi(
            f"A reversal mirrors every line of {original.number or original.pk}: account, side, currency, "
            "transaction amount, rate and counterparty.",
            f"العكس يقابل كل بنود القيد {original.number or original.pk}: الحساب والجانب والعملة ومبلغ المعاملة "
            "والسعر والطرف المقابل.",
        )]
    return []


def _is_revaluation_entry(entry):
    """R10: entries created by revalue_period, the automatic reversal of one, or the two correction entries of
    reverse_revaluation."""
    ids = [entry.pk] + ([entry.reversal_of_id] if entry.reversal_of_id else [])
    return RevaluationRun.objects.filter(
        Q(entry_id__in=ids) | Q(correction_entry_id=entry.pk) | Q(correction_reversal_id=entry.pk)).exists()


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
                    # A document entry may use the rate row its document pinned at approval, if that row is
                    # still a valid rate for the entry date. Only a type's build_lines names such a row: caller
                    # lines with exchange_rate are refused (create_entry, _document_lines).
                    pinned = (_pinned_rate(entity, line.exchange_rate, cur.code, functional, entry.date)
                              if entry.document_type else None)
                    if pinned is None or pinned.rate != line.fx_rate:
                        problems.append(
                            f"{where}: rate does not match entry date {entry.date}; re-rate the draft. / "
                            f"السعر لا يطابق تاريخ القيد {entry.date}؛ أعد تسعير المسودة."
                        )
        if line.rate_source == Src.SAMA_TAX_POINT and not is_reversal:  # R51; a reversal copies the original
            problems += _sama_tax_point_problems(entry, line, cur.code, functional, where)
        if line.line_kind == Kind_.SETTLEMENT or line.rate_source == Src.CARRYING or line.settles_line_id:
            problems += _settlement_problems(entry, line, where, is_reversal, consumed)
        elif cur.code != functional and amount != fx.convert(line.txn_amount, line.fx_rate):  # R7
            problems.append(
                f"{where}: functional amount {amount} should be {fx.convert(line.txn_amount, line.fx_rate)} "
                f"({line.txn_amount} x {line.fx_rate}). / المبلغ بالعملة الوظيفية {amount} يجب أن يكون "
                f"{fx.convert(line.txn_amount, line.fx_rate)}."
            )
    return problems


def _sama_row_problem(row, code, functional, on, *, bridge=True, as_of=None):
    """R51 (owner decisions H2, I2): None if ``row`` is the SAMA rate for ``code`` on ``on`` (a tax point), else the
    (English, Arabic) reason. The SAMA rate for ``on`` is a global spot ``source=sama`` row from ``code`` to SAR, in
    an entity whose functional currency is SAR, dated ``on`` or earlier and the **latest** such row (as
    :func:`get_sama_rate`, I2): no other SAMA row of the pair is dated after it up to ``on``. ``as_of`` (the time
    the document chose its row): only rows recorded by then count, so a SAMA row entered later for a day in the gap
    does not undo a document issued before it. ``bridge=False``: only the kind of row and that it is not dated after
    ``on`` (the entry date, when the lines are built and the tax point is not known yet).
    """
    if functional != SAR or code == SAR:
        return (f"A SAMA tax-point rate books only foreign-currency lines in a SAR entity ({code}->{functional}).",
                f"سعر البنك المركزي في تاريخ استحقاق الضريبة لبنود العملة الأجنبية في كيان عملته الريال فقط "
                f"({code}->{functional}).")
    if (row.entity_id is not None or row.source != ExchangeRate.Source.SAMA
            or row.rate_type != ExchangeRate.RateType.SPOT or (row.from_currency_id, row.to_currency_id) != (code, SAR)):
        return (f"Rate row {row.pk} is not a group-wide SAMA spot rate {code}->SAR.",
                f"سجل السعر {row.pk} ليس سعرًا فوريًا للبنك المركزي السعودي {code}->SAR للمجموعة.")
    if row.date > on:
        return (f"SAMA rate row {row.pk} ({row.date}) is dated after {on}.",
                f"سجل سعر البنك المركزي {row.pk} ({row.date}) مؤرخ بعد {on}.")
    if bridge and row.date < on:
        later = _sama_rows(code).filter(date__gt=row.date, date__lte=on)
        if as_of is not None:
            later = later.filter(created_at__lte=as_of)
        newer = later.order_by("-date", "-pk").first()
        if newer is not None:
            return (f"SAMA rate row {row.pk} ({row.date}) is not the SAMA rate for {on}: the later SAMA row "
                    f"{newer.pk} ({newer.date}) is the latest published on or before it (I2).",
                    f"سجل سعر البنك المركزي {row.pk} ({row.date}) ليس سعر البنك المركزي بتاريخ {on}: السجل الأحدث "
                    f"{newer.pk} ({newer.date}) هو آخر سعر منشور في ذلك التاريخ أو قبله.")
    return None


def _sama_tax_point_problems(entry, line, code, functional, where):
    """R51 (owner decisions H2, I2): a ``sama_tax_point`` line books a registered document (``SAMA_TAX_POINT_TYPES``,
    hook ``sama_tax_point``) at the SAMA rate on the tax point the stored document gives, not after the entry date,
    with that row's rate. The ledger checks this itself; the document only names its tax point (optionally with the
    time it chose its row: ``(tax_point, as_of)``) and row."""
    if line.exchange_rate_id is None:
        return [_bi(f"{where}: a SAMA tax-point rate needs its rate row.",
                    f"{where}: سعر البنك المركزي في تاريخ استحقاق الضريبة يحتاج إلى سجل السعر.")]
    if line.line_kind != Kind_.NORMAL:
        return [_bi(f"{where}: only normal document lines book at a SAMA tax-point rate.",
                    f"{where}: بنود المستند العادية فقط تُرحَّل بسعر البنك المركزي في تاريخ استحقاق الضريبة.")]
    doc_type = documents.get(entry.document_type) if entry.document_type else None
    if (doc_type is None or doc_type.sama_tax_point is None
            or doc_type.label not in documents.SAMA_TAX_POINT_TYPES):
        return [_bi(f"{where}: a SAMA tax-point rate is used only by a document type registered for it.",
                    f"{where}: سعر البنك المركزي في تاريخ استحقاق الضريبة لا يستخدمه إلا نوع مستند مسجل لذلك.")]
    document = doc_type.model._default_manager.filter(pk=entry.document_id).first()
    tax_point = doc_type.sama_tax_point(document) if document is not None else None
    as_of = None
    if isinstance(tax_point, tuple) and len(tax_point) == 2 and isinstance(tax_point[1], (datetime, type(None))):
        tax_point, as_of = tax_point
    if not isinstance(tax_point, date_type) or isinstance(tax_point, datetime) or tax_point > entry.date:
        return [_bi(f"{where}: the document gives no tax point on or before the entry date {entry.date}.",
                    f"{where}: لا يحدد المستند تاريخ استحقاق ضريبة في تاريخ القيد {entry.date} أو قبله.")]
    problem = _sama_row_problem(line.exchange_rate, code, functional, tax_point, as_of=as_of)
    if problem is None and line.fx_rate != line.exchange_rate.rate:
        problem = (f"the line rate {line.fx_rate} is not the SAMA row's rate {line.exchange_rate.rate}.",
                   f"سعر البند {line.fx_rate} ليس سعر سجل البنك المركزي {line.exchange_rate.rate}.")
    if problem is not None:
        return [_bi(f"{where}: {problem[0]}", f"{where}: {problem[1]}")]
    return []


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
        raise error("posted_by is set from the grant; only posting on clearance records another approver.",
                    "يُحدَّد المرحِّل من التفويض؛ الترحيل عند الاعتماد من الهيئة وحده يسجل معتمدًا آخر.")
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
    creator, submitter and last modifier cannot post it (access R14, R35), unless its type's registered
    ``creator_may_post`` allows it for that document (customer receipts, access R46). A
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
    creator_id = doc_type.creator_id_of(stored)
    if creator_id is None:  # nobody recorded as creator: segregation could not keep the creator out (R35)
        access.deny(user, entity, grant.permission, "document_no_creator")
    if user.pk in access_services._document_people(stored) | {creator_id} and not (
        grant.permission != CLEARANCE_PERMISSION and (doc_type.self_post_allowed(stored) or grant.top_authority)
    ):  # access R46: only a type's own creator_may_post (customer receipts) lets its makers post it; access R58 /
        # R79: or the approval engine's top-authority self-approval (verified against its log row above, R31)
        access.deny(user, entity, grant.permission, "self_approval")
    if _document_booked(entity, stored):
        raise _document_booked_error((doc_type.label, stored.pk))
    if grant.permission == CLEARANCE_PERMISSION:
        approver, basis = _clearance_approver(entity, user, grant, posted_by, stored, doc_type, created_by)
        _approval_basis.set(basis)
        return approver, stored
    if posted_by is not None and posted_by != user:
        raise error("posted_by is set from the grant; only posting on clearance records another approver.",
                    "يُحدَّد المرحِّل من التفويض؛ الترحيل عند الاعتماد من الهيئة وحده يسجل معتمدًا آخر.")
    return user, stored


def _document_booked_error(ref):
    label, pk = ref
    return error(
        f"{label} {pk} is already booked; a document posts once (reverse it to correct it).",
        f"المستند {label} {pk} مرحّل مسبقًا؛ يُرحَّل المستند مرة واحدة (اعكسه للتصحيح).",
        code="document_booked",
    )


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
    person_ok = (
        getattr(posted_by, "pk", None) is not None
        and kind_of(posted_by) == Kind.HUMAN
        and posted_by.pk != getattr(created_by, "pk", None)
        and posted_by.pk not in access_services._document_people(document)
    )
    if person_ok and posted_by.pk in approvals:
        return posted_by, {"permission": APPROVE_SALES_PERMISSION, "document": list(_ref(document))}
    contract = _contract_approval(entity, posted_by, document, doc_type) if person_ok else None
    if contract is None:
        access.deny(user, entity, grant.permission, "posted_by_not_approver")
    return posted_by, contract


def _ref(obj):
    return (obj._meta.label_lower, obj.pk)


def _contract_approval(entity, posted_by, document, doc_type):
    """billing R53: an invoice auto-issued from billing has no per-document approval; the approval that counts
    is ``posted_by``'s granted approval of the billed contract version, named by the type's registered
    ``contract_approval`` hook and found in the access log for that very record, issued no later than the recorded
    approval time and recorded as used (GrantUse). Returns the basis for the access log, or None."""
    if doc_type.contract_approval is None or doc_type.label not in documents.CONTRACT_APPROVAL_TYPES:
        return None
    ref = doc_type.contract_approval(document)
    if not isinstance(ref, documents.RecordedApproval) or ref.permission not in documents.CONTRACT_APPROVAL_PERMISSIONS:
        return None
    target = ref.target
    if getattr(target, "pk", None) is None or getattr(target, "entity_id", None) != entity.pk:
        return None
    if ref.approver_id != posted_by.pk or ref.approved_at is None:
        return None
    # Only an approval that was actually used for that record (a GrantUse row, written by billing's approve_contract
    # / approve_version): a grant issued for a return or a rejection of the same record never counts.
    approvers = access_services.recorded_approvers(entity, ref.permission, target, used=True, until=ref.approved_at)
    if posted_by.pk not in approvers:
        return None
    return {"permission": ref.permission, "document": list(_ref(target))}


def _purification_flags(entry, lines):
    """CLAUDE.md rule 5, invoicing R6: the lines of ``entry`` that carry non-compliant income, set at posting.

    In an ordinary entry, a credit on an account flagged
    ``requires_purification`` (4900), or on a Sharia-prohibited income
    account (which can only post in an entity that does not enforce
    Sharia), is non-compliant income. Debits on those accounts (the
    purification transfer E14) are not flagged.

    A reversal flags exactly the lines that mirror flagged lines of the
    original, so the flags carry their sign: a reversed income line is a
    flagged debit and nets the original to zero, and reversing a
    purification transfer (an unflagged debit) creates no new income.
    """
    if entry.reversal_of_id:
        original = list(JournalLine.objects.filter(entry_id=entry.reversal_of_id).order_by("pk"))
        mine = sorted(lines, key=lambda l: l.pk)
        if len(original) != len(mine):
            raise error("A reversal mirrors every line of its entry.", "العكس يقابل كل بنود القيد.")
        return [rev.pk for rev, orig in zip(mine, original) if orig.requires_purification]
    return [
        line.pk for line in lines
        if line.credit > ZERO and line.line_kind == Kind_.NORMAL and (
            line.account.requires_purification
            or (line.account.sharia_prohibited and line.account.type == Account.Type.INCOME)
        )
    ]


def signed_purification(lines):
    """Net non-compliant income of ``lines``: flagged credits minus flagged debits (rule 5).

    Pure helper over lines the caller has read; reversals net their original to zero.
    """
    return sum(((l.credit - l.debit) for l in lines if l.requires_purification), ZERO)


def purification_amount(entity, *, user, as_of=None, date_from=None):
    """Non-compliant income of ``entity`` still to purify from booked entries, signed (rule 5, invoicing R6).

    Flagged credits minus flagged debits on posted and reversed entries
    (a reversal cancels its original). The purification transfers
    themselves (Dr 4900 / Cr 2600) are not flagged and do not reduce it;
    what was transferred is on 2600. ``user`` needs ``ledger.view``.
    """
    access.require(user, entity, "ledger.view")
    qs = JournalLine.objects.filter(entry__entity=entity, entry__status__in=BOOKED, requires_purification=True)
    if as_of is not None:
        qs = qs.filter(entry__date__lte=as_of)
    if date_from is not None:
        qs = qs.filter(entry__date__gte=date_from)
    return signed_purification(qs.only("debit", "credit", "requires_purification"))


def _booked_balance_floor(entity, code, on):
    """The smallest booked balance (debits less credits, functional amounts) of account ``code`` of ``entity`` on
    ``on`` and on every later booking date, as ``(amount, date)``: the room a credit dated ``on`` has without
    taking the account below zero then or later. Posted and reversed entries count; drafts do not. Internal
    helper for module checks (no access check): callers check their own rights first."""
    deltas = {}
    rows = (JournalLine.objects.filter(entry__entity=entity, entry__status__in=BOOKED, account__code=code)
            .values("entry__date").annotate(d=Sum("debit"), c=Sum("credit")))
    for row in rows:
        deltas[row["entry__date"]] = (row["d"] or ZERO) - (row["c"] or ZERO)
    deltas.setdefault(on, ZERO)
    balance, room, at = ZERO, None, None
    for day in sorted(deltas):
        balance += deltas[day]
        if day >= on and (room is None or balance < room):
            room, at = balance, day
    return room.quantize(Decimal("0.01")), at  # SQLite sums lose the scale


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
    _lock_restricted(entry)
    period = _lock_for_posting(entry)
    problems = validate_entry(entry)
    extra, extra_code = _post_check_problems(entry)
    if problems or extra:
        exc = LedgerError(problems + extra)
        if {restricted.refusal(code) for code in restricted.codes()} & set(problems):
            exc.code = "restricted_account"
        elif _control_problems(entry, list(entry.lines.select_related("account"))):
            exc.code = "control_account"
        elif not problems:  # the ledger's own refusal keeps its code; an app's code only when the ledger found none
            exc.code = extra_code
        raise exc
    entry.period = period
    lines = list(entry.lines.select_related("account"))
    flagged = set(_purification_flags(entry, lines))
    wrong = [l.pk for l in lines if l.requires_purification != (l.pk in flagged)]
    if wrong:  # still a draft here, so the line lock allows it; flags are set only here
        for pk in wrong:  # base update: the posting itself is not a change of the approved draft
            models.QuerySet.update(JournalLine.objects.filter(pk=pk), requires_purification=pk in flagged)
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
    if _is_revaluation_entry(entry):  # posted with its automatic reversal, after a fresh check (R23, R44)
        raise error("A revaluation draft is posted with post_revaluation.", "تُرحَّل مسودة إعادة التقييم من خلال ترحيل إعادة التقييم.",
                    code="use_post_revaluation")
    if entry.reversal_of_id is not None or entry.document_ref is not None:
        # Reversals post in reverse_entry, document entries in create_entry(post=True); never as manual drafts.
        raise error("This entry is posted by the service that created it (a reversal or a document's posting).",
                    "يُرحَّل هذا القيد من خلال الخدمة التي أنشأته (عكس قيد أو ترحيل مستند).", code="draft_has_links")
    user = _authorise_draft_post(entry, grant, posted_by)
    with transaction.atomic():
        access.use_grant(grant, target=entry)
        return _post(entry, user)


def reverse_entry(entry, grant=None, date=None, reason=""):
    """Reverse a posted entry (R23, R30).

    ``grant`` is ``require(user, entity, "ledger.reverse_entry", document=entry)``,
    or a reversal permission registered for the entry's document type (e.g.
    ``invoicing.reverse_document``), issued for that document. An entry of a registered document type is
    reversed only with that type's reversal permission (never ``ledger.reverse_entry``), and only while the
    type's ``reverse_guard`` (if any) says its own module is reversing it (ledger/documents.py).
    """
    entry = JournalEntry.objects.select_related("entity").get(pk=entry.pk)
    if not isinstance(grant, access.Grant):
        _deny_grant(grant, entry.entity, "grant_missing")
    if _is_revaluation_entry(entry):  # its run would no longer match the ledger (OQ-9 team default)
        raise error("A revaluation is corrected with reverse_revaluation, which reverses the whole run.",
                    "تُصحَّح إعادة التقييم من خلال عكس إعادة التقييم، الذي يعكس التشغيل كله.",
                    code="use_reverse_revaluation")
    if grant.permission == "ledger.reverse_entry":
        if entry.document_type and documents.get(entry.document_type) is not None:
            # Review item 5: a registered document's entry is reversed only through its own module and
            # reversal permission, which keeps the document's status in step (never by a generic grant).
            _deny_grant(grant, entry.entity, "document_reversal_only")
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
        if doc_type.reverse_guard is not None and doc_type.reverse_guard(document) is not True:
            _deny_grant(grant, entry.entity, "reverse_through_owner")
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
        raise error("Only posted entries can be reversed.", "يمكن عكس القيود المرحّلة فقط.")
    date = date or entry.date
    reversal = JournalEntry(
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
    reversal._allow_links()  # the only place a reversal link is written
    reversal.save(force_insert=True)
    for line in entry.lines.all():
        copy = JournalLine(
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
        copy._service_write = True
        copy.save()
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
    carrying rate), ``exchange_rate`` (a rate row the document pinned at approval;
    document postings only, valid for ``date``) and ``line_kind="realised_fx"``. ``debit`` / ``credit`` are
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
    check_lines = None
    if post:
        posted_by, document = _authorise_document_post(entity, grant, posted_by, document, user)
        if not (getattr(user, "pk", None) is not None and user == grant.user):
            access.require(user, entity, "ledger.create_entry")
        ref = (document._meta.label_lower, document.pk)
        lines, check_lines = _document_lines(documents.get(ref[0]), document, lines)
    else:
        if grant is not None or posted_by is not None or document is not None:
            raise error("A grant, posted_by or document is only used with post=True.",
                        "يُستخدم التفويض أو المرحِّل أو المستند مع الترحيل المباشر فقط.")
        lines = list(lines) if lines is not None else None
        if lines and any("exchange_rate" in spec for spec in lines):
            raise error("A pinned rate row is used only by a document's own posting (post=True).",
                        "سجل السعر المثبت يُستخدم فقط في ترحيل المستند نفسه.", code="pinned_rate_not_allowed")
        # A draft is still a business record in ``entity``: ledger.create_entry there (access.md 3.6).
        access.require(user, entity, "ledger.create_entry")
    with transaction.atomic():
        if post:
            access.use_grant(grant, target=document)
        entry = _create(entity, date, description, lines, user, reference, post, posted_by, entry_source, ref,
                        check_lines)
        if post and grant.permission == CLEARANCE_PERMISSION:  # zatca D-8: who posted for whom, logged
            access_log(
                AccessLog.Event.PERMISSION_GRANTED, actor=grant.user, entity=entity, permission=CLEARANCE_PERMISSION,
                details={"action": "post_on_clearance", "posted_document": list(ref), "entry": entry.pk,
                         "posted_by": posted_by.pk, "approval": _approval_basis.get()},
            )
        return entry


# The approval a clearance posting relied on (the document's own approval, or the contract approval of billing
# R53), recorded in its access-log row.
_approval_basis = ContextVar("ledger_approval_basis", default=None)

# (entity id, document label, pk) while a registered type's build_lines runs inside create_entry(post=True):
# the only place _settlement_for_document may be called (verification review item 5).
_building_document = ContextVar("ledger_building_document", default=None)


def _document_lines(doc_type, document, lines):
    """(line dicts, check) for a document's posting through its registered hooks (ledger.documents).

    ``build_lines``: the type supplies the lines; the caller passes none.
    ``validate_lines``: ``check(built)`` runs it on the priced, unsaved lines before anything is written.
    """
    if doc_type.build_lines is not None:
        if lines is not None:
            raise error(
                f"{doc_type.label} builds its own lines; pass lines=None.",
                f"المستند {doc_type.label} يبني بنوده بنفسه؛ لا تمرّر بنودًا.", code="document_builds_lines",
            )
        token = _building_document.set((doc_type.entity_id_of(document), doc_type.label, document.pk))
        try:
            lines = list(doc_type.build_lines(document))
        finally:
            _building_document.reset(token)
    elif lines is None:
        raise error("An entry needs its lines.", "يحتاج القيد إلى بنوده.")
    else:
        lines = list(lines)
        # A pinned rate row may book at a rate older than the entry date's (_pinned_rate): only the document's
        # own build_lines, which reads the row the document stored at approval, may name one (review item 4).
        if any(isinstance(spec, dict) and "exchange_rate" in spec for spec in lines):
            raise error(
                f"{doc_type.label}: a pinned rate row comes only from the document's own build_lines, never from "
                "lines passed by the caller.",
                f"المستند {doc_type.label}: سجل السعر المثبت يأتي من بناء المستند لبنوده فقط، لا من بنود يمررها المستدعي.",
                code="pinned_rate_not_allowed",
            )
    if doc_type.validate_lines is None:
        return lines, None

    def check(built):
        problems = [str(p) for p in (doc_type.validate_lines(document, list(built)) or [])]
        if problems:
            exc = LedgerError([_bi(f"The lines do not match {doc_type.label} {document.pk}.",
                                   f"البنود لا تطابق المستند {doc_type.label} {document.pk}."), *problems])
            exc.code = "document_lines_rejected"
            raise exc

    return lines, check


@transaction.atomic
def _create(entity, date, description, lines, user, reference, post, posted_by, source=JournalEntry.Source.HUMAN,
            document_ref=None, check_lines=None):
    built = _build_lines(entity, date, lines, user)
    if document_ref is None:
        _refuse_restricted_draft(entity, built, source)
        _refuse_control_draft(entity, built, source)
    if check_lines is not None:  # the document type's own rule for its lines, before any write
        check_lines(built)
    entry = JournalEntry(
        entity=entity, date=date, description=description, reference=reference, created_by=user, source=source,
        document_type=document_ref[0] if document_ref else "", document_id=document_ref[1] if document_ref else None,
    )
    entry._allow_links()  # the only place a document link is written (R30)
    try:
        with transaction.atomic():
            entry.save(force_insert=True)
    except IntegrityError:
        # A concurrent posting of the same document committed first (one_entry_per_document, R36).
        if document_ref and JournalEntry.objects.filter(
            entity=entity, document_type=document_ref[0], document_id=document_ref[1], reversal_of__isnull=True,
        ).exists():
            raise _document_booked_error(document_ref) from None
        raise
    for line in built:
        line.entry = entry
        line._service_write = True
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
        if entry.document_ref is None and entry.reversal_of_id is None:
            _refuse_restricted_draft(entry.entity, built, entry.source)
            _refuse_control_draft(entry.entity, built, entry.source)
        entry.lines.all().delete()
        for line in built:
            line.entry = entry
            line._service_write = True
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
        approval_engine.submit(entry, user, submit_permission="ledger.create_entry")  # access R61, R71, R77: the request opens now; a gap in the bands is refused
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
    elif spec.get("exchange_rate") is not None and spec.get("rate_source") == Src.SAMA_TAX_POINT:
        # R51 (owner decision H2): the SAMA tax-point row of a sales document, possibly older than the rate age
        # limit. Checked here for its kind and date; against the document's tax point by validate_entry.
        row = spec["exchange_rate"]
        row = row if isinstance(row, ExchangeRate) else ExchangeRate.objects.get(pk=row)
        problem = _sama_row_problem(row, cur.code, functional, on, bridge=False)  # the tax point is checked later
        if problem is not None:
            raise error(problem[0], problem[1], code="sama_tax_point_invalid")
        line.rate_source, line.fx_rate, line.exchange_rate = Src.SAMA_TAX_POINT, row.rate, row
    elif spec.get("rate_source") not in (None, ""):
        raise error(
            f"Line {account.code}: rate source {spec.get('rate_source')!r} is not set by the caller (only a document's "
            "own SAMA tax-point row, R51).",
            f"البند {account.code}: مصدر السعر {spec.get('rate_source')!r} لا يحدده المستدعي (فقط سجل سعر البنك "
            "المركزي في تاريخ استحقاق الضريبة للمستند نفسه).", code="rate_source_reserved",
        )
    elif spec.get("exchange_rate") is not None:  # a rate row pinned by the document (create_entry, post=True)
        row = spec["exchange_rate"]
        row = row if isinstance(row, ExchangeRate) else ExchangeRate.objects.get(pk=row)
        found = None if cur.code == functional else _pinned_rate(entity, row, cur.code, functional, on)
        if found is None:
            raise error(
                f"Rate row {row.pk} is not a valid {cur.code}->{functional} rate for {on}.",
                f"سجل السعر {row.pk} ليس سعرًا صالحًا {cur.code}->{functional} بتاريخ {on}.", code="pinned_rate_invalid",
            )
        line.rate_source, line.fx_rate, line.exchange_rate = Src.TABLE, found.rate, row
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
        raise error("Draft entries exist in this period.", "توجد مسودات قيود في هذه الفترة.")
    pending = documents.pending_in_period(entity, period.start_date, period.end_date)
    if pending:  # e.g. sales documents awaiting ZATCA clearance (invoicing R43)
        raise error(
            f"Documents in this period still wait to be booked: {', '.join(pending[:10])}.",
            f"توجد مستندات في هذه الفترة لم تُرحَّل بعد: {', '.join(pending[:10])}.",
            code="documents_pending",
        )
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
        raise error(f"{group.code} is not a group parent entity.", f"{group.code} ليس الكيان الأم للمجموعة.")
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


def _pinned_rate(entity, row, from_code, to_code, on):
    """A Rate from ``row`` if it is a valid spot rate for ``from_code`` -> ``to_code`` on ``on``, else None.

    Valid means: the entity's own or a group-wide row, dated on ``on`` or up to the age limit before it,
    in that direction or its inverse. Used for rate rows a document pinned at approval (invoicing review
    item 2): the document books at its approved rate even when a later row for the date arrives.
    """
    if row is None or row.rate_type != ExchangeRate.RateType.SPOT or row.entity_id not in (None, entity.pk):
        return None
    if not (on - _max_rate_age() <= row.date <= on):
        return None
    pair = (row.from_currency_id, row.to_currency_id)
    if pair == (from_code, to_code):
        return Rate(row.rate, row)
    if pair == (to_code, from_code):
        return Rate(fx.inverse(row.rate), row, True)
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


SAR = "SAR"


def get_sama_rate(currency, on, *, user, entity):
    """VAT in SAR (ledger-fx R12 amendment; invoicing R100; owner decisions G7, I2): the SAMA rate for ``currency``
    -> SAR on ``on``, the document's tax point, for ``user`` (``ledger.view_rates`` in ``entity``, as ``get_rate``).

    Only global spot rows with ``source=sama`` count; never the manual, file or entity rows that ``get_rate``
    prefers. The row of ``on`` itself, else the **latest SAMA row published before** ``on`` (I2: after a long
    holiday or any other day without a published rate; no 5-day tolerance and no ``NonPublicationDay`` check, those
    days are information only). Raises ``sama_rate_missing`` (en/ar, naming currency and date) only when no SAMA
    row exists on or before ``on``. SAR itself: rate 1, no row.
    """
    access.require(user, entity, "ledger.view_rates")
    return _sama_rate(_currency(currency).code, on)


def _sama_rows(code):
    """Global spot SAMA rows ``code`` -> SAR (the only rows that are SAMA rates, R12 amendment, R51)."""
    return ExchangeRate.objects.filter(
        entity__isnull=True, source=ExchangeRate.Source.SAMA, rate_type=ExchangeRate.RateType.SPOT,
        from_currency_id=code, to_currency_id=SAR,
    )


def _sama_rate(code, on):
    """:func:`get_sama_rate` without the rights check (internal): the latest SAMA row on or before ``on`` (I2)."""
    if code == SAR:
        return Rate(ONE, None)
    row = (_sama_rows(code).filter(date__lte=on, from_currency__is_active=True)
           .order_by("-date", "-pk").first())
    if row is None:
        raise error(f"No SAMA exchange rate {code}->SAR on or before {on}; enter the SAMA rate for that date.",
                    f"لا يوجد سعر صرف من البنك المركزي السعودي {code}->SAR بتاريخ {on} أو قبله؛ أدخل سعر البنك "
                    "المركزي لذلك التاريخ.", code="sama_rate_missing")
    return Rate(row.rate, row)


def _name_problem(name_en, name_ar):
    if not name_en or not name_ar:
        raise error("The day needs an English and an Arabic name.", "يحتاج اليوم إلى اسم بالإنجليزية والعربية.",
                    code="name_required")


@transaction.atomic
def create_non_publication_day(*, user, on, name_en, name_ar, group=None, source=""):
    """Record a weekend day or KSA public holiday on which SAMA publishes no rate (R12 amendment, AT-41).

    Global reference data, written like a global rate (R28: the group parent's treasury). Bilingual name required.
    A day entered by hand is taken from the announcement by the person entering it, like a SAMA rate row, and is
    verified by that entry (``verified_by`` = ``user``); seeded proposals are loaded unverified (H1,
    :func:`load_non_publication_days`).
    """
    _authorise_rate_write(user, None, group)
    name_en, name_ar = (name_en or "").strip(), (name_ar or "").strip()
    _name_problem(name_en, name_ar)
    if NonPublicationDay.objects.filter(date=on).exists():
        raise error(f"{on} is already recorded as a non-publication day.", f"اليوم {on} مسجل مسبقًا يومًا بلا نشر.",
                    code="duplicate_day")
    return NonPublicationDay.objects.create(date=on, name_en=name_en, name_ar=name_ar, created_by=user,
                                            source=(source or "").strip()[:300], verified_by=user,
                                            verified_at=timezone.now())


def _non_publication_writer(user, group):
    """H1: loading and verifying proposed days is done by a person holding ``ledger.maintain_global_rate`` at the
    top-level group parent (R28); system users and agents are refused (logged)."""
    group = _group_parent(group)
    access.require(user, group, "ledger.maintain_global_rate")  # refuses system users and agents (logged)
    if kind_of(user) != Kind.HUMAN:
        access.deny(user, group, "ledger.maintain_global_rate", "system_forbidden")
    return group


def _seed_row(raw, index):
    """(date, name_en, name_ar, source) of one seed row; bilingual LedgerError naming the row when malformed."""
    where = f"Row {index}"
    if not isinstance(raw, dict):
        raise error(f"{where} is not an object.", f"السطر {index} ليس كائنًا.", code="seed_row_invalid")
    try:
        on = date_type.fromisoformat(str(raw.get("date", "")))
    except ValueError:
        raise error(f"{where}: date {raw.get('date')!r} is not YYYY-MM-DD.",
                    f"السطر {index}: التاريخ {raw.get('date')!r} ليس بصيغة YYYY-MM-DD.", code="seed_row_invalid") from None
    name_en, name_ar = str(raw.get("name_en") or "").strip(), str(raw.get("name_ar") or "").strip()
    source = str(raw.get("source") or "").strip()
    if not name_en or not name_ar or not source:
        raise error(f"{where} ({on}) needs name_en, name_ar and source.",
                    f"السطر {index} ({on}) يحتاج إلى الاسم بالإنجليزية والعربية والمصدر.", code="seed_row_invalid")
    return on, name_en[:120], name_ar[:120], source[:300]


def load_non_publication_days(rows, *, user, group=None, apply=False):
    """Load proposed SAMA non-publication days (owner decision H1) **unverified**.

    ``rows``: dicts with ``date``, ``name_en``, ``name_ar``, ``source`` (the seed file). Dates already recorded are
    skipped and never changed. Without ``apply`` nothing is written (dry run). Returns ``(new, skipped)`` lists of
    dates. The loader holds ``ledger.maintain_global_rate`` at the group parent and is a person; the load is logged.
    """
    group = _non_publication_writer(user, group)
    parsed, seen = [], set()
    for index, raw in enumerate(rows, start=1):
        on, name_en, name_ar, source = _seed_row(raw, index)
        if on in seen:
            raise error(f"Row {index}: {on} appears twice in the file.", f"السطر {index}: التاريخ {on} مكرر في الملف.",
                        code="seed_row_invalid")
        seen.add(on)
        parsed.append((on, name_en, name_ar, source))
    with transaction.atomic():
        existing = set(NonPublicationDay.objects.select_for_update().filter(date__in=seen)
                       .values_list("date", flat=True))
        new = [row for row in parsed if row[0] not in existing]
        if apply and new:
            NonPublicationDay.objects.bulk_create([
                NonPublicationDay(date=on, name_en=en, name_ar=ar, source=source, created_by=user)
                for on, en, ar, source in new
            ])
            access_log(AccessLog.Event.PERMISSION_GRANTED, actor=user, entity=group,
                       permission="ledger.maintain_global_rate",
                       details={"action": "load_non_publication_days", "created": len(new),
                                "skipped": len(existing), "first": str(new[0][0]), "last": str(new[-1][0])})
    return [row[0] for row in new], sorted(existing)


@transaction.atomic
def verify_non_publication_day(day, *, user, group=None):
    """H1: a second person confirms a proposed non-publication day against the SAMA / official announcement.

    Needs ``ledger.maintain_global_rate`` at the group parent; never the person who loaded the row
    (``self_approval``, logged). Logged. Since I2 the days are information only: ``get_sama_rate`` uses the
    latest earlier SAMA row across any gap.
    """
    group = _non_publication_writer(user, group)
    found = NonPublicationDay.objects.select_for_update().get(pk=day.pk)
    if found.verified_at is not None:
        raise error(f"{found.date} is already verified.", f"تم التحقق من اليوم {found.date} مسبقًا.",
                    code="already_verified")
    if found.created_by_id == user.pk:
        access.deny(user, group, "ledger.maintain_global_rate", "self_approval")
    found.verified_by, found.verified_at = user, timezone.now()
    found.save(update_fields=["verified_by", "verified_at"])
    access_log(AccessLog.Event.PERMISSION_GRANTED, actor=user, entity=group, permission="ledger.maintain_global_rate",
               details={"action": "verify_non_publication_day", "day": str(found.date), "row": found.pk})
    return found


def _group_parent(group):
    """The top-level group parent entity global rates are checked against (access.md R6, ledger-fx R28)."""
    if group is None:
        parents = list(Entity.objects.filter(consolidation=Entity.Consolidation.PARENT, parent__isnull=True)[:2])
        if len(parents) != 1:
            raise error(
                "Pass the group parent entity (group=...) for a group-wide rate.",
                "حدّد الكيان الأم للمجموعة (group=...) لسعر المجموعة.",
            )
        group = parents[0]
    if group.consolidation != Entity.Consolidation.PARENT or group.parent_id is not None:
        # Global rates are used by every entity: only the top-level group parent's treasury writes them (R28).
        raise error(f"{group.code} is not the top-level group parent entity.",
                    f"{group.code} ليس الكيان الأم الأعلى للمجموعة.")
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
    if source == ExchangeRate.Source.SAMA:  # R12 amendment: global rows, with where the rate was taken from
        if entity is not None:
            raise error("SAMA rates are group-wide; enter them without an entity.",
                        "أسعار البنك المركزي السعودي للمجموعة كلها؛ أدخلها دون كيان.", code="sama_rate_global")
        if not (source_reference or "").strip():
            raise error("A SAMA rate needs its source reference (the SAMA publication it was taken from).",
                        "يحتاج سعر البنك المركزي السعودي إلى مرجع مصدره (نشرة البنك المركزي المأخوذ منها).",
                        code="sama_reference_required")
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
    are not built: their sources and precedence are open (OQ-2). ``source="sama"``: a CSV of
    SAMA's published rates (R12 amendment), group-wide only, each row with its ``source_reference``.
    """
    if source not in (ExchangeRate.Source.FILE, ExchangeRate.Source.SAMA):
        raise error(
            f"Import from {source!r} is not enabled; only CSV files can be imported (OQ-2).",
            f"الاستيراد من {source!r} غير مفعّل؛ يمكن استيراد ملفات CSV فقط (OQ-2).",
            code="source_not_enabled",
        )
    if source == ExchangeRate.Source.SAMA and entity is not None:  # R12 amendment: SAMA rows are global only
        raise error("SAMA rates are group-wide; import them without an entity.",
                    "أسعار البنك المركزي السعودي للمجموعة كلها؛ استوردها دون كيان.", code="sama_rate_global")
    _authorise_rate_write(user, entity, group)
    if isinstance(file, (str, Path)):
        with open(file, encoding="utf-8", newline="") as fh:
            text = fh.read()
        reference = Path(file).name
    else:
        text = file.read()
        reference = getattr(file, "name", "") or "upload"
    return _import_rows(text, user=user, entity=entity, reference=str(reference)[:200], source=source)


@transaction.atomic
def _import_rows(text, *, user, entity, reference, source=ExchangeRate.Source.FILE):
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
            entity=entity, date=on, from_currency_id=f, to_currency_id=t, rate_type=rate_type, source=source,
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
                source=source, source_reference=(
                    (row.get("source_reference") or "").strip() if source == ExchangeRate.Source.SAMA
                    else (row.get("source_reference") or reference))[:200],
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
    lines = list(entry.lines.all())
    if any(l.line_kind in (Kind_.SETTLEMENT, Kind_.REALISED_FX) for l in lines):
        # Re-rating the counter line alone would leave the realised FX line wrong; build it again instead.
        raise error(
            "A settlement draft is not re-rated: discard it and build its lines again with fx_settlement_lines for the new date.",
            "لا يُعاد تسعير مسودة تسوية: احذفها وأعد بناء بنودها بخدمة بنود التسوية للتاريخ الجديد.",
            code="rerate_settlement",
        )
    functional = entry.entity.base_currency_id
    for line in lines:
        line._service_write = True  # modified_by / modified_at are set below
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
        rounding._service_write = True
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


def _settlement_for_document(item, txn_amount, description=""):
    """(line dict, functional amount) settling ``txn_amount`` of the open item ``item`` at its carrying rate (R18).

    The final settlement clears exactly what is left. No counter line and no rights check: only for the
    ``build_lines`` hook of a registered document, which runs inside create_entry(post=True) once the
    posting is authorised (so it does not depend on the rights of whoever approved the document); the
    settlement is checked again at posting (_settlement_problems). Called anywhere else, or for an item of
    another entity than the document being posted, it is refused: it reads open items without a rights check.
    """
    building = _building_document.get()
    if building is None:
        raise error("A settlement is priced only inside a document's posting (its build_lines).",
                    "تُسعَّر التسوية داخل ترحيل المستند فقط (بناء بنوده).", code="settlement_outside_posting")
    if item.entry.entity_id != building[0]:
        raise error("The line to settle belongs to another entity.", "البند المراد تسويته يخص كيانًا آخر.",
                    code="settlement_other_entity")
    if item.entry.status != JournalEntry.Status.POSTED or item.line_kind != Kind_.NORMAL:
        raise error("Only a posted normal line can be settled.", "لا يُسوّى إلا بند عادي مرحّل.")
    amount = fx.to_decimal(txn_amount, "transaction amount")
    open_txn, open_func = _open_item(item)
    if amount <= ZERO or amount > open_txn:
        raise error(f"Settle between 0 and the open {open_txn} {item.currency_id}.",
                    f"المبلغ يجب أن يكون بين 0 والمفتوح {open_txn}.")
    settled = open_func if amount == open_txn else fx.convert(amount, item.fx_rate)
    side = "credit" if item.debit > 0 else "debit"
    spec = {"account": item.account, "currency": item.currency_id, f"txn_{side}": amount, "settles_line": item,
            "counterparty": item.counterparty_entity, "description": description}
    return spec, settled


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
    legs = _exchange_legs(entity, on, from_acc, f_cur.code, from_amount, to_acc, t_cur.code, to_amount, description)
    return create_entry(
        entity, on, description or f"Currency exchange {f_cur.code}->{t_cur.code} / صرف عملة {f_cur.code}->{t_cur.code}",
        legs, user=user,
    )


def _exchange_legs(entity, on, from_account, from_currency, from_amount, to_account, to_currency, to_amount,
                   description=""):
    """R20 pricing of a spot currency exchange: the line dicts ``[to_leg, from_leg]`` for create_entry. Writes nothing.

    The foreign leg carries the actual bank rate (functional paid / foreign received, ``rate_source=manual``,
    reason ACTUAL_BANK_RATE), so no FX gain or loss arises; when neither leg is functional the sold leg is at
    the day's table rate and the bought leg at the implied rate. No rights check: used by
    :func:`record_currency_exchange` after its check, and by a registered document's ``build_lines`` (banking
    own transfers, banking R31) inside an authorised posting, where the ledger checks the manual-rate right of
    the entry's creator (R16).
    """
    functional = entity.base_currency_id
    f_cur, t_cur = _currency(from_currency), _currency(to_currency)
    if f_cur.code == t_cur.code:
        raise error("An exchange is between two different currencies.", "الصرف يكون بين عملتين مختلفتين.")
    f_amount, t_amount = fx.to_decimal(from_amount, "from amount"), fx.to_decimal(to_amount, "to amount")
    if f_amount <= ZERO or t_amount <= ZERO:
        raise error("Both amounts must be above zero.", "يجب أن يكون المبلغان أكبر من صفر.")
    fx.check_txn_decimals(f_amount, f_cur)
    fx.check_txn_decimals(t_amount, t_cur)
    to_leg = {"account": to_account, "currency": t_cur.code, "txn_debit": t_amount, "description": description}
    from_leg = {"account": from_account, "currency": f_cur.code, "txn_credit": f_amount, "description": description}
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
    return [to_leg, from_leg]


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
    if RevaluationRun.objects.filter(entity=entity, period=period).exclude(
            status__in=[RevaluationRun.Status.DISCARDED, RevaluationRun.Status.REVERSED]).exists():
        raise error(
            f"{entity.code} {period.name} is already revalued.", f"تمت إعادة تقييم {period.name} للكيان {entity.code} مسبقًا.",
            code="already_revalued",
        )
    _find_open_period(entity, period.end_date)
    _find_open_period(entity, period.end_date + timedelta(days=1))
    lines = _revaluation_lines(entity, period)
    run = RevaluationRun(entity=entity, period=period, rate_date=period.end_date, run_by=user)
    if not lines:
        run.save()
        return run
    entry = JournalEntry.objects.create(
        entity=entity, date=period.end_date, created_by=user, reference=f"FXREV-{period.name}",
        description=f"FX revaluation {period.name} / إعادة تقييم العملات {period.name}",
    )
    for line in lines:
        line.entry = entry
        line._service_write = True
        line.save()
    run.entry = entry
    run.save()
    problems = validate_entry(entry)
    if problems:
        raise LedgerError(problems)
    return run


def _revaluation_lines(entity, period):
    """R21: unsaved revaluation lines for ``period`` from the booked balances and closing rates now.

    One ``revaluation`` line per (account, currency, counterparty) with a
    difference, plus the 4710 gain / 5760 loss lines. A missing closing rate
    raises (R22).
    """
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
    if not lines:
        return lines
    for code, side, amount in ((FX_GAIN_UNREALISED, "credit", gains), (FX_LOSS_UNREALISED, "debit", losses)):
        if amount:
            line = JournalLine(
                account=Account.objects.get(entity=entity, code=code), currency_id=functional, txn_amount=amount,
                fx_rate=ONE, rate_source=Src.FUNCTIONAL, line_kind=Kind_.NORMAL,
                description=f"Unrealised FX {period.name} / فروقات عملة غير محققة {period.name}",
            )
            _set_side(line, side, amount)
            lines.append(line)
    return lines


def _revaluation_key(line):
    return (
        line.account_id, line.currency_id, line.counterparty_entity_id, line.debit, line.credit,
        line.exchange_rate_id, line.line_kind,
    )


def post_revaluation(run, grant):
    """Post a revaluation draft and, in the same transaction, its automatic reversal (R23, R44).

    ``grant`` is ``require(approver, entity, "ledger.post_entry", document=run.entry)``;
    the person who ran the revaluation cannot approve it (access.md R14).
    Only a ``draft`` run posts. The differences and closing rates are
    computed again; if they no longer match the draft (a backdated posting,
    a new closing rate), nothing posts and the revaluation is re-run.
    """
    run = RevaluationRun.objects.select_related("entity", "period", "entry").get(pk=run.pk)
    if getattr(grant, "permission", None) != "ledger.post_entry":
        access.deny(getattr(grant, "user", None), run.entity, getattr(grant, "permission", ""), "grant_wrong_permission")
    if run.status != RevaluationRun.Status.DRAFT:
        raise error(f"This revaluation is {run.status}; only a draft run is posted.",
                    f"حالة إعادة التقييم هذه {run.get_status_display()}؛ تُرحَّل المسودة فقط.", code="run_not_draft")
    if run.entry is None:
        raise error("This revaluation found no differences; nothing to post.", "لم تظهر فروقات في إعادة التقييم؛ لا شيء للترحيل.")
    if getattr(grant, "self_approval", False):  # AT-31, R14: the preparer never posts the run, not even the top
        access.deny(grant.user, run.entity, grant.permission, "self_approval")  # authority's logged R79 grant
    user = _authorise_draft_post(run.entry, grant, None)
    with transaction.atomic():
        access.use_grant(grant, target=run.entry)
        return _post_revaluation(run, user)


@transaction.atomic
def _post_revaluation(run, user):
    run = RevaluationRun.objects.select_for_update(of=("self",)).select_related("entity", "period").get(pk=run.pk)
    if run.status != RevaluationRun.Status.DRAFT or run.reversal_id is not None:
        raise error("This revaluation is already posted or discarded.", "إعادة التقييم هذه مرحّلة أو ملغاة مسبقًا.",
                    code="run_not_draft")
    next_start = run.period.end_date + timedelta(days=1)
    # Every period up to the next one: no posting can change the revalued balances until this commits.
    list(FiscalPeriod.objects.select_for_update(of=("self",)).filter(
        entity=run.entity, start_date__lte=next_start).order_by("start_date"))
    _find_open_period(run.entity, next_start, lock=True)
    expected = sorted(map(_revaluation_key, _revaluation_lines(run.entity, run.period)), key=repr)
    drafted = sorted(map(_revaluation_key, JournalLine.objects.filter(entry_id=run.entry_id)), key=repr)
    if expected != drafted:
        raise error(
            "Balances or closing rates changed since this revaluation was prepared; discard it and re-run the revaluation.",
            "تغيرت الأرصدة أو أسعار الإقفال منذ إعداد إعادة التقييم هذه؛ ألغها وأعد تشغيل إعادة التقييم.",
            code="revaluation_stale",
        )
    entry = _post(run.entry, user)
    reversal = _reverse(
        entry, user, next_start, "Automatic reversal of FX revaluation / عكس تلقائي لإعادة تقييم العملات",
    )
    run.reversal = reversal
    run.status = RevaluationRun.Status.POSTED
    run.posted_by, run.posted_at = user, timezone.now()
    run._allow_transition(RevaluationRun.Status.DRAFT, RevaluationRun.Status.POSTED)
    run.save(update_fields=["reversal", "status", "posted_by", "posted_at"])
    return run


@transaction.atomic
def discard_revaluation(run, *, user):
    """Discard a draft revaluation run so the period can be revalued again (R45, OQ-18 default).

    The run is kept (``discarded``, with who and when) for audit; its draft
    entry is deleted, which rule 1 allows because it was never posted. A
    posted revaluation cannot be discarded (R24, OQ-9 default): correct it
    with a manual journal before closing.
    """
    run = RevaluationRun.objects.select_for_update(of=("self",)).select_related("entity", "entry").get(pk=run.pk)
    access.require(user, run.entity, "ledger.revalue")
    if run.status == RevaluationRun.Status.POSTED or (run.entry is not None and run.entry.status != JournalEntry.Status.DRAFT):
        raise error(
            "A posted revaluation cannot be discarded; correct it with a manual journal (OQ-9).",
            "لا يمكن إلغاء إعادة تقييم مرحّلة؛ صحّحها بقيد يدوي (OQ-9).",
        )
    if run.status == RevaluationRun.Status.DISCARDED:
        raise error("This revaluation is already discarded.", "إعادة التقييم هذه ملغاة مسبقًا.", code="run_not_draft")
    entry = run.entry
    run.entry = None
    run.status = RevaluationRun.Status.DISCARDED
    run.discarded_by, run.discarded_at = user, timezone.now()
    run._allow_transition(RevaluationRun.Status.DRAFT, RevaluationRun.Status.DISCARDED)
    run.save(update_fields=["entry", "status", "discarded_by", "discarded_at"])
    if entry is not None:
        entry.delete()
    return run


def reverse_revaluation(run, grant, *, reason):
    """Reverse a posted revaluation run so its period can be revalued again (ledger-fx OQ-9 team default, invoicing
    R97 review 2026-10-22: a wrong revaluation of a control account is not corrected by a manual journal).

    ``grant`` is ``require(user, entity, "ledger.reverse_entry", document=run.entry)``. The run's period and the
    next one must be open, and no later run of the entity may be draft or posted (it was computed on these
    balances). Two correction entries undo the run: one dated the period end mirroring the revaluation entry, one
    dated the first day of the next period mirroring its automatic reversal. The run becomes ``reversed`` (kept for
    audit, with who, when and why) and no longer counts toward one run per period, so ``revalue_period`` can run
    again at the right rate and ``post_revaluation`` posts it.
    """
    run = RevaluationRun.objects.select_related("entity", "period", "entry").get(pk=run.pk)
    if getattr(grant, "permission", None) != "ledger.reverse_entry":
        access.deny(getattr(grant, "user", None), run.entity, getattr(grant, "permission", ""),
                    "grant_wrong_permission")
    if run.status != RevaluationRun.Status.POSTED:
        raise error(f"This revaluation is {run.status}; only a posted run is reversed.",
                    f"حالة إعادة التقييم هذه {run.get_status_display()}؛ يُعكس التشغيل المرحّل فقط.", code="run_not_posted")
    if not (reason or "").strip():
        raise error("A reason is required.", "السبب مطلوب.", code="reason_required")
    user = access.verify_grant(grant, run.entity, {"ledger.reverse_entry"}, document=run.entry, use=False)
    if kind_of(user) != Kind.HUMAN:  # R26: revaluations are run, posted and reversed by people only
        access.deny(user, run.entity, "ledger.reverse_entry",
                    "agent_forbidden" if kind_of(user) == Kind.AGENT else "system_forbidden")
    with transaction.atomic():
        access.use_grant(grant, target=run.entry)
        return _reverse_revaluation(run, user, reason.strip()[:500])


def _mirror_lines(source, target):
    for line in JournalLine.objects.filter(entry=source).order_by("pk"):
        copy = JournalLine(
            entry=target, account_id=line.account_id, description=line.description, debit=line.credit,
            credit=line.debit, counterparty_entity_id=line.counterparty_entity_id, currency_id=line.currency_id,
            txn_amount=line.txn_amount, fx_rate=line.fx_rate, rate_source=line.rate_source,
            exchange_rate_id=line.exchange_rate_id, line_kind=line.line_kind,
        )
        copy._service_write = True
        copy.save()


@transaction.atomic
def _reverse_revaluation(run, user, reason):
    run = RevaluationRun.objects.select_for_update(of=("self",)).select_related("entity", "period").get(pk=run.pk)
    if run.status != RevaluationRun.Status.POSTED:
        raise error("This revaluation is no longer posted.", "إعادة التقييم هذه لم تعد مرحّلة.", code="run_not_posted")
    entity, period = run.entity, run.period
    next_start = period.end_date + timedelta(days=1)
    list(FiscalPeriod.objects.select_for_update(of=("self",)).filter(
        entity=entity, start_date__lte=next_start).order_by("start_date"))
    _find_open_period(entity, period.end_date, lock=True)
    _find_open_period(entity, next_start, lock=True)
    later = RevaluationRun.objects.filter(
        entity=entity, period__start_date__gt=period.end_date,
        status__in=[RevaluationRun.Status.DRAFT, RevaluationRun.Status.POSTED])
    if later.exists():
        raise error("A later revaluation was run on these balances; discard or reverse it first.",
                    "أُجريت إعادة تقييم لاحقة على هذه الأرصدة؛ ألغها أو اعكسها أولًا.", code="later_revaluation")
    label = f"Correction of FX revaluation {period.name} / تصحيح إعادة تقييم العملات {period.name}"
    corrections = []
    for source, on in ((run.entry_id, period.end_date), (run.reversal_id, next_start)):
        entry = JournalEntry.objects.create(entity=entity, date=on, created_by=user,
                                            reference=f"FXREV-{period.name}-X", description=f"{label}: {reason}"[:500])
        _mirror_lines(source, entry)
        corrections.append(entry)
    run.correction_entry, run.correction_reversal = corrections
    run.status = RevaluationRun.Status.REVERSED
    run.reversed_by, run.reversed_at, run.reversal_reason = user, timezone.now(), reason
    run._allow_transition(RevaluationRun.Status.POSTED, RevaluationRun.Status.REVERSED)
    run.save(update_fields=["correction_entry", "correction_reversal", "status", "reversed_by", "reversed_at",
                            "reversal_reason"])
    for entry in corrections:  # linked to the run first, so they are recognised as revaluation entries
        _post(entry, user)
    return RevaluationRun.objects.select_related("correction_entry", "correction_reversal").get(pk=run.pk)


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


# --- read-only lists for the web UI (web.md R3, R37, R38; slice 1) ------------------------------------------

def journal_entries(entity, *, user, status=None, source=None, date_from=None, date_to=None, number=None):
    """Journal entries of ``entity`` for ``user`` (``ledger.view`` there; access R2), newest first.

    A user without ``ledger.view`` in ``entity`` is refused (AccessDenied, logged); the rows are also passed through
    :func:`core.access.scope`, so nothing of another entity can appear. Filters are the enumerated values and
    dates the list offers (web.md R23). Each row carries ``debit_total`` (functional currency) and ``lines_total``
    computed in the same query, so a page costs the same whatever its length (R37).
    """
    access.require(user, entity, "ledger.view")
    qs = access.scope(JournalEntry.objects.all(), user, "ledger.view").filter(entity=entity)
    if status:
        qs = qs.filter(status=status)
    if source:
        qs = qs.filter(source=source)
    if date_from:
        qs = qs.filter(date__gte=date_from)
    if date_to:
        qs = qs.filter(date__lte=date_to)
    if number:
        qs = qs.filter(number__icontains=number)
    return (
        qs.select_related("entity", "entity__base_currency", "period")
        .annotate(debit_total=Sum("lines__debit"), lines_total=models.Count("lines"))
        .order_by("-date", "-id")
    )


def journal_entry(entity, pk, *, user):
    """One journal entry of ``entity`` with its lines, for ``user`` (``ledger.view``).

    An entry of another entity, or one the user may not see, is "not found" (Http404), never 403, so its existence
    is not revealed (access R2, web.md R3, R4).
    """
    access.require(user, entity, "ledger.view")
    lines = JournalLine.objects.select_related("account", "currency", "counterparty_entity").order_by("id")
    qs = (
        JournalEntry.objects.filter(entity=entity)
        .select_related("entity", "entity__base_currency", "period", "reversal_of", "reversed_by",
                        "created_by__access_profile", "posted_by__access_profile")
        .prefetch_related(models.Prefetch("lines", queryset=lines))
    )
    return access.get_scoped_or_404(qs, user, "ledger.view", pk=pk)


def entry_counts(entity, *, user):
    """``{status: count}`` of ``entity``'s journal entries, for the dashboard card (``ledger.view``; web.md R38)."""
    access.require(user, entity, "ledger.view")
    rows = (
        access.scope(JournalEntry.objects.all(), user, "ledger.view").filter(entity=entity)
        .values("status").annotate(n=models.Count("id")).order_by()
    )
    counts = {s: 0 for s in JournalEntry.Status.values}
    counts.update({r["status"]: r["n"] for r in rows})
    return counts
