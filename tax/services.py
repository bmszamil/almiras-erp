"""VAT returns (docs/specs/tax-returns.md, VAT slice): the only way to prepare, review, approve, file, correct
and pay a VAT return.

Every public function checks rights through ``core.access`` (scan: core/test_access_scan.py). Postings go through
``ledger.services.create_entry(post=True, grant=..., document=...)`` and ``reverse_entry`` only, as registered
documents (``tax.apps``). Money is ``Decimal``, SAR, 2 dp, ROUND_HALF_UP. Messages are "English / العربية".

Box numbers and labels follow the spec's default mapping; every box is **[verify]** against the current ZATCA VAT
return form (OQ-2) and returns say "unverified" until a definition is verified (R6). Nothing here calls ZATCA: the
return is exported as a worksheet and keyed into the ZATCA portal by the accountant (§6, OQ-29).
"""
import calendar
import csv
import hashlib
import io
import json
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from django.utils.module_loading import import_string

from core import access
from core.access.models import AccessLog, Kind
from core.access import services as access_services
from core.access.services import kind_of
from core.models import Entity
from ledger import restricted
from ledger import services as ledger
from ledger.models import Account, FiscalPeriod, JournalEntry, JournalLine, LedgerError

from . import reference
from .models import (
    FILED_STATUSES,
    BLOCKING_EXCEPTIONS,
    LOCKED_STATUSES,
    Column,
    DocStatus,
    ExceptionKind,
    Frequency,
    ImportVatRecord,
    ReturnStatus,
    StatutoryTaxRate,
    TaxAlert,
    TaxError,
    TaxPayment,
    TaxRefund,
    TaxSettings,
    VatBoxDefinition,
    VatFrequencyChange,
    VatCorrection,
    VatExceptionAck,
    VatManualAdjustment,
    VatReturn,
    VatReturnBox,
    VatReturnSchedule,
    VatReturnSourceLine,
    error,
)

ZERO = Decimal("0")
CENT = Decimal("0.01")
ONE = Decimal("1")
SAR = "SAR"

VAT_OUTPUT, VAT_INPUT, BANK = "2200", "1310", "1100"
SALES_TYPE, PURCHASE_TYPE, IMPORT_TYPE = "invoicing.salesdocument", "invoicing.purchasedocument", "tax.importvatrecord"
SOURCE_TYPES = (SALES_TYPE, PURCHASE_TYPE, IMPORT_TYPE)
# T1, T2, T4: the return's own settlement entries; never part of the tie-out (R15). T3 (import VAT) is a source.
SETTLEMENT_TYPES = ("tax.vatreturn", "tax.taxpayment", "tax.taxrefund")
# R23 / invoicing R30, R103: supply kinds that are reverse charged when bought from a non-resident. invoicing builds
# ``services`` today; ``goods_in_ksa`` is accepted for when invoicing R103 is built (OQ, [CC-verify]).
REVERSE_CHARGE_KINDS = frozenset({"services", "goods_in_ksa"})
CITIZEN_CODES = frozenset({"VATEX-SA-HEA", "VATEX-SA-EDU"})  # box 2 (R19)
EXPORT_CODES = frozenset({"VATEX-SA-32", "VATEX-SA-33"})  # box 4 (R19)
QUARTERLY_LIMIT_SAR = Decimal("40000000.00")  # R5 [CC-verify] PSG §13.2 p.52
UNVERIFIED = "unverified / غير متحقق منه"
OUTPUT_BOXES, INPUT_BOXES = (1, 2, 3, 4, 5), (7, 8, 9, 10, 11)


def _today():
    """Today in Asia/Riyadh; a function so tests can move the clock."""
    return timezone.localdate()


def _now():
    return access_services.now()


def _q(amount):
    return Decimal(amount).quantize(CENT, rounding=ROUND_HALF_UP)


def _sar(amount, rate):
    return _q(Decimal(amount) * (rate if rate is not None else ONE))


def _money(value, what_en, what_ar, *, positive=False, allow_zero=False):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise error(f"{what_en} is not an amount.", f"{what_ar} ليس مبلغًا.", code="amount_invalid") from None
    if not amount.is_finite() or amount != amount.quantize(CENT):
        raise error(f"{what_en} has more than 2 decimals (SAR).", f"{what_ar} بأكثر من خانتين عشريتين.",
                    code="amount_decimals")
    if positive and (amount < ZERO or (amount == ZERO and not allow_zero)):
        raise error(f"{what_en} must be above zero.", f"يجب أن يكون {what_ar} أكبر من صفر.", code="amount_not_positive")
    return amount.quantize(CENT)


def _human(user, entity, permission):
    """R14: agents compute drafts and read; every other step needs a person (logged refusal)."""
    if kind_of(user) == Kind.AGENT:
        access.deny(user, entity, permission, "agent_forbidden")


def _preparers(ret):
    """R8 (review item 10): whoever created, recomputed or submitted the return, chose the refund, or wrote one of
    its manual adjustments prepared it, so cannot review or approve it."""
    people = {ret.created_by_id, ret.modified_by_id, ret.submitted_by_id, ret.refund_requested_by_id}
    if ret.pk:
        people |= set(VatManualAdjustment.objects.filter(vat_return_id=ret.pk).values_list("created_by_id", flat=True))
    return people - {None}


def _add_months(day, months):
    """``day`` plus ``months`` calendar months; a month-end date stays a month end, other days are clamped."""
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    last = calendar.monthrange(year, month + 1)[1]
    if day.day == calendar.monthrange(day.year, day.month)[1]:
        return date_type(year, month + 1, last)
    return date_type(year, month + 1, min(day.day, last))


# --- settings ----------------------------------------------------------------------------------------

def _settings(entity):
    found = TaxSettings.objects.filter(entity=entity).first()
    if found is None:
        found = TaxSettings(entity=entity)
        found._service_write = True
        found.save()
    return found


def tax_settings(entity, *, user):
    """The entity's TaxSettings (defaults created on first use). Needs ``tax.view``."""
    access.require(user, entity, "tax.view")
    return _settings(entity)


SETTINGS_FIELDS = ("vat_quarter_start_month", "alert_days", "vat_correction_threshold_sar", "vat_golive_date")


def change_tax_settings(entity, *, user, **changes):
    """Change the VAT settings (3.1): ``tax.change_settings`` (approver, A), logged before / after (AccessLog).

    ``vat_golive_date`` (owner decision G4): VAT returns in the ERP start on that date; it must be the start of a
    return period, and no ERP return or alert may exist before it. Earlier periods are filed outside the ERP and
    only imported (R12, review item 3)."""
    unknown = set(changes) - set(SETTINGS_FIELDS)
    if unknown:
        raise error(f"Not a tax setting: {', '.join(sorted(unknown))}.", "ليس من إعدادات الضرائب.", code="unknown_setting")
    with transaction.atomic():
        current = _settings(entity)
        access.require(user, entity, "tax.change_settings", document=current)
        _human(user, entity, "tax.change_settings")
        Entity.objects.select_for_update().get(pk=entity.pk)  # the schedule lock (review item 13)
        current = TaxSettings.objects.select_for_update().get(pk=current.pk)
        before = {name: str(getattr(current, name)) for name in changes}
        if "vat_quarter_start_month" in changes:
            month = int(changes["vat_quarter_start_month"])
            if not 1 <= month <= 12:
                raise error("The quarter start month is 1 to 12.", "شهر بداية الربع من 1 إلى 12.", code="setting_invalid")
            current.vat_quarter_start_month = month
        if "alert_days" in changes:
            days = changes["alert_days"]
            if not isinstance(days, (list, tuple)) or not all(isinstance(d, int) and d >= 0 for d in days):
                raise error("Alert days are a list of whole days.", "أيام التنبيه قائمة بأعداد أيام صحيحة.",
                            code="setting_invalid")
            current.alert_days = sorted(set(days), reverse=True)
        if "vat_correction_threshold_sar" in changes:
            current.vat_correction_threshold_sar = _money(changes["vat_correction_threshold_sar"], "The threshold",
                                                          "الحد", positive=True)
        if "vat_golive_date" in changes:
            current.vat_golive_date = _check_golive(entity, current, changes["vat_golive_date"])
        current._service_write = True
        current.save()
        if "vat_golive_date" in changes or "vat_quarter_start_month" in changes:
            fresh = Entity.objects.get(pk=entity.pk)
            rows = VatReturnSchedule.objects.filter(entity=entity)
            if rows.exists() and _vat_ready(fresh):
                _ensure_schedule(fresh, max(r.period_end for r in rows))
        access_services.log(AccessLog.Event.ENTITY_SETTINGS_CHANGED, actor=user, entity=entity, permission="tax.change_settings",
                   details={"tax_settings": {"before": before,
                                             "after": {name: str(getattr(current, name)) for name in changes}}})
    return current


def _check_golive(entity, conf, value):
    """G4: a go-live date is the start of a return period; nothing of the ERP's own exists before it."""
    if value is None:
        if VatReturn.objects.filter(entity=entity, source=VatReturn.Source.IMPORT).exists():
            raise error("Imported legacy returns depend on the go-live date; it cannot be removed.",
                        "تعتمد الإقرارات المستوردة على تاريخ البدء؛ لا يمكن حذفه.", code="golive_in_use")
        return None
    if not isinstance(value, date_type):
        raise error("The go-live date is a date.", "تاريخ البدء يجب أن يكون تاريخًا.", code="setting_invalid")
    entity = Entity.objects.get(pk=entity.pk)
    _check_vat_entity(entity)
    starts = {s for s, _, _ in _walk(entity, conf, value)}
    if value not in starts:
        raise error(f"The go-live date {value} is not the start of a VAT return period of {entity.code}.",
                    f"تاريخ البدء {value} ليس بداية فترة إقرار للكيان {entity.code}.", code="golive_not_period_start")
    if VatReturn.objects.filter(entity=entity, period_start__lt=value).exclude(source=VatReturn.Source.IMPORT).exists() \
            or VatReturn.objects.filter(entity=entity, period_end__gte=value, source=VatReturn.Source.IMPORT).exists() \
            or TaxAlert.objects.filter(entity=entity, schedule__period_start__lt=value).exists():
        raise error("Returns or alerts of the ERP already exist on the other side of this go-live date.",
                    "توجد إقرارات أو تنبيهات للنظام على الجانب الآخر من تاريخ البدء هذا.", code="golive_in_use")
    return value


# --- entity checks and the return schedule (R2 to R4) -------------------------------------------------

def _check_vat_entity(entity):
    """R2: only a VAT-registered entity with a return period has VAT returns; SAR functional currency."""
    if not (entity.vat_number or "").strip():
        raise error(f"{entity.code} is not VAT-registered, so it has no VAT return (R2).",
                    f"الكيان {entity.code} غير مسجل في ضريبة القيمة المضافة، فلا إقرار ضريبي له.",
                    code="not_vat_registered")
    if not entity.vat_return_period or not entity.vat_effective_date:
        raise error(f"{entity.code} needs a VAT return period and VAT effective date (R2, R3).",
                    f"يحتاج الكيان {entity.code} إلى فترة إقرار وتاريخ سريان التسجيل الضريبي.", code="vat_setup_missing")
    if entity.base_currency_id != SAR:
        raise error(f"{entity.code} keeps its books in {entity.base_currency_id}; VAT returns are built for SAR "
                    "functional entities only (open question).",
                    f"عملة دفاتر الكيان {entity.code} هي {entity.base_currency_id}؛ تُعدّ الإقرارات للكيانات التي "
                    "عملتها الوظيفية الريال فقط.", code="vat_requires_sar_functional")


def _month_end(year, month):
    return date_type(year, month, calendar.monthrange(year, month)[1])


def _period_end(start, frequency, quarter_start_month):
    if frequency == Frequency.MONTHLY:
        return _month_end(start.year, start.month)
    offset = (start.month - quarter_start_month) % 3
    month = start.month + (2 - offset)
    year = start.year + (month - 1) // 12
    return _month_end(year, (month - 1) % 12 + 1)


def _due_date(period_end):
    """Last day of the month after the period (PSG §13.2 p.52; met, compliance C4); no weekend shift (OQ-8)."""
    nxt = period_end + timedelta(days=1)
    return _month_end(nxt.year, nxt.month)


def _vat_ready(entity):
    try:
        _check_vat_entity(entity)
    except TaxError:
        return False
    return True


def _history(entity):
    return list(VatFrequencyChange.objects.filter(entity=entity).order_by("changed_on", "pk"))


def _frequency_at(entity, start, history):
    """R4 (review item 6): the frequency that applies to a period starting on ``start``: the latest change made
    before that day, else the frequency before the first change (else the entity's current one)."""
    frequency = history[0].previous_frequency if history else entity.vat_return_period
    for change in history:
        if change.changed_on < start:
            frequency = change.frequency
    return frequency


def _walk(entity, conf, until, fixed=None):
    """Every return period from ``vat_effective_date`` up to the one containing ``until`` (and every ``fixed`` row,
    i.e. a period that has a return and never changes): (start, end, frequency). Deterministic: the same result
    whenever it runs (review item 6)."""
    history = _history(entity)
    fixed = fixed or {}
    last_fixed = max((r.period_end for r in fixed.values()), default=None)
    out, start = [], entity.vat_effective_date
    while start <= until or (last_fixed is not None and start <= last_fixed):
        row = fixed.get(start)
        if row is not None:
            end, frequency = row.period_end, row.frequency
        else:
            frequency = _frequency_at(entity, start, history)
            end = _period_end(start, frequency, conf.vat_quarter_start_month)
            later = [s for s in fixed if s > start]
            if later and end >= min(later):
                raise error("A frequency change conflicts with a period that already has a return.",
                            "يتعارض تغيير الدورية مع فترة لها إقرار بالفعل.", code="schedule_conflict")
        out.append((start, end, frequency))
        start = end + timedelta(days=1)
    if set(fixed) - {s for s, _, _ in out}:
        raise error("A period that has a return does not fit the schedule.", "فترة لها إقرار لا تتوافق مع الجدول.",
                    code="schedule_conflict")
    return out


