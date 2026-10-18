"""Tax operator jobs (management commands only; not user actions, so outside ``tax/services.py`` and its access
scan, like ``hr/jobs.py``). Run daily by an operator or the scheduler on the KSA-hosted host.

``deadline_alerts`` (R13): for each VAT return period not yet filed, an in-app alert at each of the entity's
``alert_days`` before the due date, and daily once overdue with an estimate of the late-payment exposure
(``StatutoryTaxRate vat_late_payment_monthly``, display only, unverified); daily once a filed return's payment is
overdue; for amendment corrections, alerts before and after ``notify_by`` (R27). No weekend or holiday shift (OQ-8),
in-app only (OQ-9). Idempotent: one alert per period (or correction), kind and day.
"""
import math
from decimal import Decimal

from django.db import IntegrityError, transaction

from . import services
from .models import FILED_STATUSES, TaxAlert, VatCorrection, VatReturn, VatReturnSchedule

ZERO = Decimal("0")


def _exposure(ret, on, due):
    """Late-payment exposure estimate: open amount x monthly rate x started months late [CC-verify]."""
    rate = services._late_payment_rate(on)
    if ret is None or rate is None or ret.net_vat_sar <= ZERO:
        return None
    months = math.ceil((on - due).days / 30)
    base = services._open_amount(ret) if ret.status in FILED_STATUSES else ret.net_vat_sar
    return services._q(max(ZERO, base) * rate.rate / Decimal("100") * months)


def _alert(entity, kind, on, due, en, ar, *, schedule=None, correction=None, exposure=None):
    alert = TaxAlert(entity=entity, kind=kind, schedule=schedule, correction=correction, alert_date=on, due_date=due,
                     message_en=en[:500], message_ar=ar[:500], exposure_sar=exposure, created_at=services._now())
    alert._service_write = True
    try:
        with transaction.atomic():
            alert.save()
    except IntegrityError:  # already raised today
        return None
    return alert


def deadline_alerts(*, today=None):
    on = today or services._today()
    created = []
    for row in VatReturnSchedule.objects.select_related("entity").filter(period_end__lt=on).order_by("pk"):
        entity = row.entity
        if not entity.is_active:
            continue
        days = services._settings(entity).alert_days
        latest = VatReturn.objects.filter(entity=entity, period_start=row.period_start).order_by("-version").first()
        filed = latest is not None and latest.status in FILED_STATUSES
        to_due = (row.due_date - on).days
        period = f"{row.period_start}..{row.period_end}"
        if not filed:
            if to_due in days:
                created.append(_alert(
                    entity, TaxAlert.Kind.VAT_DUE_SOON, on, row.due_date, schedule=row,
                    en=f"{entity.code}: the VAT return {period} is due on {row.due_date} ({to_due} days).",
                    ar=f"{entity.code}: يستحق الإقرار الضريبي للفترة {period} في {row.due_date} (بعد {to_due} يوم)."))
            elif to_due < 0:
                exposure = _exposure(latest, on, row.due_date)
                estimate = f" Estimated late-payment exposure SAR {exposure} (unverified)." if exposure is not None else ""
                estimate_ar = f" تقدير غرامة التأخير {exposure} ريال (غير متحقق منه)." if exposure is not None else ""
                created.append(_alert(
                    entity, TaxAlert.Kind.VAT_OVERDUE, on, row.due_date, schedule=row, exposure=exposure,
                    en=f"{entity.code}: the VAT return {period} was due on {row.due_date} and is not filed.{estimate}",
                    ar=f"{entity.code}: كان الإقرار الضريبي للفترة {period} مستحقًا في {row.due_date} ولم يُقدَّم."
                       f"{estimate_ar}"))
        elif to_due < 0 and services._open_amount(latest) > ZERO:
            exposure = _exposure(latest, on, row.due_date)
            created.append(_alert(
                entity, TaxAlert.Kind.VAT_PAYMENT_OVERDUE, on, row.due_date, schedule=row, exposure=exposure,
                en=f"{entity.code}: VAT of {period} (SAR {services._open_amount(latest)}) was due on {row.due_date} and "
                   f"is not paid. Estimated exposure SAR {exposure} (unverified).",
                ar=f"{entity.code}: ضريبة الفترة {period} مستحقة منذ {row.due_date} ولم تُسدَّد. تقدير الغرامة "
                   f"{exposure} ريال (غير متحقق منه)."))
    for corr in VatCorrection.objects.select_related("entity").filter(
            treatment=VatCorrection.Treatment.AMENDMENT, notify_by__isnull=False,
            status__in=[VatCorrection.Status.OPEN, VatCorrection.Status.ASSIGNED]):
        to_due = (corr.notify_by - on).days
        if to_due in services._settings(corr.entity).alert_days or to_due < 0 or corr.discovered_on == on:
            created.append(_alert(
                corr.entity, TaxAlert.Kind.CORRECTION_NOTIFY, on, corr.notify_by, correction=corr,
                en=f"{corr.entity.code}: underdeclared VAT SAR {corr.tax_difference_sar} needs a voluntary disclosure "
                   f"by {corr.notify_by}.",
                ar=f"{corr.entity.code}: نقص في الضريبة المقرّة قدره {corr.tax_difference_sar} ريال يتطلب إفصاحًا "
                   f"طوعيًا قبل {corr.notify_by}."))
    return [a for a in created if a is not None]
