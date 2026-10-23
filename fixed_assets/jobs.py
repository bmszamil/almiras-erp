"""Scheduled jobs of the fixed-asset module (operator commands; no user action, no business data changed).

``raise_reminders(on)`` (R16, R19; AT-48): for every entity with fixed-asset settings,

- at its fiscal year end and at each period end it designates as a reporting date (``reporting_dates``), an
  impairment-indicator reminder (IAS 36.9; IFRS for SMEs 27.7);
- at its fiscal year end, a review reminder for residual values, useful lives and methods: full IFRS "at least at each
  financial year end" (IAS 16.51); IFRS for SMEs worded as an indicator check (s17.19).

Reminders are append-only rows (one per entity, kind and date). No job deletes anything (R42).
"""
from django.db import transaction

from core.models import Entity
from ledger.models import FiscalPeriod

from .models import FixedAssetSettings, ReviewReminder

IMPAIRMENT = (
    "Impairment: assess at {on} whether there is any indication that an asset of {entity} is impaired (IAS 36.9; "
    "IFRS for SMEs 27.7); record an impairment event with the recoverable amount and evidence where there is.",
    "هبوط القيمة: قيّم في {on} وجود أي مؤشر على هبوط قيمة أي أصل للكيان {entity} (معيار المحاسبة الدولي 36؛ "
    "القسم 27)؛ وسجّل حدث هبوط قيمة بالمبلغ القابل للاسترداد والمستند المؤيد إن وُجد.",
)
REVIEW_FULL = (
    "Year end {on}: review the residual value, useful life and depreciation method of every asset of {entity} "
    "(IAS 16.51); record a change of estimate event where they changed.",
    "نهاية السنة {on}: راجع القيمة المتبقية والعمر الإنتاجي وطريقة الاستهلاك لكل أصل للكيان {entity} (معيار المحاسبة "
    "الدولي 16)؛ وسجّل حدث تغيير تقدير إن تغيّرت.",
)
REVIEW_SME = (
    "Year end {on}: check for indications that the residual value, useful life or depreciation method of an asset "
    "of {entity} has changed (IFRS for SMEs 17.19); record a change of estimate event where it has.",
    "نهاية السنة {on}: تحقق من وجود مؤشرات على تغيّر القيمة المتبقية أو العمر الإنتاجي أو طريقة الاستهلاك لأي أصل "
    "للكيان {entity} (القسم 17)؛ وسجّل حدث تغيير تقدير إن تغيّرت.",
)


def _year_end(entity, on):
    last = FiscalPeriod.objects.filter(entity=entity, start_date__year=on.year).order_by("-end_date").first()
    return last is not None and last.end_date == on


def _remind(entity, kind, on, texts):
    en, ar = (t.format(on=on, entity=entity.code) for t in texts)
    if ReviewReminder.objects.filter(entity=entity, kind=kind, due_date=on).exists():
        return None
    row = ReviewReminder(entity=entity, kind=kind, due_date=on, message_en=en, message_ar=ar)
    row._service_write = True  # append-only (R42)
    row.save()
    return row


def raise_reminders(on):
    """Raise the reminders due on ``on``; returns the new :class:`ReviewReminder` rows."""
    out = []
    with transaction.atomic():
        for settings in FixedAssetSettings.objects.select_related("entity").filter(entity__is_active=True):
            entity = settings.entity
            year_end = _year_end(entity, on)
            reporting = year_end or str(on) in (settings.reporting_dates or [])
            if reporting:
                out.append(_remind(entity, ReviewReminder.Kind.IMPAIRMENT_INDICATORS, on, IMPAIRMENT))
            if year_end:
                full = entity.reporting_standard == Entity.Standard.IFRS_FULL
                out.append(_remind(entity, ReviewReminder.Kind.ESTIMATE_REVIEW, on, REVIEW_FULL if full else REVIEW_SME))
    return [r for r in out if r is not None]