def _ensure_schedule(entity, until, *, agent=None):
    """Generate the entity's periods up to the one containing ``until`` (R3, R4, G4).

    Periods start at the VAT go-live date (``TaxSettings.vat_golive_date``, else the VAT effective date). Each
    period takes the frequency in force on its start (dated history, R4); a period that has a return never
    changes. Rows that no longer fit are regenerated. Runs under a lock on the entity row, so two generations never
    overlap (review item 13). ``agent``: an AI agent may only use existing rows (review item 12)."""
    entity = Entity.objects.select_for_update().get(pk=entity.pk)
    _check_vat_entity(entity)
    conf = _settings(entity)
    rows = list(VatReturnSchedule.objects.filter(entity=entity).order_by("period_start"))
    if rows:
        until = max(until, rows[-1].period_end)
    fixed = {r.period_start: r for r in rows if r.returns.exists()}
    first = conf.vat_golive_date or entity.vat_effective_date
    expected = [p for p in _walk(entity, conf, until, fixed) if p[0] >= first or p[0] in fixed]
    wanted = set(expected)
    stale = [r for r in rows if (r.period_start, r.period_end, r.frequency) not in wanted]
    have = {(r.period_start, r.period_end, r.frequency) for r in rows}
    missing = [p for p in expected if p not in have]
    if agent is not None and (stale or missing):
        access.deny(agent, entity, "tax.prepare_return", "agent_forbidden")
    for row in stale:
        if row.returns.exists() or TaxAlert.objects.filter(schedule=row).exists():
            raise error("A VAT period with a return or alerts would change; the schedule cannot be regenerated.",
                        "ستتغير فترة لها إقرار أو تنبيهات؛ لا يمكن إعادة إنشاء الجدول.", code="schedule_has_return")
        row._service_write = True
        row.delete()
    for start, end, frequency in missing:
        row = VatReturnSchedule(entity=entity, period_start=start, period_end=end, due_date=_due_date(end),
                                frequency=frequency)
        row._service_write = True
        row.save()
    return list(VatReturnSchedule.objects.filter(entity=entity).order_by("period_start"))


def _on_entity_settings_changed(sender, *, entity, user, before, after, reason="", **kwargs):
    """Receiver of ``core.signals.entity_settings_changed`` (review item 6): a VAT frequency change is dated
    (``VatFrequencyChange``) and the schedule regenerated in the same transaction (R4)."""
    if "vat_return_period" not in after or not before.get("vat_return_period"):
        return
    change = VatFrequencyChange(entity=entity, previous_frequency=before["vat_return_period"],
                                frequency=after["vat_return_period"], changed_on=_today(),
                                reason=str(reason or "")[:500], recorded_by=user, recorded_at=_now())
    change._service_write = True
    change.save()
    fresh = Entity.objects.get(pk=entity.pk)
    rows = VatReturnSchedule.objects.filter(entity=fresh)
    if rows.exists() and _vat_ready(fresh):
        _ensure_schedule(fresh, max(r.period_end for r in rows))


def generate_schedule(entity, *, user, until):
    """Generate the VAT return periods of ``entity`` up to ``until`` (R3, R4). Needs ``tax.prepare_return`` and a
    person: agents never regenerate or delete periods (review item 12)."""
    access.require(user, entity, "tax.prepare_return")
    _human(user, entity, "tax.prepare_return")
    with transaction.atomic():
        return _ensure_schedule(entity, until)


def schedule(entity, *, user):
    """The entity's VAT return periods with their due dates. Needs ``tax.view``."""
    access.require(user, entity, "tax.view")
    return list(VatReturnSchedule.objects.filter(entity=entity).order_by("period_start"))


# --- reading returns (R1, R11) ------------------------------------------------------------------------

def vat_returns(user, entity=None):
    """Returns the user may see (``tax.view``, R1): of ``entity`` (checked and logged), else of every entity where
    the user holds ``tax.view`` (``access.scope``)."""
    if entity is not None:
        access.require(user, entity, "tax.view")
        return VatReturn.objects.select_related("entity").filter(entity=entity)
    return access.scope(VatReturn.objects.select_related("entity"), user, "tax.view")


def get_return(pk, *, user):
    """One return by id. Unknown ids and returns of entities where the user lacks ``tax.view`` give the same error
    (review item 12); the refusal for another entity is logged (AT-1)."""
    ret = VatReturn.objects.select_related("entity").filter(pk=pk).first()
    if ret is not None:
        try:
            access.require(user, ret.entity, "tax.view")
            return ret
        except access.AccessDenied:
            pass
    raise error("No VAT return with this id is available to you.", "لا يوجد إقرار ضريبي بهذا الرقم متاح لك.",
                code="return_not_found")


def drill_down(vat_return, *, user, box_no, column=None):
    """R11: the source lines of a box (and column), each with its journal entry and document, with the same rights
    as ``ledger.view`` in the return's entity."""
    ret = VatReturn.objects.select_related("entity").get(pk=vat_return.pk)
    access.require(user, ret.entity, "tax.view")
    access.require(user, ret.entity, "ledger.view")
    lines = ret.source_lines.filter(box_no=box_no).select_related("journal_entry")
    if column:
        lines = lines.filter(column=column)
    return [{"box_no": l.box_no, "column": l.column, "source_type": l.source_type, "source_id": l.source_id,
             "document_number": l.document_number, "journal_entry": l.journal_entry_id,
             "entry_number": l.journal_entry.number if l.journal_entry_id else "", "date": l.date,
             "amount_sar": l.amount_sar, "note": l.note} for l in lines]


# --- the computation (R15 to R27, R55) ------------------------------------------------------------------

@dataclass
class Computation:
    boxes: dict = field(default_factory=lambda: {n: {"amount": ZERO, "adjustment": ZERO, "vat": ZERO}
                                                 for n in range(1, 17)})
    lines: list = field(default_factory=list)
    rc_output: Decimal = ZERO
    output_vat: Decimal = ZERO
    input_vat: Decimal = ZERO
    # Review fixes: what the reviewer must see (exceptions) and what the tie-out must explain.
    flags: list = field(default_factory=list)  # exception dicts (kind, reference, number, ...)
    fallback_documents: list = field(default_factory=list)  # C10: invoices put in the period of their issue date
    shifted_in: list = field(default_factory=list)  # entries dated outside the period, reported in it
    shifted_out: list = field(default_factory=list)  # entries dated in the period, reported in another one
    rate_differences: list = field(default_factory=list)  # C12: (entry id, booked 1310 - box 7 VAT)
    output_rate_differences: list = field(default_factory=list)  # §10.5: (entry id, booked 2200 - box 1 VAT)

    def add(self, box_no, column, amount, source_type, *, source_id=None, number="", entry=None, on=None, note=""):
        amount = _q(amount)
        if amount == ZERO:
            return
        if box_no != 13:  # box 13 is a total; its lines are the reverse-charge output part only
            self.boxes[box_no][column] += amount
        self.lines.append({"box_no": box_no, "column": column, "source_type": source_type, "source_id": source_id,
                           "document_number": number or "", "journal_entry_id": getattr(entry, "pk", entry),
                           "date": on, "amount_sar": amount, "note": note[:200]})

    def flag(self, kind, reference, **details):
        if not any(f["kind"] == kind and f["reference"] == reference for f in self.flags):
            self.flags.append({"kind": kind, "reference": reference, **details})


def _box_definitions(on):
    found = {}
    for d in VatBoxDefinition.objects.filter(valid_from__lte=on).filter(Q(valid_to__isnull=True) | Q(valid_to__gte=on)) \
            .order_by("box_no", "valid_from"):
        found[d.box_no] = d
    return found


def _correction_exclusions(entity):
    """R26: correcting entries (and their reversals) of live corrections never count in boxes 1 to 13."""
    ids = set(VatCorrection.objects.filter(entity=entity).exclude(status=VatCorrection.Status.CANCELLED)
              .values_list("correcting_entries", flat=True)) - {None}
    if ids:
        ids |= set(JournalEntry.objects.filter(reversal_of_id__in=ids).values_list("pk", flat=True))
    return ids


def _filed_before(entity, original_date, note_date):
    """R20: the return of the period holding ``original_date`` was filed before ``note_date``."""
    return VatReturn.objects.filter(entity=entity, period_start__lte=original_date, period_end__gte=original_date,
                                    status__in=FILED_STATUSES, filed_on__lt=note_date).exists()


def _sales_box(line):
    category = line.vat_category
    reason = line.vat_exemption_reason.code if line.vat_exemption_reason_id else ""
    if category == "S":
        return 1
    if category == "Z":
        return 2 if reason in CITIZEN_CODES else 4 if reason in EXPORT_CODES else 3
    if category == "E":
        return 5
    return None  # O (out of scope): not reported (R19)


def _tax_point(doc):
    """C10 / G6 (PSG §11.3, TIR §4.2.3): the return period of a sale is its tax point, ``tax_point_date`` as
    invoicing computed it (invoicing R100). Notes keep their own date (X9, TIR §7.1.1). Legacy invoices, whose
    tax point migration 0026 set to the issue date without computing it (basis ``legacy``), or without one, fall
    back to the issue date and are listed for the reviewer (``tax_point_fallback``). Returns (date, known)."""
    if doc.doc_type != "invoice":
        return doc.issue_date, True
    if doc.tax_point_date and doc.tax_point_basis not in ("", "legacy"):
        return doc.tax_point_date, True
    return doc.issue_date, False


def _note_column(entity, entry, original_date, note_date, boxes):
    """R20 (TIR §7.1.1): a credit note goes to the Adjustment column when the original's return was filed before
    the note's date, else the Amount column. A reversal of a note takes the column its note took (review item 11)."""
    if entry.reversal_of_id:
        taken = (VatReturnSourceLine.objects.filter(journal_entry_id=entry.reversal_of_id, box_no__in=boxes,
                                                    column__in=[Column.AMOUNT, Column.ADJUSTMENT],
                                                    vat_return__status__in=LOCKED_STATUSES)
                 .values_list("column", flat=True).first())
        if taken:
            return taken
    if original_date is not None and _filed_before(entity, original_date, note_date):
        return Column.ADJUSTMENT
    return Column.AMOUNT


def _sales_sources(comp, entity, entry, doc, sign):
    """R18 to R20: a sales document's lines by box; SAR at the document's stored SAR rate (G7 / OQ-34: invoicing
    stores it; no fallback to the booking rate, C11)."""
    rate = ONE if doc.currency_id == SAR else doc.sar_rate
    if rate is None:
        comp.flag(ExceptionKind.SAR_RATE_MISSING, f"sales:{doc.pk}", document=doc.pk, number=doc.number,
                  output=doc.total_vat_sar, input=ZERO)
    note = doc.doc_type == "credit_note"
    s = sign * (-1 if note else 1)
    column = Column.AMOUNT
    if note and doc.original_document_id:
        column = _note_column(entity, entry, doc.original_document.issue_date, doc.issue_date, OUTPUT_BOXES)
    if rate is not None:
        nets = defaultdict(lambda: ZERO)
        for line in doc.lines.select_related("vat_exemption_reason"):
            box = _sales_box(line)
            if box is not None:
                nets[box] += line.net_amount
        for box, net in sorted(nets.items()):
            comp.add(box, column, s * _sar(net, rate), "sales_document", source_id=doc.pk, number=doc.number,
                     entry=entry, on=entry.date)
    vat = sum((Decimal(g["vat_sar"]) for g in doc.vat_breakdown if g["category"] == "S"), ZERO)
    comp.add(1, Column.VAT, s * vat, "sales_document", source_id=doc.pk, number=doc.number, entry=entry, on=entry.date)
    if doc.currency_id != SAR and rate is not None:
        # §10.5 (G7): box 1 VAT is at the SAMA tax-point rate; a document booked at another rate (invoicing R100(d))
        # leaves the difference on 2200, explained in the tie-out like C12 for bills.
        booked = _ledger_entries(entity, ids={entry.pk}).get(entry.pk, {}).get("output", ZERO)
        if booked != s * vat:
            comp.output_rate_differences.append((entry.pk, booked - s * vat))
    if not entry.reversal_of_id and not _tax_point(doc)[1]:
        comp.fallback_documents.append(doc)


def _purchase_box(resident, line):
    category = line.vat_category
    if category == "Z":
        return 10
    if category == "E":
        return 11
    if category == "S":
        if resident:
            return 7
        return 9 if line.supply_kind in REVERSE_CHARGE_KINDS else None  # non-resident goods: customs, box 8
    return None  # O: unregistered vendors (R92) and out of scope, not reported (OQ-12 default)


def _recoverable(doc, lines, resident):
    """Recoverable VAT of a posted purchase document in its currency, from the stored lines and breakdown exactly as
    invoicing booked 1310 (invoicing R29, R30, ``_purchase_amounts``)."""
    groups = defaultdict(list)
    for line in lines:
        rc_line = not resident and line.supply_kind in REVERSE_CHARGE_KINDS and line.vat_category == "S"
        if not resident and not rc_line:
            continue
        groups[(line.vat_category, str(line.vat_rate.rate) if line.vat_rate_id else "0.00")].append(line)
    vat_of = {(g["category"], g["rate"]): Decimal(g["vat"]) for g in doc.vat_breakdown}
    recoverable = ZERO
    for key, group in groups.items():
        rec = [l for l in group if l.vat_recoverable]
        if rec:
            recoverable += vat_of.get(key, ZERO) - sum((l.vat_amount for l in group if not l.vat_recoverable), ZERO)
    return recoverable


def _purchase_sources(comp, entity, entry, doc, sign):
    """R21, R23, R24: a bill or vendor credit note by box. Amounts at the booking rate; box 7 VAT of a
    foreign-currency bill = the SAR VAT on the supplier's invoice (C12, TIR §4.2.2), the booking-rate difference
    goes to the tie-out only."""
    rate = ONE if doc.currency_id == SAR else doc.fx_rate
    resident = doc.vendor.is_resident
    note = doc.doc_type == "vendor_credit_note"
    s = sign * (-1 if note else 1)
    column = Column.AMOUNT
    if note and doc.original_document_id:  # C9: the note's own (supplier) date decides
        column = _note_column(entity, entry, doc.original_document.accounting_date, doc.vendor_document_date,
                              INPUT_BOXES)
    lines = list(doc.lines.select_related("vat_rate"))
    unregistered = any(g.get("unregistered_vendor") for g in doc.vat_breakdown)
    nets = defaultdict(lambda: ZERO)
    for line in lines:
        box = None if unregistered else _purchase_box(resident, line)
        if box is not None:
            nets[box] += line.net_amount
    for box, net in sorted(nets.items()):
        comp.add(box, column, s * _sar(net, rate), "purchase_document", source_id=doc.pk, number=doc.number,
                 entry=entry, on=entry.date)
    if unregistered:
        return
    recoverable = _recoverable(doc, lines, resident)
    booked = _sar(recoverable, rate)
    reported = booked
    if resident and doc.currency_id != SAR and recoverable:
        if doc.vendor_vat_sar is not None and doc.total_vat:
            reported = _q(doc.vendor_vat_sar * recoverable / doc.total_vat)
            if reported != booked:
                comp.rate_differences.append((entry.pk, s * (booked - reported)))
        else:
            comp.flag(ExceptionKind.PURCHASE_SAR_VAT_MISSING, f"purchase:{doc.pk}", document=doc.pk,
                      number=doc.number, output=ZERO, input=booked)
    comp.add(7 if resident else 9, Column.VAT, s * reported, "purchase_document", source_id=doc.pk,
             number=doc.number, entry=entry, on=entry.date)
    if not resident and doc.reverse_charge_vat:
        rc = s * _sar(doc.reverse_charge_vat, rate)
        comp.rc_output += rc
        comp.add(13, Column.VAT, rc, "reverse_charge_output", source_id=doc.pk, number=doc.number, entry=entry,
                 on=entry.date, note="Reverse-charge output VAT / ضريبة مخرجات الاحتساب العكسي")


