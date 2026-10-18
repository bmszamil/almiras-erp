"""Tax operator jobs (management commands only; not user actions, so outside ``tax/services.py`` and its access
scan, like ``hr/jobs.py``). Run daily by an operator or the scheduler on the KSA-hosted host.

``deadline_alerts`` (R13): first the return schedule of every active VAT entity is generated up to today (review
item 7; from the entity's VAT go-live date, owner decision G4, so periods filed outside the ERP raise nothing).
Then, for each VAT return period of which no version is filed (``filed``, ``settled`` or ``amended``), an in-app
alert at each of the entity's ``alert_days`` before the due date, and daily once overdue with display-only
estimates (PSG §14): late payment 5% of the open VAT per calendar month or part of a month started since the due
date (C16), and late filing 5% to 25% of the tax that should have been reported (C17). Daily once a filed
return's payment is overdue. For amendment corrections, alerts before and after ``notify_by`` (R27) with the 25%
incorrect-return penalty on the difference (C17). No weekend or holiday shift (OQ-8), in-app only (OQ-9).
Idempotent: one alert per period (or correction), kind and day. Rates are ``StatutoryTaxRate`` rows; a row nobody
verified is shown as "unverified" (R6).
"""
from decimal import Decimal

from django.db import IntegrityError, transaction

from core.models import Entity

from . import services
from .models import FILED_STATUSES, LOCKED_STATUSES, StatutoryTaxRate, TaxAlert, TaxError, VatCorrection, VatReturn, \
    VatReturnSchedule

ZERO = Decimal("0")
HUNDRED = Decimal("100")


def _pct(amount, row):
    if row is None or amount is None:
        return None
    return services._q(max(ZERO, amount) * row.rate / HUNDRED)


def _unverified(*rows):
    return any(r is not None and r.verified_at is None for r in rows)


def _exposure(base, on, due):
    """Late-payment estimate: open amount x monthly rate x calendar months started since the due date (C16)."""
    rate = services._late_payment_rate(on)
    if base is None or rate is None or base <= ZERO:
        return None
    return services._q(base * rate.rate / HUNDRED * services._months_late(due, on))


def _alert(entity, kind, on, due, en, ar, *, schedule=None, correction=None, exposure=None, filing=(None, None),
           incorrect=None):
    alert = TaxAlert(entity=entity, kind=kind, schedule=schedule, correction=correction, alert_date=on, due_date=due,
                     message_en=en[:500], message_ar=ar[:500], exposure_sar=exposure, late_filing_min_sar=filing[0],
                     late_filing_max_sar=filing[1], incorrect_return_sar=incorrect, created_at=services._now())
    alert._service_write = True
    try:
        with transaction.atomic():
            alert.save()
    except IntegrityError:  # already raised today
        return None
    return alert


def _generate_schedules(on):
    """Review item 7: every active VAT entity's periods up to today, so a period nobody opened still alerts."""
    for entity in Entity.objects.filter(is_active=True).exclude(vat_number="").order_by("pk"):
        if not services._vat_ready(entity):
            continue
        try:
            with transaction.atomic():
                services._ensure_schedule(entity, on)
        except TaxError:  # a conflicting schedule is left for a person to resolve; it never stops the job
            continue