def _import_sources(comp, entry, rec, sign):
    """R22: box 8 amount = the import VAT base (customs value + duties + other charges, C13 [verify]); VAT = the
    recoverable VAT (attribution ``taxable``)."""
    number = rec.customs_declaration_no
    comp.add(8, Column.AMOUNT, sign * rec.vat_base_sar, "import_record", source_id=rec.pk, number=number,
             entry=entry, on=entry.date)
    if rec.attribution == ImportVatRecord.Attribution.TAXABLE:
        comp.add(8, Column.VAT, sign * rec.vat_paid_sar, "import_record", source_id=rec.pk, number=number,
                 entry=entry, on=entry.date)


def _source_document(entry, cache):
    from invoicing.models import PurchaseDocument, SalesDocument

    key = (entry.document_type, entry.document_id)
    if key not in cache:
        if entry.document_type == SALES_TYPE:
            cache[key] = SalesDocument.objects.select_related("original_document").get(pk=entry.document_id)
        elif entry.document_type == PURCHASE_TYPE:
            cache[key] = PurchaseDocument.objects.select_related("vendor", "original_document").get(pk=entry.document_id)
        else:
            cache[key] = ImportVatRecord.objects.get(pk=entry.document_id)
    return cache[key]


def _period_date(entry, doc):
    """R17 as amended (C9, C10): the date that decides the return period. A reversal: its own date. A sale: its
    tax point. A vendor credit note: the supplier note's date. Bills and import records: the journal date."""
    if entry.reversal_of_id:
        return entry.date
    if entry.document_type == SALES_TYPE:
        return _tax_point(doc)[0]
    if entry.document_type == PURCHASE_TYPE and doc.doc_type == "vendor_credit_note":
        return doc.vendor_document_date
    return entry.date


def _period_sources(entity, start, end):
    """(entries reported in the period, entries dated in it but reported in another period), each as
    (entry, document). Correcting entries of live corrections are left out (R26)."""
    from invoicing.models import PurchaseDocument, SalesDocument

    excluded = _correction_exclusions(entity)
    base = (JournalEntry.objects.filter(entity=entity, status__in=ledger.BOOKED, document_type__in=SOURCE_TYPES)
            .exclude(pk__in=excluded))
    sales = Q(issue_date__gte=start, issue_date__lte=end) | Q(tax_point_date__gte=start, tax_point_date__lte=end)
    sales_ids = SalesDocument.objects.filter(sales, entity=entity).values_list("pk", flat=True)
    note_ids = PurchaseDocument.objects.filter(entity=entity, doc_type="vendor_credit_note",
                                               vendor_document_date__gte=start, vendor_document_date__lte=end
                                               ).values_list("pk", flat=True)
    candidates = base.filter(Q(date__gte=start, date__lte=end)
                             | Q(document_type=SALES_TYPE, document_id__in=list(sales_ids), reversal_of__isnull=True)
                             | Q(document_type=PURCHASE_TYPE, document_id__in=list(note_ids),
                                 reversal_of__isnull=True))
    cache, inside, outside = {}, [], []
    for entry in candidates.order_by("date", "pk"):
        doc = _source_document(entry, cache)
        on = _period_date(entry, doc)
        if start <= on <= end:
            inside.append((entry, doc))
        elif start <= entry.date <= end:
            outside.append((entry, doc))
    return inside, outside


def _previous_period(ret):
    """The return of the period just before ``ret`` (latest approved or later, not amended), and whether that
    period is in the ERP schedule without one (a gap, review item 13)."""
    day_before = ret.period_start - timedelta(days=1)
    prev = (VatReturn.objects.filter(entity=ret.entity, period_end=day_before,
                                     status__in=[ReturnStatus.APPROVED, ReturnStatus.FILED, ReturnStatus.SETTLED])
            .order_by("-version").first())
    if prev is not None:
        return prev, False
    return None, VatReturnSchedule.objects.filter(entity=ret.entity, period_end=day_before).exists()


def _carried_credit(ret):
    """R25: box 15 = the previous period's negative box 16 when no refund was requested (OQ-18), less refunds."""
    prev, _ = _previous_period(ret)
    if prev is None or prev.net_vat_sar >= ZERO or prev.refund_requested:
        return None, ZERO
    refunded = TaxRefund.objects.filter(vat_return=prev, status=DocStatus.POSTED).aggregate(t=Sum("amount_sar"))["t"]
    return prev, max(ZERO, -prev.net_vat_sar - (refunded or ZERO))


def _art51_port():
    return import_string(getattr(settings, "TAX_ART51_PORT", "tax.art51_port.InvoicingArt51Port"))()


def _last_return_of_year(ret):
    """R55 (review item 1): the year whose 31 December falls in the return period, else None. A quarter that
    starts in November (quarter start month 2) holds 31 December of its first year."""
    for year in range(ret.period_start.year, ret.period_end.year + 1):
        if ret.period_start <= date_type(year, 12, 31) <= ret.period_end:
            return year
    return None


def _totals(comp, ret):
    b = comp.boxes
    for col in ("amount", "adjustment", "vat"):
        b[6][col] = sum((b[n][col] for n in OUTPUT_BOXES), ZERO)
        b[12][col] = sum((b[n][col] for n in INPUT_BOXES), ZERO)
    comp.output_vat = b[6]["vat"] + comp.rc_output
    comp.input_vat = b[12]["vat"]
    b[13]["vat"] = comp.output_vat - comp.input_vat
    b[16]["vat"] = b[13]["vat"] + b[14]["vat"] - b[15]["vat"]


def _compute(ret):
    """Recompute every box of ``ret`` from posted ledger and invoicing data (R15 to R27, R55). Reads only."""
    comp = Computation()
    entity = ret.entity
    if ret.version > 0:
        return _compute_amendment(ret, comp)
    inside, outside = _period_sources(entity, ret.period_start, ret.period_end)
    for entry, doc in inside:
        sign = -1 if entry.reversal_of_id else 1
        if entry.document_type == SALES_TYPE:
            _sales_sources(comp, entity, entry, doc, sign)
        elif entry.document_type == PURCHASE_TYPE:
            _purchase_sources(comp, entity, entry, doc, sign)
        else:
            _import_sources(comp, entry, doc, sign)
        if not ret.period_start <= entry.date <= ret.period_end:
            comp.shifted_in.append(entry)
    comp.shifted_out = [entry for entry, _ in outside]
    year = _last_return_of_year(ret)
    if year is not None:  # R55: the year's Art. 51 true-up in the last return of the year
        status = _art51_port().year_status(entity, year)
        for entry, amount in (status.true_up_entries if status.applies else ()):
            for column in (Column.ADJUSTMENT, Column.VAT):  # [verify] placement (OQ-2); VAT keeps box 13 whole
                comp.add(7, column, amount, "art51_true_up", source_id=entry.pk, number=entry.number, entry=entry,
                         on=entry.date, note="Art. 51 true-up / تسوية المادة 51")
    for corr in ret.assigned_corrections.exclude(status=VatCorrection.Status.CANCELLED).order_by("pk"):
        comp.add(14, Column.VAT, corr.tax_difference_sar, "correction", source_id=corr.pk,
                 number=f"COR-{corr.pk}", on=corr.discovered_on)
    prev, carried = _carried_credit(ret)
    if carried:
        comp.add(15, Column.VAT, carried, "carried_credit", source_id=prev.pk, on=prev.period_end,
                 number=f"{prev.period_start}..{prev.period_end} v{prev.version}")
    for adj in ret.manual_adjustments.order_by("pk"):
        comp.add(adj.box_no, adj.column, adj.amount_sar, "manual_adjustment", source_id=adj.pk, number=f"ADJ-{adj.pk}",
                 on=adj.created_at.date(), note=adj.reason_en or adj.reason_ar)
    _totals(comp, ret)
    return comp


def _compute_amendment(ret, comp):
    """R28: an amendment carries the figures of the return it amends plus the corrections assigned to it (box 14).
    [CC-verify] whether ZATCA's voluntary disclosure restates the boxes instead (OQ-2)."""
    base = ret.amends
    for box in base.boxes.all():
        if box.box_no in (6, 12, 13, 16):
            continue
        for column, value in (("amount", box.amount_sar), ("adjustment", box.adjustment_sar), ("vat", box.vat_sar)):
            comp.add(box.box_no, column, value, "prior_return", source_id=base.pk, on=base.period_end,
                     number=f"v{base.version}")
    comp.rc_output = base.rc_output_vat_sar
    if comp.rc_output:
        comp.add(13, Column.VAT, comp.rc_output, "prior_return", source_id=base.pk, on=base.period_end,
                 number=f"v{base.version}", note="Reverse-charge output VAT / ضريبة مخرجات الاحتساب العكسي")
    for corr in ret.assigned_corrections.exclude(status=VatCorrection.Status.CANCELLED).order_by("pk"):
        comp.add(14, Column.VAT, corr.tax_difference_sar, "correction", source_id=corr.pk, number=f"COR-{corr.pk}",
                 on=corr.discovered_on)
    _totals(comp, ret)
    return comp


def _line_key(row):
    return (row["box_no"], row["column"], row["source_type"], row["source_id"] or 0, row["document_number"],
            row["journal_entry_id"] or 0, row["date"].isoformat() if row["date"] else "", str(_q(row["amount_sar"])))


def _canonical(boxes, lines, rc_output):
    payload = {
        "boxes": [[n, str(_q(boxes[n]["amount"])), str(_q(boxes[n]["adjustment"])), str(_q(boxes[n]["vat"]))]
                  for n in sorted(boxes)],
        "rc_output": str(_q(rc_output)),
        "lines": sorted([list(_line_key(r)) for r in lines]),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _canonical_of_comp(comp):
    return _canonical(comp.boxes, comp.lines, comp.rc_output)


def _canonical_of_stored(ret):
    boxes = {b.box_no: {"amount": b.amount_sar, "adjustment": b.adjustment_sar, "vat": b.vat_sar}
             for b in ret.boxes.all()}
    lines = [{"box_no": l.box_no, "column": l.column, "source_type": l.source_type, "source_id": l.source_id,
              "document_number": l.document_number, "journal_entry_id": l.journal_entry_id, "date": l.date,
              "amount_sar": l.amount_sar} for l in ret.source_lines.all()]
    return _canonical(boxes, lines, ret.rc_output_vat_sar)


def _store(ret, comp):
    """Replace the stored boxes and source lines of a draft (the model guards refuse this once locked)."""
    for row in list(ret.source_lines.all()) + list(ret.boxes.all()):
        row._service_write = True
        row.delete()
    defs = _box_definitions(ret.period_end)
    for n in range(1, 17):
        d = defs.get(n)
        en, ar = (d.label_en, d.label_ar) if d else reference.BOX_LABELS[n]
        box = VatReturnBox(entity=ret.entity, vat_return=ret, box_no=n, label_en=en, label_ar=ar,
                           amount_sar=comp.boxes[n]["amount"], adjustment_sar=comp.boxes[n]["adjustment"],
                           vat_sar=comp.boxes[n]["vat"], verified=bool(d and d.verified_at))
        box._service_write = True
        box.save()
    for row in comp.lines:
        line = VatReturnSourceLine(entity=ret.entity, vat_return=ret, box_no=row["box_no"], column=row["column"],
                                   source_type=row["source_type"], source_id=row["source_id"],
                                   document_number=row["document_number"], journal_entry_id=row["journal_entry_id"],
                                   date=row["date"], amount_sar=row["amount_sar"], note=row["note"])
        line._service_write = True
        line.save()
    ret.output_vat_sar, ret.input_vat_sar = comp.output_vat, comp.input_vat
    ret.rc_output_vat_sar, ret.net_vat_sar = comp.rc_output, comp.boxes[16]["vat"]


def _time_bar(corr):
    """R27 / C7 (VAT IR Art. 63, PSG §13.5): an overstatement (any negative difference, below or above the
    threshold) is corrected at the latest 5 years after the end of the calendar year of the original period, i.e.
    the return carrying it must be filed by then."""
    return date_type(corr.original_return.period_end.year + 5, 12, 31)


def _assign_corrections(ret):
    """R27: open next-return corrections of earlier periods go to box 14 of this (not yet approved) return; an
    overstatement past its time bar is not taken (review item 13)."""
    for corr in VatCorrection.objects.select_for_update(of=("self",)).select_related("original_return").filter(
            entity=ret.entity, status=VatCorrection.Status.OPEN, target_return__isnull=True,
            treatment__in=[VatCorrection.Treatment.NEXT_RETURN, VatCorrection.Treatment.OVERSTATEMENT_NEXT_RETURN],
            original_return__period_end__lt=ret.period_start):
        if corr.tax_difference_sar < ZERO and ret.period_start > _time_bar(corr):
            continue
        corr.target_return, corr.status = ret, VatCorrection.Status.ASSIGNED
        corr._service_write = True
        corr.save()


def _not_import(ret):
    if ret.source == VatReturn.Source.IMPORT:
        raise error("An imported legacy return keeps the figures filed; it is never recomputed or adjusted (R12).",
                    "يحتفظ الإقرار المستورد بالأرقام المقدَّمة؛ لا يُعاد احتسابه ولا يُعدَّل.", code="import_not_recomputed")


def _recompute(ret, user):
    _not_import(ret)
    ret.modified_by, ret.modified_at = user, _now()
    if ret.version == 0:
        _assign_corrections(ret)
    _store(ret, _compute(ret))
    ret._service_write = True
    ret.save()
    if kind_of(user) == Kind.AGENT:  # gateway rule: every agent action is logged (access.md R21)
        access_services.log(AccessLog.Event.PERMISSION_GRANTED, actor=user, entity=ret.entity, permission="tax.prepare_return",
                   details={"document": ["tax.vatreturn", ret.pk], "action": "compute_draft"})
    return ret


def compute_draft(entity, period_start, *, user):
    """Create or recompute the draft VAT return (version 0) of the period starting ``period_start`` (R14 to R27).

    ``tax.prepare_return``; agents may call it (R14: the draft is marked ``source=agent``) but never change the
    schedule (review item 12)."""
    access.require(user, entity, "tax.prepare_return")
    entity = Entity.objects.get(pk=entity.pk)
    _check_vat_entity(entity)
    with transaction.atomic():
        rows = _ensure_schedule(entity, period_start, agent=user if kind_of(user) == Kind.AGENT else None)
        row = next((r for r in rows if r.period_start == period_start), None)
        if row is None:
            raise error(f"No VAT return period of {entity.code} starts on {period_start}.",
                        f"لا تبدأ فترة إقرار للكيان {entity.code} في {period_start}.", code="no_such_period")
        ret = VatReturn.objects.select_for_update().filter(entity=entity, period_start=period_start, version=0).first()
        if ret is None:
            ret = VatReturn(entity=entity, schedule=row, period_start=row.period_start, period_end=row.period_end,
                            frequency=row.frequency, due_date=row.due_date, created_by=user, created_at=_now(),
                            source=VatReturn.Source.AGENT if kind_of(user) == Kind.AGENT else VatReturn.Source.HUMAN)
            ret._service_write = True
            ret.save()
        elif ret.status != ReturnStatus.DRAFT:
            raise _not_draft(ret)
        return _recompute(ret, user)


def recompute_draft(vat_return, *, user):
    """Recompute a draft return or amendment (``tax.prepare_return``; agents allowed, R14)."""
    ret = VatReturn.objects.select_related("entity").get(pk=vat_return.pk)
    access.require(user, ret.entity, "tax.prepare_return")
    with transaction.atomic():
        ret = VatReturn.objects.select_for_update(of=("self",)).select_related("entity").get(pk=ret.pk)
        if ret.status != ReturnStatus.DRAFT:
            raise _not_draft(ret)
        return _recompute(ret, user)


def _not_draft(ret):
    if ret.is_locked:
        return error("This VAT return is approved and locked: it is never recomputed, edited or deleted (R7).",
                     "هذا الإقرار الضريبي معتمد ومقفل: لا يُعاد احتسابه ولا يُعدَّل ولا يُحذف.", code="return_locked")
    return error(f"The return is {ret.status}; return it to draft first.",
                 f"حالة الإقرار {ret.status}؛ أعده إلى المسودة أولًا.", code="not_draft")


def _locked_draft(vat_return, user, permission):
    ret = VatReturn.objects.select_related("entity").get(pk=vat_return.pk)
    access.require(user, ret.entity, permission)
    _human(user, ret.entity, permission)
    ret = VatReturn.objects.select_for_update(of=("self",)).select_related("entity").get(pk=ret.pk)
    if ret.status != ReturnStatus.DRAFT:
        raise _not_draft(ret)
    return ret


def set_refund_requested(vat_return, *, user, requested):
    """Whether a claim (negative box 16) is to be refunded instead of carried to box 15 (R25, OQ-18). The person
    making the choice counts as a preparer (review item 10)."""
    with transaction.atomic():
        ret = _locked_draft(vat_return, user, "tax.prepare_return")
        _not_import(ret)
        ret.refund_requested, ret.refund_requested_by = bool(requested), user
        return _recompute(ret, user)


def add_manual_adjustment(vat_return, *, user, box_no, column, amount_sar, reason_ar, evidence, reason_en="",
                          journal_lines=()):
    """A manual amount on a draft (3.2, R15): bilingual reason (Arabic required), evidence file required."""
    with transaction.atomic():
        ret = _locked_draft(vat_return, user, "tax.prepare_return")
        if box_no not in (*OUTPUT_BOXES, *INPUT_BOXES, 14) or column not in Column.values:
            raise error("Manual adjustments go to boxes 1 to 5, 7 to 11 or 14 (totals are computed).",
                        "تُضاف التعديلات اليدوية إلى الخانات 1 إلى 5 أو 7 إلى 11 أو 14 (المجاميع محسوبة).",
                        code="adjustment_box")
        if box_no == 14 and column != Column.VAT:
            raise error("Box 14 has a VAT column only.", "للخانة 14 عمود ضريبة فقط.", code="adjustment_box")
        if not (reason_ar or "").strip():
            raise error("An Arabic reason is required.", "السبب بالعربية مطلوب.", code="reason_required")
        if not evidence:
            raise error("Evidence is required for a manual adjustment.", "المستند المؤيد مطلوب للتعديل اليدوي.",
                        code="evidence_required")
        amount = _money(amount_sar, "The adjustment", "التعديل")
        if amount == ZERO:
            raise error("The adjustment is zero.", "التعديل صفر.", code="amount_zero")
        lines = list(journal_lines)
        if any(getattr(l, "entry", None) is None or l.entry.entity_id != ret.entity_id for l in lines):
            raise error("Journal lines must be the entity's own.", "يجب أن تكون بنود القيود للكيان نفسه.",
                        code="journal_lines_other_entity")
        adj = VatManualAdjustment(entity=ret.entity, vat_return=ret, box_no=box_no, column=column, amount_sar=amount,
                                  reason_en=(reason_en or "").strip(), reason_ar=reason_ar.strip(), evidence=evidence,
                                  created_by=user, created_at=_now())
        adj._service_write = True
        adj.save()
        if lines:
            adj.journal_lines.set(lines)
        _recompute(ret, user)
        return adj


# --- exceptions and tie-out (R15, R16) -----------------------------------------------------------------

def _ledger_entries(entity, start=None, end=None, ids=None):
    """Per entry: (output = net credit on 2200, input = net debit on 1310), settlement entries (T1, T2, T4) out.
    Entries dated in [start, end], or the entries ``ids``."""
    rows = JournalLine.objects.filter(entry__entity=entity, entry__status__in=ledger.BOOKED,
                                      account__code__in=[VAT_OUTPUT, VAT_INPUT])
    rows = rows.filter(entry_id__in=ids) if ids is not None else rows.filter(entry__date__gte=start,
                                                                              entry__date__lte=end)
    rows = (rows.exclude(entry__document_type__in=SETTLEMENT_TYPES)
            .values("entry_id", "entry__document_type", "entry__number", "account__code")
            .annotate(d=Sum("debit"), c=Sum("credit")))
    out = {}
    for r in rows:
        e = out.setdefault(r["entry_id"], {"type": r["entry__document_type"], "number": r["entry__number"],
                                           "output": ZERO, "input": ZERO})
        d, c = r["d"] or ZERO, r["c"] or ZERO
        if r["account__code"] == VAT_OUTPUT:
            e["output"] += c - d
        else:
            e["input"] += d - c
    return out


def _true_up_entry_ids(ret):
    year = _last_return_of_year(ret)
    if ret.version > 0 or year is None:
        return set()
    status = _art51_port().year_status(ret.entity, year)
    return {e.pk for e, _ in status.true_up_entries} if status.applies else set()


def _correction_net(corr):
    """Net VAT effect of a correction's correcting entries and their reversals: 2200 credits - 1310 debits."""
    ids = set(corr.correcting_entries.values_list("pk", flat=True))
    if not ids:
        return None
    ids |= set(JournalEntry.objects.filter(reversal_of_id__in=ids).values_list("pk", flat=True))
    moves = _ledger_entries(corr.entity, ids=ids)
    return sum((m["output"] - m["input"] for m in moves.values()), ZERO)


def _correction_flags(ret, acks):
    """Review item 4: every correction in box 14 has correcting entries whose net 2200 - 1310 effect equals its
    tax difference; else the reviewer decides (mismatch) or the return cannot proceed (no entry: 2200 would
    drift when the return is paid)."""
    found = []
    for corr in ret.assigned_corrections.exclude(status=VatCorrection.Status.CANCELLED).order_by("pk"):
        ref = f"corr:{corr.pk}"
        net = _correction_net(corr)
        if net is None:
            found.append({"kind": ExceptionKind.CORRECTION_WITHOUT_ENTRY, "reference": ref, "correction": corr.pk,
                          "number": f"COR-{corr.pk}", "difference": corr.tax_difference_sar, "ack": None})
        elif net != corr.tax_difference_sar:
            found.append({"kind": ExceptionKind.CORRECTION_MISMATCH, "reference": ref, "correction": corr.pk,
                          "number": f"COR-{corr.pk}", "difference": corr.tax_difference_sar - net, "entries_net": net,
                          "ack": acks.get((ExceptionKind.CORRECTION_MISMATCH, ref))})
    return found


def _unpaid_bills(ret, acks):
    """C15 (VAT IR as amended 2025, unverified, OQ-43): input VAT on a bill still unpaid 12 months after its date
    may have to be reversed. Listed for the reviewer in the return of the period in which the 12 months end; no
    automatic reversal."""
    from invoicing.models import PayableAllocation, PurchaseDocument

    found = []
    bills = PurchaseDocument.objects.filter(
        entity=ret.entity, doc_type="bill", status="posted", vendor__is_resident=True, total_vat__gt=0,
        accounting_date__gte=_add_months(ret.period_start, -12), accounting_date__lte=_add_months(ret.period_end, -12))
    for doc in bills.order_by("accounting_date", "pk"):
        paid = PayableAllocation.objects.filter(target=doc, date__lte=ret.period_end).aggregate(t=Sum("amount"))["t"]
        if doc.total_gross - (paid or ZERO) > ZERO:
            ref = f"purchase:{doc.pk}"
            found.append({"kind": ExceptionKind.UNPAID_BILL_12_MONTHS, "reference": ref, "document": doc.pk,
                          "number": doc.number, "output": ZERO, "input": doc.total_vat,
                          "ack": acks.get((ExceptionKind.UNPAID_BILL_12_MONTHS, ref))})
    return found


def _exceptions(ret, comp=None):
    """R15, R16 and the review fixes: the exceptions of a return, each with its acknowledgement (if any), and the
    tie-out. ``comp``: the recomputation, when the caller has one."""
    from invoicing.models import SalesDocument

    acks = {(a.kind, a.reference): a for a in ret.exception_acks.all()}
    if ret.source == VatReturn.Source.IMPORT:  # filed outside the ERP; reviewed against its filing
        return [], None
    found = _correction_flags(ret, acks)
    if ret.version > 0:  # an amendment restates a filed return; its sources were tied out then
        return found, None
    comp = comp or _compute(ret)
    excluded = _correction_exclusions(ret.entity)
    tagged = _true_up_entry_ids(ret)
    every = _ledger_entries(ret.entity, ret.period_start, ret.period_end)
    entries = {pk: e for pk, e in every.items() if pk not in excluded}
    not_relevant = set()
    for pk, e in sorted(entries.items()):
        if e["type"] in SOURCE_TYPES or pk in tagged:
            continue
        ref = f"je:{pk}"
        ack = acks.get((ExceptionKind.UNTAGGED_VAT_JOURNAL, ref))
        found.append({"kind": ExceptionKind.UNTAGGED_VAT_JOURNAL, "reference": ref, "entry": pk,
                      "number": e["number"], "output": e["output"], "input": e["input"], "ack": ack})
        if ack and ack.decision == VatExceptionAck.Decision.NOT_RETURN_RELEVANT:
            not_relevant.add(pk)
    for pk in sorted(excluded & set(every)):  # review item 4: left out as correcting entries
        ref = f"je:{pk}"
        found.append({"kind": ExceptionKind.CORRECTING_ENTRY, "reference": ref, "entry": pk,
                      "number": every[pk]["number"], "output": every[pk]["output"], "input": every[pk]["input"],
                      "ack": acks.get((ExceptionKind.CORRECTING_ENTRY, ref))})
    for doc in SalesDocument.objects.filter(entity=ret.entity, status="rejected", issue_date__gte=ret.period_start,
                                            issue_date__lte=ret.period_end).order_by("pk"):
        ref = f"sales:{doc.pk}"
        found.append({"kind": ExceptionKind.REJECTED_DOCUMENT, "reference": ref, "document": doc.pk,
                      "number": doc.number, "output": doc.total_vat_sar, "input": ZERO,
                      "ack": acks.get((ExceptionKind.REJECTED_DOCUMENT, ref))})
    if comp.fallback_documents:  # C10: one decision for the invoices dated by their issue date
        ids = sorted(d.pk for d in comp.fallback_documents)
        ref = "tp:" + hashlib.sha256(",".join(map(str, ids)).encode()).hexdigest()[:20]
        found.append({"kind": ExceptionKind.TAX_POINT_FALLBACK, "reference": ref, "documents": ids,
                      "number": ", ".join(d.number for d in comp.fallback_documents)[:500],
                      "ack": acks.get((ExceptionKind.TAX_POINT_FALLBACK, ref))})
    for flag in comp.flags:
        found.append({**flag, "ack": acks.get((flag["kind"], flag["reference"]))})
    for entry in comp.shifted_in + comp.shifted_out:
        ref = f"je:{entry.pk}"
        found.append({"kind": ExceptionKind.PERIOD_SHIFT, "reference": ref, "entry": entry.pk, "number": entry.number,
                      "date": entry.date, "reported_here": entry in comp.shifted_in,
                      "ack": acks.get((ExceptionKind.PERIOD_SHIFT, ref))})
    _, gap = _previous_period(ret)
    if gap:
        ref = f"period:{ret.period_start - timedelta(days=1)}"
        found.append({"kind": ExceptionKind.CARRY_FORWARD_GAP, "reference": ref, "ack": None})
    found += _unpaid_bills(ret, acks)
    shifted_out = {e.pk for e in comp.shifted_out}
    moves_in = _ledger_entries(ret.entity, ids={e.pk for e in comp.shifted_in}) if comp.shifted_in else {}
    counted = [e for pk, e in entries.items() if pk not in not_relevant and pk not in shifted_out]
    counted += list(moves_in.values())
    rate_difference = sum((d for _, d in comp.rate_differences), ZERO)
    output_rate_difference = sum((d for _, d in comp.output_rate_differences), ZERO)
    ledger_output = sum((e["output"] for e in counted), ZERO) - output_rate_difference
    ledger_input = sum((e["input"] for e in counted), ZERO) - rate_difference
    tie = {"box_output": ret.output_vat_sar, "box_input": ret.input_vat_sar,
           "ledger_output": ledger_output, "ledger_input": ledger_input,
           "difference_output": ledger_output - ret.output_vat_sar,
           "difference_input": ledger_input - ret.input_vat_sar,
           "rate_difference_input": rate_difference,
           "rate_difference_output": output_rate_difference,
           "shifted_in": sorted(moves_in), "shifted_out": sorted(shifted_out),
           "excluded_entries": sorted(not_relevant | (excluded & set(every)))}
    for account, diff in ((VAT_OUTPUT, tie["difference_output"]), (VAT_INPUT, tie["difference_input"])):
        if diff:
            found.append({"kind": ExceptionKind.TIE_OUT_DIFFERENCE, "reference": account, "difference": diff,
                          "ack": None})
    return found, tie


def exceptions(vat_return, *, user):
    """The return's exceptions and acknowledgements (R15, R16). Needs ``tax.view`` and ``ledger.view`` (they show
    journal entries, like the tie-out; review item 12)."""
    ret = VatReturn.objects.select_related("entity").get(pk=vat_return.pk)
    access.require(user, ret.entity, "tax.view")
    access.require(user, ret.entity, "ledger.view")
    return _exceptions(ret)[0]


def tie_out(vat_return, *, user):
    """R15: output VAT in the boxes vs net credits on 2200, recoverable input VAT vs net debits on 1310, for the
    period, without the return's own settlement entries (T1, T2, T4), correcting entries (R26) and journals the
    reviewer marked not relevant; plus entries reported here but dated in another period and minus those dated
    here but reported elsewhere (tax point, supplier note date: C9, C10), and minus the booking-rate difference of
    foreign-currency bills (C12) and of sales booked at another rate than their SAMA tax-point rate (§10.5).
    Needs ``tax.view`` and ``ledger.view``."""
    ret = VatReturn.objects.select_related("entity").get(pk=vat_return.pk)
    access.require(user, ret.entity, "tax.view")
    access.require(user, ret.entity, "ledger.view")
    return _exceptions(ret)[1]


ALLOWED_DECISIONS = {
    ExceptionKind.UNTAGGED_VAT_JOURNAL: {VatExceptionAck.Decision.NOT_RETURN_RELEVANT,
                                         VatExceptionAck.Decision.INCLUDED_BY_ADJUSTMENT},
    ExceptionKind.REJECTED_DOCUMENT: {VatExceptionAck.Decision.TAGGED, VatExceptionAck.Decision.NOT_RETURN_RELEVANT,
                                      VatExceptionAck.Decision.INCLUDED_BY_ADJUSTMENT},
    ExceptionKind.TAX_POINT_FALLBACK: {VatExceptionAck.Decision.TAX_POINT_CONFIRMED,
                                       VatExceptionAck.Decision.INCLUDED_BY_ADJUSTMENT},
    ExceptionKind.PERIOD_SHIFT: {VatExceptionAck.Decision.TAX_POINT_CONFIRMED},
    ExceptionKind.SAR_RATE_MISSING: {VatExceptionAck.Decision.INCLUDED_BY_ADJUSTMENT},
    ExceptionKind.PURCHASE_SAR_VAT_MISSING: {VatExceptionAck.Decision.DIFFERENCE_EXPLAINED,
                                             VatExceptionAck.Decision.INCLUDED_BY_ADJUSTMENT},
    ExceptionKind.CORRECTING_ENTRY: {VatExceptionAck.Decision.CONFIRMED_CORRECTION},
    ExceptionKind.CORRECTION_MISMATCH: {VatExceptionAck.Decision.DIFFERENCE_EXPLAINED},
    ExceptionKind.UNPAID_BILL_12_MONTHS: {VatExceptionAck.Decision.NOT_RETURN_RELEVANT,
                                          VatExceptionAck.Decision.INCLUDED_BY_ADJUSTMENT},
}
OUTPUT_KINDS = {ExceptionKind.REJECTED_DOCUMENT, ExceptionKind.TAX_POINT_FALLBACK, ExceptionKind.SAR_RATE_MISSING}


def acknowledge_exception(vat_return, *, user, kind, reference, decision, reason_ar, reason_en=""):
    """The reviewer's decision on an exception (R15, R16), while the return is in review; not by a preparer.
    Blocking kinds (tie-out difference, correction without entry, earlier period without a return) are fixed at
    their source, never acknowledged."""
    ret = VatReturn.objects.select_related("entity").get(pk=vat_return.pk)
    access.require(user, ret.entity, "tax.review_return")
    _human(user, ret.entity, "tax.review_return")
    if user.pk in _preparers(ret):
        access.deny(user, ret.entity, "tax.review_return", "self_approval")
    with transaction.atomic():
        ret = VatReturn.objects.select_for_update(of=("self",)).select_related("entity").get(pk=ret.pk)
        if ret.status != ReturnStatus.IN_REVIEW:
            raise error("Exceptions are acknowledged while the return is in review.",
                        "تُقَرّ الاستثناءات أثناء مراجعة الإقرار.", code="not_in_review")
        if not (reason_ar or "").strip():
            raise error("An Arabic reason is required.", "السبب بالعربية مطلوب.", code="reason_required")
        found = {(e["kind"], e["reference"]): e for e in _exceptions(ret)[0]}
        item = found.get((kind, reference))
        if item is None or kind in BLOCKING_EXCEPTIONS:
            raise error("No open exception with this reference; a tie-out difference, a correction without a "
                        "correcting entry or a missing earlier return is fixed at its source, never acknowledged.",
                        "لا يوجد استثناء مفتوح بهذا المرجع؛ يُعالَج فرق المطابقة أو التصحيح بلا قيد أو الإقرار السابق "
                        "المفقود من مصدره ولا يُقَرّ.", code="no_such_exception")
        if decision not in ALLOWED_DECISIONS.get(kind, set()):
            raise error("This decision does not apply to this exception.", "هذا القرار لا ينطبق على هذا الاستثناء.",
                        code="decision_invalid")
        if decision == VatExceptionAck.Decision.INCLUDED_BY_ADJUSTMENT:
            adjustments = ret.manual_adjustments.all()
            if kind == ExceptionKind.UNTAGGED_VAT_JOURNAL:
                adjustments = adjustments.filter(journal_lines__entry_id=item["entry"])
            elif kind in OUTPUT_KINDS:
                adjustments = adjustments.filter(box_no__in=OUTPUT_BOXES)
            else:
                adjustments = adjustments.filter(box_no__in=INPUT_BOXES)
            if not adjustments.exists():
                raise error("No manual adjustment of this return covers it.", "لا يوجد تعديل يدوي في الإقرار يغطيه.",
                            code="adjustment_missing")
        old = item["ack"]
        if old is not None:
            old._service_write = True
            old.delete()
        ack = VatExceptionAck(entity=ret.entity, vat_return=ret, kind=kind, reference=reference, decision=decision,
                              reason_en=(reason_en or "").strip(), reason_ar=reason_ar.strip(), acknowledged_by=user,
                              acknowledged_at=_now())
        ack._service_write = True
        ack.save()
        return ack


# --- lifecycle (R7 to R10) ------------------------------------------------------------------------------

def submit_for_review(vat_return, *, user):
    """draft -> in_review by the preparer (``tax.prepare_return``; a person, R14)."""
    with transaction.atomic():
        ret = _locked_draft(vat_return, user, "tax.prepare_return")
        ret.status, ret.submitted_by, ret.submitted_at = ReturnStatus.IN_REVIEW, user, _now()
        ret.returned_reason_en = ret.returned_reason_ar = ""
        ret._service_write = True
        ret.save()
        return ret


def return_to_draft(vat_return, *, user, reason_ar, reason_en=""):
    """in_review or reviewed -> draft with a reason (R7), by a reviewer or approver."""
    ret = VatReturn.objects.select_related("entity").get(pk=vat_return.pk)
    permission = "tax.review_return" if access.has(user, ret.entity, "tax.review_return") else "tax.approve_return"
    access.require(user, ret.entity, permission, **({"document": ret} if permission == "tax.approve_return" else {}))
    _human(user, ret.entity, permission)
    if not (reason_ar or "").strip():
        raise error("An Arabic reason is required.", "السبب بالعربية مطلوب.", code="reason_required")
    with transaction.atomic():
        ret = VatReturn.objects.select_for_update().get(pk=ret.pk)
        if ret.status not in (ReturnStatus.IN_REVIEW, ReturnStatus.REVIEWED):
            raise _not_draft(ret) if ret.is_locked else error(
                "Only a return in review or reviewed goes back to draft.", "يعود إلى المسودة الإقرار قيد المراجعة فقط.",
                code="not_in_review")
        _back_to_draft(ret, reason_en.strip(), reason_ar.strip())
        return ret


def _back_to_draft(ret, reason_en, reason_ar):
    ret.status = ReturnStatus.DRAFT
    ret.reviewed_by = ret.reviewed_at = None
    ret.returned_reason_en, ret.returned_reason_ar = reason_en[:500], reason_ar[:500]
    ret._service_write = True
    ret.save()


SOURCES_CHANGED = ("The sources changed since the return was computed; it is back in draft to be recomputed (R10).",
                   "تغيرت المصادر منذ احتساب الإقرار؛ أُعيد إلى المسودة لإعادة احتسابه.")


def _refuse_changed_sources(ret):
    """R10: if a recomputation differs from what is stored, the return goes back to draft and the step is refused.
    Returns the recomputation."""
    if ret.source == VatReturn.Source.IMPORT:
        return None
    comp = _compute(ret)
    if _canonical_of_comp(comp) == _canonical_of_stored(ret):
        return comp
    with transaction.atomic():
        stored = VatReturn.objects.select_for_update().get(pk=ret.pk)
        if stored.status in (ReturnStatus.IN_REVIEW, ReturnStatus.REVIEWED):
            _back_to_draft(stored, *SOURCES_CHANGED)
    raise error(*SOURCES_CHANGED, code="sources_changed")


def _refuse_open_exceptions(ret, comp):
    """R15, R16 (review item 2): no step past review while an exception is unacknowledged or the VAT accounts do
    not tie out to 0.00 (re-run at approval, so a journal posted after review is caught)."""
    found, tie = _exceptions(ret, comp)
    open_ = [e for e in found if e["ack"] is None]
    if tie is not None and (tie["difference_output"] or tie["difference_input"]) and not any(
            e["kind"] == ExceptionKind.TIE_OUT_DIFFERENCE for e in open_):
        open_.append({"kind": ExceptionKind.TIE_OUT_DIFFERENCE, "reference": "tie-out"})
    if open_:
        listing = ", ".join(f"{e['kind']} {e['reference']}" for e in open_)
        raise error(f"Exceptions are open or the VAT accounts do not tie out: {listing}.",
                    f"توجد استثناءات مفتوحة أو لا تتطابق حسابات الضريبة: {listing}.", code="exceptions_open")
    return found, tie


def complete_review(vat_return, *, user):
    """in_review -> reviewed (``tax.review_return``), by a person who did not prepare it (R8); every exception
    acknowledged and the tie-out difference 0.00 (R15, R16); sources unchanged (R10)."""
    ret = VatReturn.objects.select_related("entity").get(pk=vat_return.pk)
    access.require(user, ret.entity, "tax.review_return")
    _human(user, ret.entity, "tax.review_return")
    if user.pk in _preparers(ret):
        access.deny(user, ret.entity, "tax.review_return", "self_approval")
    if ret.status != ReturnStatus.IN_REVIEW:
        raise error("Only a return in review is reviewed.", "تُراجَع الإقرارات قيد المراجعة فقط.", code="not_in_review")
    if ret.source == VatReturn.Source.IMPORT:
        _check_import_allowed(ret.entity, ret.period_start, ret.period_end, existing=ret)
    else:
        _refuse_open_exceptions(ret, _refuse_changed_sources(ret))
    with transaction.atomic():
        ret = VatReturn.objects.select_for_update().get(pk=ret.pk)
        if ret.status != ReturnStatus.IN_REVIEW:
            raise error("Only a return in review is reviewed.", "تُراجَع الإقرارات قيد المراجعة فقط.",
                        code="not_in_review")
        ret.status, ret.reviewed_by, ret.reviewed_at = ReturnStatus.REVIEWED, user, _now()
        ret._service_write = True
        ret.save()
        return ret


def _open_periods(ret):
    return list(FiscalPeriod.objects.filter(entity=ret.entity, start_date__lte=ret.period_end,
                                            end_date__gte=ret.period_start)
                .exclude(status=FiscalPeriod.Status.CLOSED).order_by("start_date").values_list("name", flat=True))


def _correction_ledger(ret):
    """Net 2200 credits and 1310 debits of the correcting entries of the corrections in box 14 (any date)."""
    ids = set(ret.assigned_corrections.exclude(status=VatCorrection.Status.CANCELLED)
              .values_list("correcting_entries", flat=True)) - {None}
    out_, in_ = ZERO, ZERO
    for line in JournalLine.objects.filter(entry_id__in=ids, entry__status__in=ledger.BOOKED,
                                           account__code__in=[VAT_OUTPUT, VAT_INPUT]):
        if line.account.code == VAT_OUTPUT:
            out_ += line.credit - line.debit
        else:
            in_ += line.debit - line.credit
    return out_, in_


def _offset(ret):
    """T1 / R33: offset = min(output VAT, input VAT + box 15 credit), each side with the correcting entries of the
    box-14 corrections. Amendments post no offset (their difference is booked by the correcting entries)."""
    if ret.version > 0:
        return ZERO
    box15 = ret.boxes.get(box_no=15).vat_sar
    corr_out, corr_in = _correction_ledger(ret)
    return max(ZERO, min(ret.output_vat_sar + corr_out, ret.input_vat_sar + corr_in + box15))


_posting = ContextVar("tax_posting_document", default=None)


@contextmanager
def _posting_document(document):
    token = _posting.set((document._meta.label_lower, document.pk))
    try:
        yield
    finally:
        _posting.reset(token)


_reversing = ContextVar("tax_reversing_document", default=None)


@contextmanager
def _reversing_document(document):
    token = _reversing.set((document._meta.label_lower, document.pk))
    try:
        yield
    finally:
        _reversing.reset(token)


def _reverse_guard(document):
    """Registered ``reverse_guard`` (review item 5): T2 to T4 are reversed only by ``reverse_tax_document``, which
    keeps the document's and the return's status (settled / filed) in step with the ledger."""
    return _reversing.get() == (document._meta.label_lower, document.pk)


def _require_posting(document):
    if _posting.get() != (document._meta.label_lower, document.pk):
        raise error(f"{document._meta.label} posts only through tax.services.",
                    f"يُرحَّل المستند {document._meta.label} من خلال خدمات الضرائب فقط.", code="post_through_services")


def _source_hash(ret):
    """R10 (review item 2): SHA-256 of the canonical boxes and source lines plus every 2200 / 1310 movement of the
    period (entry, output, input), so a later change to the VAT accounts of the period is visible."""
    moves = _ledger_entries(ret.entity, ret.period_start, ret.period_end)
    payload = _canonical_of_stored(ret) + json.dumps(
        [[pk, str(m["output"]), str(m["input"])] for pk, m in sorted(moves.items())], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _check_time_bars(ret, on):
    for corr in ret.assigned_corrections.select_related("original_return").filter(
            tax_difference_sar__lt=0).exclude(status=VatCorrection.Status.CANCELLED):
        if on > _time_bar(corr):
            raise error(f"The overstatement COR-{corr.pk} is time-barred on {on}: it had to be corrected by "
                        f"{_time_bar(corr)} (VAT IR Art. 63).",
                        f"انقضت مهلة تصحيح الزيادة COR-{corr.pk} في {on}: كان يجب تصحيحها قبل {_time_bar(corr)}.",
                        code="correction_time_barred")


def approve_return(vat_return, *, user):
    """reviewed -> approved (locked) by the CFO (R7 to R10, R33): ``tax.approve_return`` grant issued for this
    return, not a preparer (R8, review item 10) or its reviewer; three different people always (owner decision G3:
    no top-authority self-approval for VAT returns). Every overlapping fiscal period closed (R9); every earlier
    period of the schedule has an approved return; the Art. 51 true-up posted in the last return of the year
    (R55); sources unchanged (R10); exceptions acknowledged and the tie-out 0.00 (R15, review item 2). Freezes the
    source lines (``source_hash``) and posts T1 dated today.

    An imported legacy return (R12, review item 3) goes from reviewed straight to ``filed`` with the figures as
    filed, if its period is still before the go-live date and has no posted ERP sources; nothing is posted."""
    ret = VatReturn.objects.select_related("entity").get(pk=vat_return.pk)
    grant = access.require(user, ret.entity, "tax.approve_return", document=ret)
    _human(user, ret.entity, "tax.approve_return")
    if ret.reviewed_by_id == user.pk or user.pk in _preparers(ret):
        access.deny(user, ret.entity, "tax.approve_return", "self_approval")
    if ret.status != ReturnStatus.REVIEWED:
        raise error("Only a reviewed return is approved.", "يُعتمد الإقرار بعد مراجعته فقط.", code="not_reviewed")
    if ret.source == VatReturn.Source.IMPORT:
        return _approve_import(ret, user, grant)
    periods = _open_periods(ret)
    if periods:
        raise error(f"Close every fiscal period of the return first (R9): {', '.join(periods)} still open.",
                    f"أقفل كل الفترات المالية للإقرار أولًا: {', '.join(periods)} ما زالت مفتوحة.", code="periods_open")
    earlier = VatReturn.objects.filter(entity=ret.entity, period_start__lt=ret.period_start).exclude(
        status__in=LOCKED_STATUSES)
    if earlier.exists():
        raise error("An earlier VAT return of the entity is not approved yet; approve it first (box 15).",
                    "إقرار سابق للكيان لم يُعتمد بعد؛ اعتمده أولًا (الخانة 15).", code="earlier_return_open")
    missing = [r.period_start for r in VatReturnSchedule.objects.filter(entity=ret.entity,
                                                                         period_end__lt=ret.period_start)
               if not r.returns.filter(status__in=LOCKED_STATUSES).exists()]
    if missing:
        raise error(f"Earlier VAT periods have no approved return: {', '.join(map(str, missing))} (box 15).",
                    f"فترات سابقة بلا إقرار معتمد: {', '.join(map(str, missing))} (الخانة 15).",
                    code="earlier_return_missing")
    year = _last_return_of_year(ret)
    if ret.version == 0 and year is not None:
        status = _art51_port().year_status(ret.entity, year)
        if status.applies and not (status.final_ratio_approved and status.true_up_posted):
            raise error(f"The Art. 51 final ratio of {year} is not approved or its true-up is not posted (R55).",
                        f"لم تُعتمد النسبة النهائية للمادة 51 لسنة {year} أو لم تُرحَّل تسويتها.",
                        code="art51_true_up_missing")
    _check_time_bars(ret, _today())
    _refuse_open_exceptions(ret, _refuse_changed_sources(ret))
    with transaction.atomic():
        ret = VatReturn.objects.select_for_update(of=("self",)).select_related("entity").get(pk=ret.pk)
        if ret.status != ReturnStatus.REVIEWED:
            raise error("Only a reviewed return is approved.", "يُعتمد الإقرار بعد مراجعته فقط.", code="not_reviewed")
        ret.source_hash = _source_hash(ret)
        ret.settlement_offset_sar = _offset(ret)
        ret.status, ret.approved_by, ret.approved_at = ReturnStatus.APPROVED, user, _now()
        ret._service_write = True
        ret.save()
        if ret.settlement_offset_sar > ZERO:
            with _posting_document(ret):
                try:
                    entry = ledger.create_entry(
                        ret.entity, _today(), f"VAT offset {ret.period_start}..{ret.period_end} / مقاصة ضريبة القيمة "
                        f"المضافة", None, user=user, reference=f"VAT-{ret.period_start:%Y%m}-v{ret.version}",
                        post=True, grant=grant, document=ret)
                except LedgerError as exc:
                    raise TaxError(exc.messages, code=getattr(exc, "code", None) or "posting_refused") from None
            ret.settlement_entry = entry
            ret._service_write = True
            ret.save()
        else:
            access.use_grant(grant, target=ret)
        return ret


def _approve_import(ret, user, grant):
    _check_import_allowed(ret.entity, ret.period_start, ret.period_end, existing=ret)
    with transaction.atomic():
        ret = VatReturn.objects.select_for_update(of=("self",)).select_related("entity").get(pk=ret.pk)
        if ret.status != ReturnStatus.REVIEWED:
            raise error("Only a reviewed return is approved.", "يُعتمد الإقرار بعد مراجعته فقط.", code="not_reviewed")
        ret.source_hash = hashlib.sha256(_canonical_of_stored(ret).encode()).hexdigest()
        ret.status, ret.approved_by, ret.approved_at = ReturnStatus.FILED, user, _now()
        ret._service_write = True
        ret.save()
        access.use_grant(grant, target=ret)
        return ret


def _vat_return_lines(document):
    """Registered ``build_lines`` for VatReturn: T1, Dr 2200 / Cr 1310 by the offset fixed at approval."""
    _require_posting(document)
    amount = document.settlement_offset_sar
    label = f"VAT offset {document.period_start}..{document.period_end}"
    return [{"account": VAT_OUTPUT, "debit": amount, "description": label},
            {"account": VAT_INPUT, "credit": amount, "description": label}]


def record_filing(vat_return, *, user, zatca_filing_reference, filed_on, acknowledgement, sadad_bill_number="",
                  voluntary_disclosure_ref=""):
    """approved -> filed (``tax.record_filing``): the ZATCA reference, date and portal receipt. An amendment needs the
    voluntary disclosure reference; the return it amends becomes ``amended`` (R28). An overstatement in box 14 must
    be filed within its 5-year bar (C7)."""
    ret = VatReturn.objects.select_related("entity").get(pk=vat_return.pk)
    access.require(user, ret.entity, "tax.record_filing")
    _human(user, ret.entity, "tax.record_filing")
    if not (zatca_filing_reference or "").strip() or filed_on is None or not acknowledgement:
        raise error("The ZATCA filing reference, filing date and portal acknowledgement are required.",
                    "مرجع التقديم وتاريخه وإيصال البوابة مطلوبة.", code="filing_incomplete")
    if ret.version > 0 and not (voluntary_disclosure_ref or "").strip():
        raise error("An amendment is filed with its voluntary disclosure reference (R28).",
                    "يُقدَّم التعديل مع مرجع الإفصاح الطوعي.", code="disclosure_ref_required")
    _check_time_bars(ret, filed_on)
    with transaction.atomic():
        ret = VatReturn.objects.select_for_update(of=("self",)).select_related("entity").get(pk=ret.pk)
        if ret.status != ReturnStatus.APPROVED:
            raise error("Only an approved return is filed.", "يُقدَّم الإقرار المعتمد فقط.", code="not_approved")
        ret.status, ret.filed_by, ret.filed_at = ReturnStatus.FILED, user, _now()
        ret.zatca_filing_reference, ret.filed_on = zatca_filing_reference.strip(), filed_on
        ret.acknowledgement, ret.sadad_bill_number = acknowledgement, (sadad_bill_number or "").strip()
        ret._service_write = True
        ret.save()
        for corr in ret.assigned_corrections.select_for_update().exclude(status=VatCorrection.Status.CANCELLED):
            corr.status = VatCorrection.Status.FILED
            if ret.version > 0:
                corr.voluntary_disclosure_ref = voluntary_disclosure_ref.strip()
            corr._service_write = True
            corr.save()
        if ret.amends_id:
            original = VatReturn.objects.select_for_update().get(pk=ret.amends_id)
            original.status = ReturnStatus.AMENDED
            original._service_write = True
            original.save()
        _refresh_settled(ret)
        return VatReturn.objects.get(pk=ret.pk)


def _check_import_allowed(entity, start, end, existing=None):
    """R12 / review item 3 / G4: a legacy return is imported only for a period that ends before the entity's VAT
    go-live date and has no posted ERP sources (sales, bills, import records)."""
    golive = _settings(entity).vat_golive_date
    if golive is None:
        raise error(f"{entity.code} has no VAT go-live date; legacy returns are imported only for periods before it.",
                    f"لا يوجد تاريخ بدء للإقرارات للكيان {entity.code}؛ تُستورد الإقرارات السابقة لما قبله فقط.",
                    code="legacy_cutoff_missing")
    if end >= golive:
        raise error(f"The period {start}..{end} is not before the VAT go-live date {golive}; it is prepared in the ERP.",
                    f"الفترة {start}..{end} ليست قبل تاريخ البدء {golive}؛ تُعدّ في النظام.", code="import_after_golive")
    if JournalEntry.objects.filter(entity=entity, status__in=ledger.BOOKED, document_type__in=SOURCE_TYPES,
                                   date__gte=start, date__lte=end).exists():
        raise error("The period has posted sales, bills or import records in the ERP; it cannot be imported.",
                    "للفترة مبيعات أو مشتريات أو سجلات استيراد مرحّلة في النظام؛ لا يمكن استيرادها.",
                    code="import_period_has_sources")
    others = VatReturn.objects.filter(entity=entity, period_start=start)
    if existing is not None:
        others = others.exclude(pk=existing.pk)
    if others.exists():
        raise error("A return for this period already exists.", "يوجد إقرار لهذه الفترة بالفعل.", code="no_such_period")


def import_filed_return(entity, *, user, period_start, boxes, zatca_filing_reference, filed_on, refund_requested=False,
                        acknowledgement=None, rc_output_vat_sar="0"):
    """R12: a return filed before go-live (e.g. in Falcon), entered with its box values and filing reference and no
    source lines. It is created as a ``draft`` with ``source=import`` and becomes ``filed`` only after review and
    approval by two other people (submit, review, approve: review item 3); only for periods before the entity's
    VAT go-live date without posted ERP sources. ``boxes`` = {box_no: {"amount", "adjustment", "vat"}} as filed."""
    access.require(user, entity, "tax.prepare_return")
    access.require(user, entity, "tax.record_filing")
    _human(user, entity, "tax.record_filing")
    entity = Entity.objects.get(pk=entity.pk)
    _check_vat_entity(entity)
    if not (zatca_filing_reference or "").strip() or filed_on is None:
        raise error("The ZATCA filing reference and date are required.", "مرجع التقديم وتاريخه مطلوبان.",
                    code="filing_incomplete")
    values = {n: {c: ZERO for c in ("amount", "adjustment", "vat")} for n in range(1, 17)}
    for n, cols in boxes.items():
        if int(n) not in values:
            raise error(f"There is no box {n}.", f"لا توجد خانة {n}.", code="adjustment_box")
        for c, v in cols.items():
            values[int(n)][c] = _money(v, f"Box {n}", f"الخانة {n}")
    if values[16]["vat"] != values[13]["vat"] + values[14]["vat"] - values[15]["vat"]:
        raise error("Box 16 must equal 13 + 14 - 15 as filed.", "يجب أن تساوي الخانة 16 مجموع 13 و14 ناقص 15.",
                    code="import_boxes_inconsistent")
    with transaction.atomic():
        Entity.objects.select_for_update().get(pk=entity.pk)
        period = next(((s, e, f) for s, e, f in _walk(entity, _settings(entity), period_start) if s == period_start),
                      None)
        if period is None:
            raise error("No VAT return period starts on this date.", "لا تبدأ فترة إقرار في هذا التاريخ.",
                        code="no_such_period")
        start, end, frequency = period
        _check_import_allowed(entity, start, end)
        now = _now()
        ret = VatReturn(entity=entity, schedule=None, period_start=start, period_end=end, frequency=frequency,
                        due_date=_due_date(end), status=ReturnStatus.DRAFT, source=VatReturn.Source.IMPORT,
                        created_by=user, created_at=now, filed_by=user, filed_at=now,
                        zatca_filing_reference=zatca_filing_reference.strip(), filed_on=filed_on,
                        refund_requested=bool(refund_requested), net_vat_sar=values[16]["vat"],
                        rc_output_vat_sar=_money(rc_output_vat_sar, "Reverse-charge output", "ضريبة الاحتساب العكسي"))
        ret.output_vat_sar = sum((values[n]["vat"] for n in OUTPUT_BOXES), ZERO) + ret.rc_output_vat_sar
        ret.input_vat_sar = sum((values[n]["vat"] for n in INPUT_BOXES), ZERO)
        if acknowledgement:
            ret.acknowledgement = acknowledgement
        ret._service_write = True
        ret.save()
        for n in range(1, 17):
            en, ar = reference.BOX_LABELS[n]
            box = VatReturnBox(entity=entity, vat_return=ret, box_no=n, label_en=en, label_ar=ar,
                               amount_sar=values[n]["amount"], adjustment_sar=values[n]["adjustment"],
                               vat_sar=values[n]["vat"])
            box._service_write = True
            box.save()
        return VatReturn.objects.get(pk=ret.pk)


# --- corrections and amendments (R26 to R28) ------------------------------------------------------------

def _treatment(difference, threshold):
    if abs(difference) < threshold:
        return VatCorrection.Treatment.NEXT_RETURN
    if difference > ZERO:
        return VatCorrection.Treatment.AMENDMENT
    return VatCorrection.Treatment.OVERSTATEMENT_NEXT_RETURN


def _period_corrections(entity, period_start):
    """Corrections of one original period, all versions (C6), not cancelled and not already filed by an amendment."""
    return (VatCorrection.objects.select_for_update(of=("self",)).select_related("target_return")
            .filter(entity=entity, original_return__period_start=period_start)
            .exclude(status=VatCorrection.Status.CANCELLED)
            .exclude(treatment=VatCorrection.Treatment.AMENDMENT, status=VatCorrection.Status.FILED))


def _check_correcting_entries(entity, entries, own=None):
    used = _correction_exclusions(entity)
    if own is not None:
        used -= set(own.correcting_entries.values_list("pk", flat=True))
    for entry in entries:
        if entry.entity_id != entity.pk or entry.status not in ledger.BOOKED or entry.pk in used \
                or entry.document_type in SETTLEMENT_TYPES:
            raise error("Correcting entries are posted entries of the entity, not linked to another correction.",
                        "قيود التصحيح قيود مرحّلة للكيان غير مرتبطة بتصحيح آخر.", code="correcting_entry_invalid")


def record_correction(original_return, *, user, discovered_on, description_en, description_ar, tax_difference_sar,
                      correcting_entries=()):
    """R27: record an error in a filed return. The corrections of the same original period (all versions) are
    netted first (C6, PSG §13.5): net below the threshold: next return (box 14); net underdeclared at or above it:
    amendment by voluntary disclosure within 20 days (``notify_by``, alert); net overdeclared at or above it:
    claimed in a next return within 5 years of the end of the calendar year of the original period. When the net
    reaches the threshold every correction of the period that is not yet locked is re-classified with it."""
    original = VatReturn.objects.select_related("entity").get(pk=original_return.pk)
    entity = original.entity
    access.require(user, entity, "tax.prepare_return")
    _human(user, entity, "tax.prepare_return")
    if original.status not in (ReturnStatus.FILED, ReturnStatus.SETTLED):
        raise error("Corrections are recorded on a filed return (its latest version).",
                    "تُسجَّل التصحيحات على إقرار مقدَّم (أحدث نسخة منه).", code="original_not_filed")
    if not ((description_en or "").strip() and (description_ar or "").strip()):
        raise error("The description is required in English and Arabic.", "الوصف مطلوب بالإنجليزية والعربية.",
                    code="description_required")
    difference = _money(tax_difference_sar, "The tax difference", "فرق الضريبة")
    if difference == ZERO:
        raise error("The tax difference is zero.", "فرق الضريبة صفر.", code="amount_zero")
    entries = list(correcting_entries)
    _check_correcting_entries(entity, entries)
    with transaction.atomic():
        others = list(_period_corrections(entity, original.period_start))
        net = difference + sum((c.tax_difference_sar for c in others), ZERO)
        treatment = _treatment(net, _settings(entity).vat_correction_threshold_sar)
        if difference < ZERO and discovered_on > date_type(original.period_end.year + 5, 12, 31):
            raise error("An overstatement is claimed at the latest 5 years after the end of the calendar year of the "
                        "original period (VAT IR Art. 63).",
                        "تُسترد الزيادة خلال 5 سنوات من نهاية السنة التقويمية لفترة الإقرار الأصلي.",
                        code="correction_time_barred")
        notify_by = discovered_on + timedelta(days=20) if treatment == VatCorrection.Treatment.AMENDMENT else None
        corr = VatCorrection(entity=entity, original_return=original, discovered_on=discovered_on,
                             description_en=description_en.strip(), description_ar=description_ar.strip(),
                             tax_difference_sar=difference, treatment=treatment, created_by=user, created_at=_now(),
                             notify_by=notify_by)
        corr._service_write = True
        corr.save()
        if entries:
            corr.correcting_entries.set(entries)
        if treatment != VatCorrection.Treatment.NEXT_RETURN:
            for other in others:
                locked = other.status == VatCorrection.Status.FILED or (
                    other.target_return_id and other.target_return.status in LOCKED_STATUSES)
                if locked or other.treatment == treatment:
                    continue
                other.treatment, other.target_return, other.status = treatment, None, VatCorrection.Status.OPEN
                other.notify_by = notify_by
                other._service_write = True
                other.save()
        return corr


def link_correcting_entries(correction, *, user, entries):
    """Review item 4: link correcting entries to a correction that no approved return carries yet (once linked, an
    entry is never unlinked, R26)."""
    corr = VatCorrection.objects.select_related("entity", "target_return").get(pk=correction.pk)
    access.require(user, corr.entity, "tax.prepare_return")
    _human(user, corr.entity, "tax.prepare_return")
    entries = list(entries)
    with transaction.atomic():
        corr = VatCorrection.objects.select_for_update(of=("self",)).select_related("target_return").get(pk=corr.pk)
        if corr.status not in (VatCorrection.Status.OPEN, VatCorrection.Status.ASSIGNED) or (
                corr.target_return_id and corr.target_return.status in LOCKED_STATUSES):
            raise error("Correcting entries are linked only while no approved return carries the correction.",
                        "تُربط قيود التصحيح فقط قبل اعتماد الإقرار الذي يتضمن التصحيح.", code="correction_locked")
        _check_correcting_entries(corr.entity, entries, own=corr)
        corr.correcting_entries.add(*entries)
        return corr


def cancel_correction(correction, *, user, reason):
    """Cancel a correction that no approved return carries yet."""
    corr = VatCorrection.objects.select_related("entity", "target_return").get(pk=correction.pk)
    access.require(user, corr.entity, "tax.prepare_return")
    _human(user, corr.entity, "tax.prepare_return")
    if not (reason or "").strip():
        raise error("A reason is required.", "السبب مطلوب.", code="reason_required")
    with transaction.atomic():
        corr = VatCorrection.objects.select_for_update(of=("self",)).select_related("target_return").get(pk=corr.pk)
        if corr.status == VatCorrection.Status.FILED or (corr.target_return and corr.target_return.is_locked):
            raise error("A correction carried by an approved return is not cancelled.",
                        "لا يُلغى تصحيح يتضمنه إقرار معتمد.", code="correction_locked")
        corr.status, corr.cancel_reason = VatCorrection.Status.CANCELLED, reason.strip()[:500]
        corr._service_write = True
        corr.save()
        return corr


def create_amendment(original_return, *, user):
    """R28: a new version of a filed return carrying its open amendment corrections; full lifecycle again."""
    original = VatReturn.objects.select_related("entity").get(pk=original_return.pk)
    entity = original.entity
    access.require(user, entity, "tax.prepare_return")
    _human(user, entity, "tax.prepare_return")
    with transaction.atomic():
        versions = VatReturn.objects.select_for_update().filter(entity=entity, period_start=original.period_start)
        latest = versions.order_by("-version").first()
        if latest.status not in (ReturnStatus.FILED, ReturnStatus.SETTLED):
            raise error("Only the filed latest version of a return is amended; an amendment may already be open.",
                        "يُعدَّل أحدث إصدار مقدَّم فقط؛ قد يكون هناك تعديل مفتوح.", code="amendment_not_allowed")
        corrections = list(VatCorrection.objects.select_for_update().filter(
            entity=entity, original_return__in=versions, treatment=VatCorrection.Treatment.AMENDMENT,
            status=VatCorrection.Status.OPEN))
        if not corrections:
            raise error("No open amendment correction for this period.", "لا يوجد تصحيح مفتوح يتطلب تعديلًا لهذه الفترة.",
                        code="no_amendment_correction")
        ret = VatReturn(entity=entity, schedule=latest.schedule, period_start=latest.period_start,
                        period_end=latest.period_end, frequency=latest.frequency, version=latest.version + 1,
                        amends=latest, due_date=min(c.notify_by for c in corrections), created_by=user,
                        created_at=_now(), refund_requested=latest.refund_requested)
        ret._service_write = True
        ret.save()
        for corr in corrections:
            corr.target_return, corr.status = ret, VatCorrection.Status.ASSIGNED
            corr._service_write = True
            corr.save()
        return _recompute(ret, user)


# --- payments, refunds and import VAT records (R22, R33 to R36) ------------------------------------------

def _latest_locked(ret):
    return (VatReturn.objects.filter(entity=ret.entity, period_start=ret.period_start,
                                     status__in=[ReturnStatus.APPROVED, ReturnStatus.FILED, ReturnStatus.SETTLED])
            .order_by("-version").first())


def _paid(ret):
    return TaxPayment.objects.filter(vat_return__entity=ret.entity, vat_return__period_start=ret.period_start,
                                     status=DocStatus.POSTED).aggregate(t=Sum("amount_sar"))["t"] or ZERO


def _open_amount(ret):
    """R35: box 16 of the latest approved version minus posted payments on every version of the period."""
    latest = _latest_locked(ret)
    if latest is None:
        return ZERO
    return max(ZERO, latest.net_vat_sar) - _paid(ret)


def _refunded(ret):
    return TaxRefund.objects.filter(vat_return=ret, status=DocStatus.POSTED).aggregate(t=Sum("amount_sar"))["t"] or ZERO


def _refresh_settled(ret):
    """filed <-> settled: settled once nothing is left to pay (box 16 > 0) or to receive (refund requested)."""
    ret = VatReturn.objects.select_for_update().get(pk=ret.pk)
    if ret.status not in (ReturnStatus.FILED, ReturnStatus.SETTLED):
        return ret
    if ret.net_vat_sar > ZERO:
        done = _open_amount(ret) <= ZERO
    elif ret.net_vat_sar < ZERO and ret.refund_requested:
        done = _refunded(ret) >= -ret.net_vat_sar
    else:
        done = ret.net_vat_sar == ZERO
    target = ReturnStatus.SETTLED if done else ReturnStatus.FILED
    if ret.status != target:
        ret.status = target
        ret._service_write = True
        ret.save()
    return ret


def open_amount(vat_return, *, user):
    """What is still to be paid for the return's period (R35). Needs ``tax.view``."""
    ret = VatReturn.objects.select_related("entity").get(pk=vat_return.pk)
    access.require(user, ret.entity, "tax.view")
    return _open_amount(ret)


def _bank_account(entity, account):
    """A bank GL account of the entity: 1100 or a sub-account of 1100 (banking R5), never a restricted account
    (1140, the purification account: invoicing R54)."""
    acc = account if isinstance(account, Account) else Account.objects.filter(entity=entity, code=account).first()
    if acc is None or acc.entity_id != entity.pk or not acc.is_postable or not acc.is_active \
            or acc.code in restricted.codes() or not (acc.code == BANK or (acc.parent_id and acc.parent.code == BANK)):
        raise error("Choose a bank account (1100 or a bank sub-account) of the entity.",
                    "اختر حسابًا بنكيًا للكيان (1100 أو حسابًا فرعيًا بنكيًا).", code="bank_account_invalid")
    return acc


def _new_document(model, user, **fields):
    doc = model(created_by=user, created_at=_now(), **fields)
    doc._service_write = True
    doc.save()
    return doc


def create_vat_payment(vat_return, *, user, amount_sar, payment_date, bank_account, sadad_bill_number="",
                       bank_reference=""):
    """Draft a VAT payment (T2) of an approved or filed return (``tax.prepare_return``; a person)."""
    ret = VatReturn.objects.select_related("entity").get(pk=vat_return.pk)
    access.require(user, ret.entity, "tax.prepare_return")
    _human(user, ret.entity, "tax.prepare_return")
    if ret.status not in (ReturnStatus.APPROVED, ReturnStatus.FILED, ReturnStatus.SETTLED):
        raise error("A payment is made for an approved, not superseded return.",
                    "تُدفع الضريبة لإقرار معتمد غير معدَّل.", code="return_not_payable")
    amount = _money(amount_sar, "The payment", "الدفعة", positive=True)
    bank = _bank_account(ret.entity, bank_account)
    return _new_document(TaxPayment, user, entity=ret.entity, vat_return=ret, amount_sar=amount,
                         payment_date=payment_date, bank_account=bank, sadad_bill_number=(sadad_bill_number or "").strip(),
                         bank_reference=(bank_reference or "").strip())


def create_vat_refund(vat_return, *, user, amount_sar, received_on, bank_account, zatca_reference):
    """Draft a refund received from ZATCA (T4) for a filed claim with ``refund_requested``."""
    ret = VatReturn.objects.select_related("entity").get(pk=vat_return.pk)
    access.require(user, ret.entity, "tax.prepare_return")
    _human(user, ret.entity, "tax.prepare_return")
    if ret.status not in (ReturnStatus.FILED, ReturnStatus.SETTLED) or ret.net_vat_sar >= ZERO \
            or not ret.refund_requested:
        raise error("A refund is recorded for a filed claim (box 16 below zero) with a refund request.",
                    "يُسجَّل الاسترداد لمطالبة مقدَّمة (الخانة 16 أقل من صفر) طُلب استردادها.", code="refund_not_allowed")
    if not (zatca_reference or "").strip():
        raise error("The ZATCA reference is required.", "مرجع الهيئة مطلوب.", code="reference_required")
    amount = _money(amount_sar, "The refund", "الاسترداد", positive=True)
    bank = _bank_account(ret.entity, bank_account)
    return _new_document(TaxRefund, user, entity=ret.entity, vat_return=ret, amount_sar=amount,
                         received_on=received_on, bank_account=bank, zatca_reference=zatca_reference.strip())


def create_import_vat_record(entity, *, user, customs_declaration_no, declaration_date, customs_value_sar,
                             vat_paid_sar, attribution, bank_account, declaration_file, debit_account=None,
                             customs_duty_sar="0", other_charges_sar="0"):
    """Draft an import VAT record (R22, T3): VAT paid at customs, by bank in this slice. The VAT base (box 8 Amount)
    is customs value + customs duties + other charges (C13 [verify]). A cost account for non-recoverable VAT is
    checked when the record is entered: never Sharia-prohibited where Sharia is enforced, never restricted (review
    item 13)."""
    access.require(user, entity, "tax.prepare_return")
    _human(user, entity, "tax.prepare_return")
    if attribution not in ImportVatRecord.Attribution.values:
        raise error("Unknown VAT attribution.", "إسناد ضريبة غير معروف.", code="attribution_invalid")
    if not (customs_declaration_no or "").strip() or not declaration_file:
        raise error("The customs declaration number and file are required.", "رقم البيان الجمركي وملفه مطلوبان.",
                    code="declaration_required")
    debit = None
    if attribution != ImportVatRecord.Attribution.TAXABLE:
        debit = debit_account if isinstance(debit_account, Account) else Account.objects.filter(
            entity=entity, code=debit_account).first()
        if debit is None or debit.entity_id != entity.pk or debit.type not in (Account.Type.EXPENSE, Account.Type.ASSET) \
                or not debit.is_postable or not debit.is_active or debit.code in (VAT_INPUT, VAT_OUTPUT, BANK) \
                or debit.code in restricted.codes():
            raise error("Non-recoverable import VAT goes to a cost or asset account of the entity.",
                        "تُحمَّل ضريبة الاستيراد غير القابلة للاسترداد على حساب تكلفة أو أصل للكيان.",
                        code="debit_account_invalid")
        if debit.sharia_prohibited and Entity.objects.get(pk=entity.pk).sharia_enforced:
            raise error(f"Account {debit.code} is Sharia-prohibited in {entity.code}.",
                        f"الحساب {debit.code} محظور شرعًا في الكيان {entity.code}.", code="debit_account_prohibited")
    if ImportVatRecord.objects.filter(entity=entity, customs_declaration_no=customs_declaration_no.strip()).exists():
        raise error("This customs declaration is already recorded.", "هذا البيان الجمركي مسجل مسبقًا.",
                    code="declaration_duplicate")
    return _new_document(
        ImportVatRecord, user, entity=entity, customs_declaration_no=customs_declaration_no.strip(),
        declaration_date=declaration_date,
        customs_value_sar=_money(customs_value_sar, "The customs value", "القيمة الجمركية", positive=True),
        customs_duty_sar=_money(customs_duty_sar, "The customs duties", "الرسوم الجمركية", positive=True,
                                allow_zero=True),
        other_charges_sar=_money(other_charges_sar, "The other charges", "الرسوم الأخرى", positive=True,
                                 allow_zero=True),
        vat_paid_sar=_money(vat_paid_sar, "The VAT paid", "الضريبة المدفوعة", positive=True), attribution=attribution,
        debit_account=debit, bank_account=_bank_account(entity, bank_account), declaration_file=declaration_file)


def submit_tax_document(document, *, user):
    """draft -> submitted for a payment, refund or import record (``tax.prepare_return``; a person)."""
    model = type(document)
    doc = model.objects.select_related("entity").get(pk=document.pk)
    access.require(user, doc.entity, "tax.prepare_return")
    _human(user, doc.entity, "tax.prepare_return")
    with transaction.atomic():
        doc = model.objects.select_for_update().get(pk=doc.pk)
        if doc.status != DocStatus.DRAFT:
            raise error("Only drafts are submitted.", "تُقدَّم المسودات فقط.", code="not_draft")
        doc.status, doc.submitted_by, doc.submitted_at = DocStatus.SUBMITTED, user, _now()
        doc._service_write = True
        doc.save()
        return doc


def _document_date(doc):
    return {ImportVatRecord: "declaration_date", TaxPayment: "payment_date", TaxRefund: "received_on"}[type(doc)]


def approve_tax_document(document, *, user):
    """Approve and post a submitted payment (T2), refund (T4) or import record (T3) with ``tax.approve_payment``,
    by a person who did not create, change or submit it (R34, access R35). A payment never exceeds the open amount
    of its return (R35); a refund never exceeds the claim."""
    model = type(document)
    doc = model.objects.select_related("entity").get(pk=document.pk)
    grant = access.require(user, doc.entity, "tax.approve_payment", document=doc)
    _human(user, doc.entity, "tax.approve_payment")
    with transaction.atomic():
        doc = model.objects.select_for_update(of=("self",)).select_related("entity").get(pk=doc.pk)
        if doc.status != DocStatus.SUBMITTED:
            raise error("Only submitted documents are approved.", "تُعتمد المستندات المقدمة فقط.", code="not_submitted")
        if model is TaxPayment:
            list(VatReturn.objects.select_for_update().filter(entity=doc.entity,  # serialise payments of the period
                                                              period_start=doc.vat_return.period_start))
            if doc.vat_return.status == ReturnStatus.AMENDED or _latest_locked(doc.vat_return) != doc.vat_return:
                raise error("Pay the latest version of the return.", "ادفع لأحدث نسخة من الإقرار.", code="return_amended")
            open_ = _open_amount(doc.vat_return)
            if doc.amount_sar > open_:
                raise error(f"The payment {doc.amount_sar} exceeds the open amount {open_} of the return (R35).",
                            f"الدفعة {doc.amount_sar} تتجاوز المبلغ المتبقي {open_} من الإقرار.", code="overpayment")
        if model is TaxRefund:
            room = -doc.vat_return.net_vat_sar - _refunded(doc.vat_return)
            if doc.amount_sar > room:
                raise error(f"The refund exceeds the claim still open ({room}).",
                            f"الاسترداد يتجاوز المطالبة المتبقية ({room}).", code="refund_exceeds_claim")
        doc.status, doc.approved_by, doc.approved_at = DocStatus.POSTED, user, _now()
        doc._service_write = True
        doc.save()
        with _posting_document(doc):
            try:
                entry = ledger.create_entry(doc.entity, getattr(doc, _document_date(doc)), _description(doc), None,
                                            user=user, reference=_reference(doc), post=True, grant=grant, document=doc)
            except LedgerError as exc:
                raise TaxError(exc.messages, code=getattr(exc, "code", None) or "posting_refused") from None
        doc.journal_entry = entry
        doc._service_write = True
        doc.save()
        if model in (TaxPayment, TaxRefund):
            _refresh_settled(doc.vat_return)
        return doc


def _reference(doc):
    if isinstance(doc, ImportVatRecord):
        return doc.customs_declaration_no
    if isinstance(doc, TaxPayment):
        return doc.sadad_bill_number or doc.bank_reference or f"VAT-PAY-{doc.pk}"
    return doc.zatca_reference


def _description(doc):
    if isinstance(doc, ImportVatRecord):
        return f"Import VAT, customs {doc.customs_declaration_no} / ضريبة الاستيراد، بيان جمركي {doc.customs_declaration_no}"
    if isinstance(doc, TaxPayment):
        return (f"VAT payment {doc.vat_return.period_start}..{doc.vat_return.period_end} / سداد ضريبة القيمة المضافة")
    return f"VAT refund {doc.zatca_reference} / استرداد ضريبة القيمة المضافة"


def _reversal_date(entity, original, requested):
    """R36: the requested date, else the original date when its period is open, else the first open period."""
    if requested is not None:
        return requested
    period = FiscalPeriod.objects.filter(entity=entity, start_date__lte=original, end_date__gte=original).first()
    if period is not None and period.status == FiscalPeriod.Status.OPEN:
        return original
    later = FiscalPeriod.objects.filter(entity=entity, status=FiscalPeriod.Status.OPEN, start_date__gt=original) \
        .order_by("start_date").first()
    if later is None:
        raise error("No open period after the original date for the reversal; pass a date.",
                    "لا توجد فترة مفتوحة بعد التاريخ الأصلي للعكس؛ حدّد تاريخًا.", code="no_open_period")
    return later.start_date


def reverse_tax_document(document, *, user, reason, date=None):
    """R36: reverse a posted payment, refund or import record (``tax.reverse_document``), dated in an open period
    (default: the original date if open, else the first open period after it); then enter a new document."""
    model = type(document)
    doc = model.objects.select_related("entity", "journal_entry").get(pk=document.pk)
    grant = access.require(user, doc.entity, "tax.reverse_document", document=doc)
    _human(user, doc.entity, "tax.reverse_document")
    if not (reason or "").strip():
        raise error("A reason is required.", "السبب مطلوب.", code="reason_required")
    with transaction.atomic():
        doc = model.objects.select_for_update(of=("self",)).select_related("entity", "journal_entry").get(pk=doc.pk)
        if doc.status != DocStatus.POSTED or doc.journal_entry_id is None:
            raise error("Only posted documents are reversed.", "تُعكس المستندات المرحّلة فقط.", code="not_posted")
        # R17: the reversal counts negative in the return of its own period, never in the locked one.
        on = _reversal_date(doc.entity, doc.journal_entry.date, date)
        try:
            with _reversing_document(doc):
                reversal = ledger.reverse_entry(doc.journal_entry, grant, date=on, reason=reason.strip())
        except LedgerError as exc:
            raise TaxError(exc.messages, code=getattr(exc, "code", None) or "reversal_refused") from None
        doc.status, doc.reversal_entry = DocStatus.REVERSED, reversal
        doc._service_write = True
        doc.save()
        if model in (TaxPayment, TaxRefund):
            _refresh_settled(doc.vat_return)
        return doc


def _bank_lines(doc, debit, credit, amount, label):
    return [{"account": debit, "debit": amount, "description": label},
            {"account": credit, "credit": amount, "description": label}]


def _import_lines(document):
    """Registered ``build_lines`` for ImportVatRecord: T3, Dr 1310 (taxable) or the cost account / Cr bank."""
    _require_posting(document)
    debit = VAT_INPUT if document.attribution == ImportVatRecord.Attribution.TAXABLE else document.debit_account
    return _bank_lines(document, debit, document.bank_account, document.vat_paid_sar,
                       f"Import VAT {document.customs_declaration_no}")


def _payment_lines(document):
    """Registered ``build_lines`` for TaxPayment: T2, Dr 2200 / Cr bank."""
    _require_posting(document)
    return _bank_lines(document, VAT_OUTPUT, document.bank_account, document.amount_sar, "VAT payment")


def _refund_lines(document):
    """Registered ``build_lines`` for TaxRefund: T4, Dr bank / Cr 1310."""
    _require_posting(document)
    return _bank_lines(document, document.bank_account, VAT_INPUT, document.amount_sar, "VAT refund")


# --- warnings, alerts, exports (R5, R6, R13, R53) --------------------------------------------------------

def _warnings(ret):
    out = []
    defs = _box_definitions(ret.period_end)
    if len(defs) < 16 or any(d.verified_at is None for d in defs.values()):
        out.append("Box numbers and labels are unverified against the current ZATCA VAT return form (R6). / "
                   "أرقام الخانات وتسمياتها غير متحقق منها مقابل نموذج الإقرار الحالي لدى الهيئة.")
    if ret.frequency == Frequency.QUARTERLY:
        since = ret.period_end - timedelta(days=365)
        total = ZERO
        seen = set()
        for other in VatReturn.objects.filter(entity=ret.entity, period_end__gt=since, period_end__lte=ret.period_end) \
                .exclude(status=ReturnStatus.AMENDED).order_by("period_start", "-version"):
            if other.period_start in seen:
                continue
            seen.add(other.period_start)
            total += sum((b.amount_sar + b.adjustment_sar for b in other.boxes.filter(box_no__in=OUTPUT_BOXES)), ZERO)
        if total > QUARTERLY_LIMIT_SAR:
            out.append(f"Supplies of the last 12 months are SAR {total}, above SAR 40,000,000: monthly returns may be "
                       "required (R5, [CC-verify]). / توريدات آخر 12 شهرًا تتجاوز 40,000,000 ريال: قد يلزم تقديم "
                       "إقرارات شهرية.")
    return out


def warnings(vat_return, *, user):
    """Bilingual warnings shown with the return (R5, R6). Needs ``tax.view``."""
    ret = VatReturn.objects.select_related("entity").get(pk=vat_return.pk)
    access.require(user, ret.entity, "tax.view")
    return _warnings(ret)


def alerts(entity, *, user):
    """R13: the entity's deadline alerts, for its accountants (``tax.prepare_return``) and approvers."""
    if not access.has(user, entity, "tax.approve_return"):
        access.require(user, entity, "tax.prepare_return")
    return list(TaxAlert.objects.filter(entity=entity).order_by("-alert_date", "pk"))


EXPORT_HEADERS = {
    "en": ["Box", "Label", "Amount (SAR)", "Adjustment (SAR)", "VAT (SAR)", "Definition"],
    "ar": ["الخانة", "التسمية", "المبلغ (ريال)", "التعديلات (ريال)", "الضريبة (ريال)", "التعريف"],
}


def _export_rows(ret, language):
    other = "ar" if language == "en" else "en"
    head = [
        ["Entity / الكيان", ret.entity.code, ret.entity.name_en, ret.entity.name_ar],
        ["VAT number / الرقم الضريبي", ret.entity.vat_number],
        ["Period / الفترة", str(ret.period_start), str(ret.period_end)],
        ["Version / النسخة", str(ret.version)],
        ["Status / الحالة", ret.get_status_display()],
        ["Due date / تاريخ الاستحقاق", str(ret.due_date)],
        ["Enter these figures manually in the ZATCA portal; this ERP does not file. / "
         "أدخل هذه الأرقام يدويًا في بوابة الهيئة؛ هذا النظام لا يقدّم الإقرار."],
        *[[w] for w in _warnings(ret)],
        [],
        EXPORT_HEADERS[language] + [EXPORT_HEADERS[other][1]],
    ]
    rows = []
    for box in ret.boxes.order_by("box_no"):
        label, label_other = (box.label_en, box.label_ar) if language == "en" else (box.label_ar, box.label_en)
        rows.append([box.box_no, label, str(box.amount_sar), str(box.adjustment_sar), str(box.vat_sar),
                     "verified / متحقق منه" if box.verified else UNVERIFIED, label_other])
    tail = [[], ["Reverse-charge output VAT included in box 13 [verify] / ضريبة مخرجات الاحتساب العكسي ضمن الخانة 13",
                 str(ret.rc_output_vat_sar)]]
    return head, rows, tail


FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def _safe_text(value):
    """Review item 8 (CSV / formula injection): text that a spreadsheet would read as a formula gets a leading
    apostrophe. Amounts (numbers, possibly negative) are left as they are."""
    if not isinstance(value, str) or not value.startswith(FORMULA_STARTS):
        return value
    try:
        Decimal(value)
        return value
    except InvalidOperation:
        return "'" + value


def export_return(vat_return, *, user, fmt="xlsx", language="en"):
    """The return as a filled worksheet for manual entry into the ZATCA portal (§6): ``fmt`` ``csv`` or ``xlsx``,
    headers in ``language`` (``en`` / ``ar``; Arabic sheets right to left), labels in both languages (R53).
    Text cells are written as strings, never formulas (review item 8). Returns (filename, bytes). Needs
    ``tax.view``."""
    ret = VatReturn.objects.select_related("entity").get(pk=vat_return.pk)
    access.require(user, ret.entity, "tax.view")
    if fmt not in ("csv", "xlsx") or language not in ("en", "ar"):
        raise error("Export as csv or xlsx, in en or ar.", "يُصدَّر بصيغة csv أو xlsx، بالعربية أو الإنجليزية.",
                    code="export_format")
    head, rows, tail = _export_rows(ret, language)
    name = f"vat-return-{ret.entity.code}-{ret.period_start}-v{ret.version}-{language}.{fmt}"
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        for row in head + rows + tail:
            writer.writerow([_safe_text(v) for v in row])
        return name, buf.getvalue().encode("utf-8-sig")
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "VAT return" if language == "en" else "الإقرار الضريبي"
    ws.sheet_view.rightToLeft = language == "ar"

    position = {"row": 0}

    def put(values):
        position["row"] += 1
        for c, value in enumerate(values, start=1):
            cell = ws.cell(row=position["row"], column=c)
            if isinstance(value, str):
                cell.value = _safe_text(value)
                cell.data_type = "s"  # never a formula
            else:
                cell.value = value

    for row in head:
        put(row)
    for row in rows:
        put([row[0], row[1], Decimal(row[2]), Decimal(row[3]), Decimal(row[4]), row[5], row[6]])
    for row in tail:
        put(row)
    out = io.BytesIO()
    wb.save(out)
    return name, out.getvalue()


PROPOSE_BOX = "tax.propose_box_verification"  # owner decision H3: the accountant proposes
VERIFY_BOX = "tax.maintain_rates"  # the approver (CFO) verifies


def _box_verifier(user, entity, definition, permission):
    access.require(user, entity, permission)
    _human(user, entity, permission)
    if entity.consolidation != Entity.Consolidation.PARENT:
        raise error("Box definitions are verified at the group parent.", "يُتحقق من تعريفات الخانات في الشركة الأم.",
                    code="group_parent_only")
    return VatBoxDefinition.objects.select_for_update().get(pk=definition.pk)


def propose_box_verification(definition, *, user, entity):
    """Step 1 (review item 12; owner decision H3): a person holding ``tax.propose_box_verification`` at the group
    parent (seeded: the accountant) records that a box definition matches the current ZATCA form. Another person,
    holding ``tax.maintain_rates`` (seeded: the approver, CFO), verifies it (``verify_box_definition``)."""
    with transaction.atomic():
        found = _box_verifier(user, entity, definition, PROPOSE_BOX)
        if found.verified_at is not None:
            raise error("This box definition is already verified.", "تم التحقق من تعريف الخانة هذا بالفعل.",
                        code="already_verified")
        found.proposed_by, found.proposed_at = user, _now()
        found.save(update_fields=["proposed_by", "proposed_at"])
        access_services.log(AccessLog.Event.PERMISSION_GRANTED, actor=user, entity=entity, permission=PROPOSE_BOX,
                            details={"action": "propose_box_verification", "box": found.box_no, "definition": found.pk})
        return found


def verify_box_definition(definition, *, user, entity):
    """Step 2 (R6, review item 12; owner decision H3): mark a proposed box definition as checked against the current
    ZATCA form; by a second person holding ``tax.maintain_rates`` in the group parent entity (seeded: the approver,
    CFO), never the proposer."""
    with transaction.atomic():
        found = _box_verifier(user, entity, definition, VERIFY_BOX)
        if found.proposed_by_id is None:
            raise error("A box definition is verified after another person proposed it.",
                        "يُتحقق من تعريف الخانة بعد أن يقترحه شخص آخر.", code="verification_not_proposed")
        if found.proposed_by_id == user.pk:
            access.deny(user, entity, "tax.maintain_rates", "self_approval")
        found.verified_by, found.verified_at = user, _now()
        found.save(update_fields=["verified_by", "verified_at"])
        access_services.log(AccessLog.Event.PERMISSION_GRANTED, actor=user, entity=entity,
                            permission="tax.maintain_rates",
                            details={"action": "verify_box_definition", "box": found.box_no, "definition": found.pk})
        return found


def _rate(code, on):
    return StatutoryTaxRate.objects.filter(code=code, valid_from__lte=on) \
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=on)).order_by("-valid_from").first()


def _late_payment_rate(on):
    return _rate(StatutoryTaxRate.Code.VAT_LATE_PAYMENT_MONTHLY, on)


def _months_late(due, on):
    """C16 (PSG §14: "for each month or part thereof"): calendar months started since the due date."""
    months = 0
    while _add_months(due, months) < on:
        months += 1
    return months