def _period_alerts(row, on):
    entity = row.entity
    days = services._settings(entity).alert_days
    versions = VatReturn.objects.filter(entity=entity, period_start=row.period_start)
    filed = versions.filter(status__in=FILED_STATUSES).exists()  # review item 7: any version filed or amended
    latest = versions.order_by("-version").first()
    to_due = (row.due_date - on).days
    period = f"{row.period_start}..{row.period_end}"
    if not filed:
        if to_due in days:
            return _alert(
                entity, TaxAlert.Kind.VAT_DUE_SOON, on, row.due_date, schedule=row,
                en=f"{entity.code}: the VAT return {period} is due on {row.due_date} ({to_due} days).",
                ar=f"{entity.code}: يستحق الإقرار الضريبي للفترة {period} في {row.due_date} (بعد {to_due} يوم).")
        if to_due < 0:
            due_tax = latest.net_vat_sar if latest is not None else None
            exposure = _exposure(due_tax, on, row.due_date)
            low_row = services._rate(StatutoryTaxRate.Code.VAT_LATE_FILING_MIN, on)
            high_row = services._rate(StatutoryTaxRate.Code.VAT_LATE_FILING_MAX, on)
            low, high = _pct(due_tax, low_row), _pct(due_tax, high_row)
            mark = " (unverified)" if _unverified(services._late_payment_rate(on), low_row, high_row) else ""
            mark_ar = " (غير متحقق منه)" if mark else ""
            estimate = f" Estimated late-payment exposure SAR {exposure}{mark}." if exposure is not None else ""
            estimate_ar = f" تقدير غرامة التأخر في السداد {exposure} ريال{mark_ar}." if exposure is not None else ""
            if low is not None and high is not None and due_tax > ZERO:
                estimate += f" Late-filing penalty SAR {low} to {high} (5% to 25%, PSG §14){mark}."
                estimate_ar += f" غرامة التأخر في التقديم من {low} إلى {high} ريال (5% إلى 25%){mark_ar}."
            return _alert(
                entity, TaxAlert.Kind.VAT_OVERDUE, on, row.due_date, schedule=row, exposure=exposure,
                filing=(low, high) if due_tax is not None and due_tax > ZERO else (None, None),
                en=f"{entity.code}: the VAT return {period} was due on {row.due_date} and is not filed.{estimate}",
                ar=f"{entity.code}: كان الإقرار الضريبي للفترة {period} مستحقًا في {row.due_date} ولم يُقدَّم."
                   f"{estimate_ar}")
        return None
    locked = versions.filter(status__in=LOCKED_STATUSES).exclude(status="amended").order_by("-version").first()
    if to_due < 0 and locked is not None and services._open_amount(locked) > ZERO:
        open_ = services._open_amount(locked)
        exposure = _exposure(open_, on, row.due_date)
        return _alert(
            entity, TaxAlert.Kind.VAT_PAYMENT_OVERDUE, on, row.due_date, schedule=row, exposure=exposure,
            en=f"{entity.code}: VAT of {period} (SAR {open_}) was due on {row.due_date} and is not paid. Estimated "
               f"exposure SAR {exposure} (unverified).",
            ar=f"{entity.code}: ضريبة الفترة {period} مستحقة منذ {row.due_date} ولم تُسدَّد. تقدير الغرامة "
               f"{exposure} ريال (غير متحقق منه).")
    return None


def deadline_alerts(*, today=None):
    on = today or services._today()
    _generate_schedules(on)
    created = []
    for row in VatReturnSchedule.objects.select_related("entity").filter(period_end__lt=on).order_by("pk"):
        if row.entity.is_active:
            created.append(_period_alerts(row, on))
    incorrect_row = services._rate(StatutoryTaxRate.Code.VAT_INCORRECT_RETURN, on)
    for corr in VatCorrection.objects.select_related("entity").filter(
            treatment=VatCorrection.Treatment.AMENDMENT, notify_by__isnull=False,
            status__in=[VatCorrection.Status.OPEN, VatCorrection.Status.ASSIGNED]):
        to_due = (corr.notify_by - on).days
        if to_due in services._settings(corr.entity).alert_days or to_due < 0 or corr.discovered_on == on:
            penalty = _pct(corr.tax_difference_sar, incorrect_row)
            note = f" Incorrect-return penalty up to SAR {penalty} (25%, PSG §14, unverified)." if penalty else ""
            note_ar = f" غرامة الإقرار الخاطئ حتى {penalty} ريال (25%، غير متحقق منه)." if penalty else ""
            created.append(_alert(
                corr.entity, TaxAlert.Kind.CORRECTION_NOTIFY, on, corr.notify_by, correction=corr, incorrect=penalty,
                en=f"{corr.entity.code}: underdeclared VAT SAR {corr.tax_difference_sar} needs a voluntary disclosure "
                   f"by {corr.notify_by}.{note}",
                ar=f"{corr.entity.code}: نقص في الضريبة المقرّة قدره {corr.tax_difference_sar} ريال يتطلب إفصاحًا "
                   f"طوعيًا قبل {corr.notify_by}.{note_ar}"))
    return [a for a in created if a is not None]
