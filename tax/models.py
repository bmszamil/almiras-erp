"""Tax returns, VAT slice (docs/specs/tax-returns.md §3.1, 3.2, 3.5): VAT return periods, returns, boxes, source
lines, exceptions, corrections, import VAT records, payments and refunds, deadline alerts.

Every business record has an ``entity`` (core.Entity, PROTECT); ``StatutoryTaxRate`` and ``VatBoxDefinition`` are
global reference data. Records change only through ``tax.services``, which sets ``obj._service_write = True`` right
before the one save it makes; bulk writes and deletes are refused here and, on PostgreSQL, by the triggers of
migration 0003. A return that is ``approved`` or later is locked: its boxes, source lines, adjustments and
acknowledgements never change (R7). Money is ``Decimal``: SAR amounts ``Decimal(18,2)``.

WHT returns, Zakat and income tax (§3.3, 3.4) are not part of this slice.
"""
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from core.models import Entity, bi

MONEY = dict(max_digits=18, decimal_places=2)
ZERO = Decimal("0")


class TaxError(ValidationError):
    """A tax rule is broken. Messages are "English / العربية"."""

    def __init__(self, message, code=None, params=None):
        super().__init__(message, code=code, params=params)
        self.code = code


def error(en, ar, code=None):
    return TaxError(f"{en} / {ar}", code=code)


SERVICE_ONLY = ("This tax record changes only through tax.services.", "يتغير هذا السجل الضريبي من خلال خدمات الضرائب فقط.")
LOCKED = ("This VAT return is approved and locked: it is never recomputed, edited or deleted (R7).",
          "هذا الإقرار الضريبي معتمد ومقفل: لا يُعاد احتسابه ولا يُعدَّل ولا يُحذف.")
APPEND_ONLY = ("This tax record is append-only: it is never changed or deleted.",
               "هذا السجل الضريبي للإضافة فقط: لا يُعدَّل ولا يُحذف.")


def _service_only():
    return error(*SERVICE_ONLY, code="service_only")


def _locked():
    return error(*LOCKED, code="return_locked")


class ServiceQuerySet(models.QuerySet):
    """Bulk writes skip save()/delete(): refused, records change one by one through tax.services."""

    def update(self, **kwargs):
        raise _service_only()

    def delete(self):
        raise _service_only()

    def bulk_update(self, objs, fields, *args, **kwargs):
        raise _service_only()

    def bulk_create(self, objs, *args, **kwargs):
        raise _service_only()


def changed_fields(obj):
    """Concrete fields (attnames) whose value differs from the stored row; empty for a new object."""
    if obj.pk is None:
        return set()
    names = [f.attname for f in obj._meta.concrete_fields]
    stored = type(obj)._base_manager.filter(pk=obj.pk).values(*names).first()
    if stored is None:
        return set()
    out = set()
    for name in names:
        value = getattr(obj, name)
        if hasattr(value, "name") and not isinstance(value, str):  # FieldFile: compare the stored name
            value = value.name or ""
        if stored[name] != value and not (stored[name] in (None, "") and value in (None, "")):
            out.add(name)
    return out


class ServiceWritten(models.Model):
    """Saved only by tax.services (``_service_write``); never deleted (R32 amendment: kept indefinitely)."""

    objects = ServiceQuerySet.as_manager()

    class Meta:
        abstract = True

    # Subclasses that allow the service to delete some rows (drafts) list the statuses here.
    deletable_statuses = ()

    def save(self, *args, **kwargs):
        allowed, self._service_write = getattr(self, "_service_write", False), False
        if not allowed:
            raise _service_only()
        if self.pk is not None and hasattr(self, "entity_id"):
            stored = type(self)._base_manager.filter(pk=self.pk).values_list("entity_id", flat=True).first()
            if stored is not None and stored != self.entity_id:
                raise error("A tax record never moves to another entity.", "لا ينتقل السجل الضريبي إلى كيان آخر.",
                            code="entity_fixed")
        self.check_change()
        super().save(*args, **kwargs)

    def check_change(self):
        """Hook: refuse changes the record's state does not allow (mirrors the PostgreSQL triggers)."""

    def delete(self, *args, **kwargs):
        allowed, self._service_write = getattr(self, "_service_write", False), False
        stored = type(self)._base_manager.filter(pk=self.pk).values_list("status", flat=True).first() \
            if "status" in {f.name for f in self._meta.fields} else None
        if not allowed or stored not in self.deletable_statuses:
            raise _service_only()
        return super().delete(*args, **kwargs)


# --- settings and reference data (3.1) --------------------------------------------------------------

def default_alert_days():
    return [15, 5, 1]


class TaxSettings(ServiceWritten):
    """Per-entity tax settings (3.1). Changed through ``tax.services.change_tax_settings`` (approver, logged).

    VAT slice: only the VAT fields; fiscal year, Zakat and WHT fields come with those parts."""

    entity = models.OneToOneField(Entity, on_delete=models.PROTECT, related_name="tax_settings",
                                  verbose_name=bi("Entity", "الكيان"))
    vat_quarter_start_month = models.PositiveSmallIntegerField(
        bi("First month of a VAT quarter", "الشهر الأول من الربع الضريبي"), default=1,
        validators=[MinValueValidator(1), MaxValueValidator(12)],
        help_text=bi("1 = calendar quarters (OQ-5 default)", "1 = الأرباع التقويمية"),
    )
    alert_days = models.JSONField(bi("Alert days before the due date", "أيام التنبيه قبل تاريخ الاستحقاق"),
                                  default=default_alert_days)
    vat_correction_threshold_sar = models.DecimalField(
        bi("VAT correction threshold (SAR)", "حد تصحيح ضريبة القيمة المضافة (ريال)"), default=Decimal("15000.00"),
        help_text=bi("PSG §13.5 p.53 (VAT IR Art. 63)", "الدليل §13.5 (المادة 63 من اللائحة)"), **MONEY,
    )
    vat_golive_date = models.DateField(
        bi("VAT go-live date in the ERP", "تاريخ بدء الإقرارات الضريبية في النظام"), null=True, blank=True,
        help_text=bi("Owner decision G4: returns start here; earlier periods were filed outside the ERP (legacy "
                     "imports only, no alerts). Must be the start of a VAT return period.",
                     "قرار المالك G4: تبدأ الإقرارات من هذا التاريخ؛ الفترات السابقة قُدّمت خارج النظام."),
    )

    class Meta:
        verbose_name = bi("tax settings", "إعدادات الضرائب")
        verbose_name_plural = bi("tax settings", "إعدادات الضرائب")

    def __str__(self):
        return f"{self.entity.code} tax settings"


class StatutoryTaxRate(models.Model):
    """Global statutory rates, as dated data (3.1). Unverified rows are marked "unverified" wherever used (R6)."""

    class Code(models.TextChoices):
        VAT_LATE_PAYMENT_MONTHLY = "vat_late_payment_monthly", bi("VAT late payment (per month)",
                                                                   "غرامة التأخر في سداد ضريبة القيمة المضافة (شهريًا)")
        VAT_LATE_FILING_MIN = "vat_late_filing_min", bi("VAT late filing, minimum", "غرامة التأخر في تقديم الإقرار، الحد الأدنى")
        VAT_LATE_FILING_MAX = "vat_late_filing_max", bi("VAT late filing, maximum", "غرامة التأخر في تقديم الإقرار، الحد الأعلى")
        VAT_INCORRECT_RETURN = "vat_incorrect_return", bi("Incorrect or amended VAT return", "إقرار خاطئ أو معدَّل")

    code = models.CharField(bi("Code", "الرمز"), max_length=40, choices=Code.choices)
    rate = models.DecimalField(bi("Rate %", "النسبة %"), max_digits=7, decimal_places=4)
    valid_from = models.DateField(bi("Valid from", "ساري من"))
    valid_to = models.DateField(bi("Valid to", "ساري حتى"), null=True, blank=True)
    source_citation = models.CharField(bi("Source", "المصدر"), max_length=300)
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=bi("Verified by", "تحقق منه"))
    verified_at = models.DateTimeField(bi("Verified at", "تاريخ التحقق"), null=True, blank=True)

    class Meta:
        verbose_name = bi("statutory tax rate", "نسبة ضريبية نظامية")
        verbose_name_plural = bi("statutory tax rates", "النسب الضريبية النظامية")
        constraints = [models.UniqueConstraint(fields=["code", "valid_from"], name="tax_rate_one_per_start")]

    def __str__(self):
        return f"{self.code} {self.rate}% from {self.valid_from}"


class VatBoxDefinition(models.Model):
    """One box of the ZATCA VAT return form, versioned by ``valid_from`` (3.1, 3.2). Global.

    Seeded unverified (migration 0002): every number and label is [verify] until checked against the current
    ZATCA VAT return form (OQ-2); returns using an unverified definition say so (R6)."""

    class Side(models.TextChoices):
        OUTPUT = "output", bi("Sales (output)", "المبيعات (المخرجات)")
        INPUT = "input", bi("Purchases (input)", "المشتريات (المدخلات)")
        SUMMARY = "summary", bi("Summary", "الملخص")

    box_no = models.PositiveSmallIntegerField(bi("Box", "الخانة"), validators=[MinValueValidator(1),
                                                                                MaxValueValidator(16)])
    label_en = models.CharField(bi("Label (English)", "التسمية (بالإنجليزية)"), max_length=200)
    label_ar = models.CharField(bi("Label (Arabic)", "التسمية (بالعربية)"), max_length=200)
    side = models.CharField(bi("Side", "الجانب"), max_length=8, choices=Side.choices)
    has_amount = models.BooleanField(bi("Amount column", "عمود المبلغ"), default=True)
    has_adjustment = models.BooleanField(bi("Adjustment column", "عمود التعديلات"), default=True)
    has_vat = models.BooleanField(bi("VAT column", "عمود الضريبة"), default=True)
    mapping_key = models.CharField(bi("Rule", "القاعدة"), max_length=40)
    verify_note = models.CharField(bi("To verify", "للتحقق"), max_length=300, blank=True)
    valid_from = models.DateField(bi("Valid from", "ساري من"))
    valid_to = models.DateField(bi("Valid to", "ساري حتى"), null=True, blank=True)
    # Review item 12: one person proposes the verification (draft), another approves it.
    proposed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=bi("Verification proposed by", "اقترح التحقق"))
    proposed_at = models.DateTimeField(bi("Verification proposed at", "تاريخ اقتراح التحقق"), null=True, blank=True)
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=bi("Verified by", "تحقق منه"))
    verified_at = models.DateTimeField(bi("Verified at", "تاريخ التحقق"), null=True, blank=True)

    class Meta:
        verbose_name = bi("VAT return box", "خانة الإقرار الضريبي")
        verbose_name_plural = bi("VAT return boxes", "خانات الإقرار الضريبي")
        ordering = ["box_no", "valid_from"]
        constraints = [models.UniqueConstraint(fields=["box_no", "valid_from"], name="vat_box_one_per_start")]

    def __str__(self):
        return f"{self.box_no} {self.label_en}"


# --- VAT return periods and returns (3.2) -----------------------------------------------------------

class Frequency(models.TextChoices):
    MONTHLY = "monthly", bi("Monthly", "شهري")
    QUARTERLY = "quarterly", bi("Quarterly", "ربع سنوي")


class VatFrequencyChange(ServiceWritten):
    """A dated change of an entity's VAT return frequency (R4, review item 6); append-only.

    Written when ``Entity.vat_return_period`` changes through ``core.services.change_entity_settings``. The
    schedule applies ``frequency`` to every period that starts after ``changed_on`` and has no return, whenever
    the schedule is generated."""

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="vat_frequency_changes",
                               verbose_name=bi("Entity", "الكيان"))
    previous_frequency = models.CharField(bi("Frequency before", "الدورية السابقة"), max_length=10,
                                          choices=Frequency.choices)
    frequency = models.CharField(bi("Frequency after", "الدورية الجديدة"), max_length=10, choices=Frequency.choices)
    changed_on = models.DateField(bi("Changed on", "تاريخ التغيير"))
    reason = models.CharField(bi("Reason", "السبب"), max_length=500, blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=bi("Recorded by", "سجّله"))
    recorded_at = models.DateTimeField(bi("Recorded at", "تاريخ التسجيل"))

    class Meta:
        verbose_name = bi("VAT frequency change", "تغيير دورية الإقرار الضريبي")
        verbose_name_plural = bi("VAT frequency changes", "تغييرات دورية الإقرار الضريبي")
        ordering = ["entity", "changed_on", "pk"]

    def check_change(self):
        if self.pk is not None and type(self)._base_manager.filter(pk=self.pk).exists():
            raise error(*APPEND_ONLY, code="append_only")


class VatReturnSchedule(ServiceWritten):
    """One VAT return period of an entity (R3, R4). Generated; a row with a return never changes or goes."""

    deletable_statuses = (None,)  # rows without a return are regenerated by the service (R4)

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="vat_schedule",
                               verbose_name=bi("Entity", "الكيان"))
    period_start = models.DateField(bi("Period start", "بداية الفترة"))
    period_end = models.DateField(bi("Period end", "نهاية الفترة"))
    due_date = models.DateField(bi("Due date", "تاريخ الاستحقاق"))
    frequency = models.CharField(bi("Frequency", "الدورية"), max_length=10, choices=Frequency.choices)

    class Meta:
        verbose_name = bi("VAT return period", "فترة الإقرار الضريبي")
        verbose_name_plural = bi("VAT return periods", "فترات الإقرار الضريبي")
        ordering = ["entity", "period_start"]
        constraints = [
            models.UniqueConstraint(fields=["entity", "period_start"], name="vat_schedule_one_per_start"),
            models.CheckConstraint(condition=models.Q(period_end__gte=models.F("period_start")),
                                   name="vat_schedule_end_after_start"),
        ]

    def check_change(self):
        if self.pk is not None and changed_fields(self) and self.returns.exists():
            raise error("A VAT return period that has a return never changes (R3).",
                        "لا تتغير فترة إقرار لها إقرار ضريبي.", code="schedule_has_return")
        overlap = VatReturnSchedule._base_manager.filter(
            entity_id=self.entity_id, period_start__lte=self.period_end, period_end__gte=self.period_start,
        ).exclude(pk=self.pk)
        if overlap.exists():
            raise error("VAT return periods never overlap (R3).", "لا تتداخل فترات الإقرار الضريبي.",
                        code="schedule_overlap")

    def __str__(self):
        return f"{self.entity.code} {self.period_start}..{self.period_end}"


class ReturnStatus(models.TextChoices):
    DRAFT = "draft", bi("Draft (being prepared)", "مسودة (قيد الإعداد)")
    IN_REVIEW = "in_review", bi("Prepared, in review", "مُعدّ، قيد المراجعة")
    REVIEWED = "reviewed", bi("Reviewed", "تمت المراجعة")
    APPROVED = "approved", bi("Approved by the CFO (locked)", "معتمد من المدير المالي (مقفل)")
    FILED = "filed", bi("Filed with ZATCA (locked)", "مقدم إلى الهيئة (مقفل)")
    SETTLED = "settled", bi("Settled (locked)", "مسدد (مقفل)")
    AMENDED = "amended", bi("Amended (locked)", "معدَّل (مقفل)")


LOCKED_STATUSES = frozenset({ReturnStatus.APPROVED, ReturnStatus.FILED, ReturnStatus.SETTLED, ReturnStatus.AMENDED})
FILED_STATUSES = frozenset({ReturnStatus.FILED, ReturnStatus.SETTLED, ReturnStatus.AMENDED})
# Fields that change after approval, per transition (R7); mirrored by the PostgreSQL trigger (migration 0003).
FILING_FIELDS = frozenset({"status", "filed_by_id", "filed_at", "zatca_filing_reference", "filed_on",
                           "acknowledgement", "sadad_bill_number"})


class VatReturn(ServiceWritten):
    """A VAT return of one entity and period (3.2). Version 0 = original, 1+ = amendment (R28)."""

    class Source(models.TextChoices):
        HUMAN = "human", bi("Prepared in the ERP", "أُعدّ في النظام")
        IMPORT = "import", bi("Filed before go-live (imported)", "قُدّم قبل التشغيل (مستورد)")
        AGENT = "agent", bi("Drafted by an AI agent", "أعدّ مسودته وكيل ذكاء اصطناعي")

    Status = ReturnStatus
    deletable_statuses = ()

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="vat_returns",
                               verbose_name=bi("Entity", "الكيان"))
    # Null only for a legacy return imported for a period before the entity's VAT go-live date (G4, R12).
    schedule = models.ForeignKey(VatReturnSchedule, null=True, blank=True, on_delete=models.PROTECT,
                                 related_name="returns", verbose_name=bi("Return period", "فترة الإقرار"))
    period_start = models.DateField(bi("Period start", "بداية الفترة"))
    period_end = models.DateField(bi("Period end", "نهاية الفترة"))
    frequency = models.CharField(bi("Frequency", "الدورية"), max_length=10, choices=Frequency.choices)
    version = models.PositiveSmallIntegerField(bi("Version", "النسخة"), default=0)
    amends = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="amendments",
                               verbose_name=bi("Amends", "يعدّل"))
    due_date = models.DateField(bi("Due date", "تاريخ الاستحقاق"))
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=ReturnStatus.choices,
                              default=ReturnStatus.DRAFT)
    source = models.CharField(bi("Source", "المصدر"), max_length=8, choices=Source.choices, default=Source.HUMAN)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
                                   verbose_name=bi("Created by", "أنشأه"))
    created_at = models.DateTimeField(bi("Created at", "تاريخ الإنشاء"))
    modified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=bi("Last recomputed or changed by", "آخر من عدّله"))
    modified_at = models.DateTimeField(bi("Last changed at", "تاريخ آخر تعديل"), null=True, blank=True)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+", verbose_name=bi("Prepared (submitted) by", "أعدّه وقدّمه"))
    submitted_at = models.DateTimeField(bi("Submitted at", "تاريخ التقديم"), null=True, blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=bi("Reviewed by", "راجعه"))
    reviewed_at = models.DateTimeField(bi("Reviewed at", "تاريخ المراجعة"), null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=bi("Approved by", "اعتمده"))
    approved_at = models.DateTimeField(bi("Approved at", "تاريخ الاعتماد"), null=True, blank=True)
    filed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                 related_name="+", verbose_name=bi("Filing recorded by", "سجّل التقديم"))
    filed_at = models.DateTimeField(bi("Filing recorded at", "تاريخ تسجيل التقديم"), null=True, blank=True)
    returned_reason_en = models.CharField(bi("Returned to draft because (English)", "سبب الإرجاع (بالإنجليزية)"),
                                          max_length=500, blank=True)
    returned_reason_ar = models.CharField(bi("Returned to draft because (Arabic)", "سبب الإرجاع (بالعربية)"),
                                          max_length=500, blank=True)

    zatca_filing_reference = models.CharField(bi("ZATCA filing reference", "مرجع التقديم لدى الهيئة"), max_length=60,
                                              blank=True)
    filed_on = models.DateField(bi("Filed on", "تاريخ التقديم"), null=True, blank=True)
    acknowledgement = models.FileField(bi("Portal acknowledgement", "إيصال البوابة"), upload_to="tax/vat/%Y/",
                                       blank=True)
    sadad_bill_number = models.CharField(bi("SADAD bill number", "رقم فاتورة سداد"), max_length=30, blank=True)

    output_vat_sar = models.DecimalField(bi("Output VAT (SAR)", "ضريبة المخرجات (ريال)"), default=ZERO, **MONEY)
    input_vat_sar = models.DecimalField(bi("Recoverable input VAT (SAR)", "ضريبة المدخلات القابلة للاسترداد (ريال)"),
                                        default=ZERO, **MONEY)
    rc_output_vat_sar = models.DecimalField(
        bi("Reverse-charge output VAT in box 13 (SAR)", "ضريبة الاحتساب العكسي ضمن الخانة 13 (ريال)"), default=ZERO,
        **MONEY)
    net_vat_sar = models.DecimalField(bi("Net VAT due (box 16, SAR)", "صافي الضريبة المستحقة (الخانة 16، ريال)"),
                                      default=ZERO, **MONEY)
    refund_requested = models.BooleanField(bi("Refund requested", "طُلب الاسترداد"), default=False)
    refund_requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
        verbose_name=bi("Refund choice made by", "حدّد طلب الاسترداد"),
        help_text=bi("Counts as a preparer (review item 10)", "يُعدّ من المُعدّين"))
    source_hash = models.CharField(bi("Source hash", "بصمة المصادر"), max_length=64, blank=True)
    settlement_offset_sar = models.DecimalField(bi("Offset posted at approval (T1)", "المقاصة المرحّلة عند الاعتماد"),
                                                default=ZERO, **MONEY)
    settlement_entry = models.ForeignKey("ledger.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                         related_name="+", verbose_name=bi("Offset entry (T1)", "قيد المقاصة"))
    notes_en = models.TextField(bi("Notes (English)", "ملاحظات (بالإنجليزية)"), blank=True)
    notes_ar = models.TextField(bi("Notes (Arabic)", "ملاحظات (بالعربية)"), blank=True)

    class Meta:
        verbose_name = bi("VAT return", "إقرار ضريبة القيمة المضافة")
        verbose_name_plural = bi("VAT returns", "إقرارات ضريبة القيمة المضافة")
        ordering = ["entity", "-period_start", "-version"]
        constraints = [
            models.UniqueConstraint(fields=["entity", "period_start", "version"], name="vat_return_one_per_version"),
            models.CheckConstraint(
                condition=(models.Q(version=0) & models.Q(amends__isnull=True))
                | (models.Q(version__gt=0) & models.Q(amends__isnull=False)),
                name="vat_return_amendment_links_original",
            ),
            models.CheckConstraint(condition=models.Q(schedule__isnull=False) | models.Q(source="import"),
                                   name="vat_return_schedule_unless_import"),
        ]

    @property
    def is_locked(self):
        return self.status in LOCKED_STATUSES

    def _check_filing_fields(self):
        """Review item 9: a return becomes ``filed`` only with its ZATCA reference, date and filer (and the portal
        receipt unless it is a legacy import, R12)."""
        if not ((self.zatca_filing_reference or "").strip() and self.filed_on and self.filed_by_id
                and (self.source == self.Source.IMPORT or self.acknowledgement)):
            raise error("A filed VAT return needs its ZATCA filing reference, filing date, filer and portal receipt.",
                        "يحتاج الإقرار المقدَّم إلى مرجع التقديم وتاريخه ومن سجّله وإيصال البوابة.",
                        code="filing_incomplete")

    def check_change(self):
        stored = type(self)._base_manager.filter(pk=self.pk).values("status", "settlement_entry_id").first() \
            if self.pk else None
        if stored is None:
            if self.status != ReturnStatus.DRAFT:  # review item 9: every return starts as a draft
                raise error("A VAT return is created as a draft; it reaches later statuses only through its "
                            "lifecycle.", "يُنشأ الإقرار الضريبي مسودةً ولا يصل إلى الحالات اللاحقة إلا عبر دورته.",
                            code="insert_not_draft")
            return
        before, after = stored["status"], self.status
        if after == ReturnStatus.FILED and before != ReturnStatus.FILED and before != ReturnStatus.SETTLED:
            self._check_filing_fields()
            if before != ReturnStatus.APPROVED and not (before == ReturnStatus.REVIEWED
                                                        and self.source == self.Source.IMPORT
                                                        and self.approved_by_id and self.reviewed_by_id):
                raise _locked()
        if before not in LOCKED_STATUSES and after in LOCKED_STATUSES and after != ReturnStatus.FILED:
            if not (before == ReturnStatus.REVIEWED and after == ReturnStatus.APPROVED and self.approved_by_id):
                raise _locked()
        if before not in LOCKED_STATUSES:
            return
        changed = changed_fields(self)
        if not changed:
            return
        if before == ReturnStatus.AMENDED:
            raise _locked()
        if before == after == ReturnStatus.APPROVED and changed <= {"settlement_entry_id"} \
                and stored["settlement_entry_id"] is None:
            return
        if before == ReturnStatus.APPROVED and after == ReturnStatus.FILED and changed <= FILING_FIELDS:
            return
        if (before, after) in {(ReturnStatus.FILED, ReturnStatus.SETTLED), (ReturnStatus.SETTLED, ReturnStatus.FILED),
                               (ReturnStatus.FILED, ReturnStatus.AMENDED), (ReturnStatus.SETTLED, ReturnStatus.AMENDED)} \
                and changed <= {"status"}:
            return
        raise _locked()

    def __str__(self):
        version = f" v{self.version}" if self.version else ""
        return f"{self.entity.code} VAT {self.period_start}..{self.period_end}{version} ({self.status})"


class ReturnChild(ServiceWritten):
    """Rows of a return that never change once the return is locked (R7, R10)."""

    deletable_statuses = ()

    class Meta:
        abstract = True

    def _return_locked(self):
        return VatReturn._base_manager.filter(pk=self.vat_return_id, status__in=LOCKED_STATUSES).exists()

    def check_change(self):
        if self._return_locked():
            raise _locked()

    def delete(self, *args, **kwargs):
        allowed, self._service_write = getattr(self, "_service_write", False), False
        if not allowed:
            raise _service_only()
        if self._return_locked():
            raise _locked()
        return models.Model.delete(self, *args, **kwargs)


class Column(models.TextChoices):
    AMOUNT = "amount", bi("Amount", "المبلغ")
    ADJUSTMENT = "adjustment", bi("Adjustment", "التعديلات")
    VAT = "vat", bi("VAT", "الضريبة")


class VatReturnBox(ReturnChild):
    """One computed box of a return (3.2); never edited by users."""

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    vat_return = models.ForeignKey(VatReturn, on_delete=models.PROTECT, related_name="boxes",
                                   verbose_name=bi("VAT return", "الإقرار"))
    box_no = models.PositiveSmallIntegerField(bi("Box", "الخانة"))
    label_en = models.CharField(bi("Label (English)", "التسمية (بالإنجليزية)"), max_length=200)
    label_ar = models.CharField(bi("Label (Arabic)", "التسمية (بالعربية)"), max_length=200)
    amount_sar = models.DecimalField(bi("Amount (SAR)", "المبلغ (ريال)"), default=ZERO, **MONEY)
    adjustment_sar = models.DecimalField(bi("Adjustment (SAR)", "التعديلات (ريال)"), default=ZERO, **MONEY)
    vat_sar = models.DecimalField(bi("VAT (SAR)", "الضريبة (ريال)"), default=ZERO, **MONEY)
    verified = models.BooleanField(bi("Definition verified", "التعريف متحقق منه"), default=False)

    class Meta:
        verbose_name = bi("VAT return box value", "قيمة خانة الإقرار")
        verbose_name_plural = bi("VAT return box values", "قيم خانات الإقرار")
        ordering = ["vat_return", "box_no"]
        constraints = [models.UniqueConstraint(fields=["vat_return", "box_no"], name="vat_return_box_once")]


class VatReturnSourceLine(ReturnChild):
    """What a box column is made of (R11): one document, record, correction or adjustment amount."""

    class SourceType(models.TextChoices):
        SALES_DOCUMENT = "sales_document", bi("Sales document", "مستند مبيعات")
        PURCHASE_DOCUMENT = "purchase_document", bi("Purchase document", "مستند مشتريات")
        IMPORT_RECORD = "import_record", bi("Import VAT record", "سجل ضريبة الاستيراد")
        CORRECTION = "correction", bi("Correction of an earlier period", "تصحيح فترة سابقة")
        MANUAL_ADJUSTMENT = "manual_adjustment", bi("Manual adjustment", "تعديل يدوي")
        CARRIED_CREDIT = "carried_credit", bi("Credit carried forward", "رصيد دائن مرحّل")
        PRIOR_RETURN = "prior_return", bi("Figures of the return amended", "أرقام الإقرار المعدَّل")
        ART51_TRUE_UP = "art51_true_up", bi("Art. 51 annual true-up", "التسوية السنوية وفق المادة 51")
        REVERSE_CHARGE_OUTPUT = "reverse_charge_output", bi("Reverse-charge output VAT", "ضريبة مخرجات الاحتساب العكسي")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    vat_return = models.ForeignKey(VatReturn, on_delete=models.PROTECT, related_name="source_lines",
                                   verbose_name=bi("VAT return", "الإقرار"))
    box_no = models.PositiveSmallIntegerField(bi("Box", "الخانة"))
    column = models.CharField(bi("Column", "العمود"), max_length=10, choices=Column.choices)
    source_type = models.CharField(bi("Source", "المصدر"), max_length=24, choices=SourceType.choices)
    source_id = models.PositiveBigIntegerField(bi("Source id", "رقم المصدر"), null=True, blank=True)
    document_number = models.CharField(bi("Document number", "رقم المستند"), max_length=60, blank=True)
    journal_entry = models.ForeignKey("ledger.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                      related_name="+", verbose_name=bi("Journal entry", "القيد اليومي"))
    date = models.DateField(bi("Date", "التاريخ"), null=True, blank=True)
    amount_sar = models.DecimalField(bi("Amount (SAR)", "المبلغ (ريال)"), **MONEY)
    note = models.CharField(bi("Note", "ملاحظة"), max_length=200, blank=True)

    class Meta:
        verbose_name = bi("VAT return source line", "بند مصدر الإقرار")
        verbose_name_plural = bi("VAT return source lines", "بنود مصادر الإقرار")
        ordering = ["vat_return", "box_no", "column", "date", "pk"]


class VatManualAdjustment(ReturnChild):
    """A manual amount on a draft return, with a bilingual reason and evidence (3.2, R15)."""

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    vat_return = models.ForeignKey(VatReturn, on_delete=models.PROTECT, related_name="manual_adjustments",
                                   verbose_name=bi("VAT return", "الإقرار"))
    box_no = models.PositiveSmallIntegerField(bi("Box", "الخانة"))
    column = models.CharField(bi("Column", "العمود"), max_length=10, choices=Column.choices)
    amount_sar = models.DecimalField(bi("Amount (SAR, signed)", "المبلغ (ريال، بإشارة)"), **MONEY)
    reason_en = models.CharField(bi("Reason (English)", "السبب (بالإنجليزية)"), max_length=500, blank=True)
    reason_ar = models.CharField(bi("Reason (Arabic)", "السبب (بالعربية)"), max_length=500)
    evidence = models.FileField(bi("Evidence", "المستند المؤيد"), upload_to="tax/vat/adjustments/%Y/")
    journal_lines = models.ManyToManyField("ledger.JournalLine", blank=True, related_name="+",
                                           verbose_name=bi("Journal lines it explains", "بنود القيود التي يفسرها"))
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
                                   verbose_name=bi("Created by", "أنشأه"))
    created_at = models.DateTimeField(bi("Created at", "تاريخ الإنشاء"))

    class Meta:
        verbose_name = bi("VAT manual adjustment", "تعديل يدوي على الإقرار")
        verbose_name_plural = bi("VAT manual adjustments", "التعديلات اليدوية على الإقرار")


class ExceptionKind(models.TextChoices):
    UNTAGGED_VAT_JOURNAL = "untagged_vat_journal", bi("Journal on a VAT account without a tax document",
                                                      "قيد على حساب ضريبة بلا مستند ضريبي")
    REJECTED_DOCUMENT = "rejected_document", bi("Sales document rejected by ZATCA", "مستند مبيعات رفضته الهيئة")
    TIE_OUT_DIFFERENCE = "tie_out_difference", bi("Difference to the VAT accounts", "فرق مع حسابات الضريبة")
    # Review fixes (tax-returns §10.4). Kinds marked "blocking" cannot be acknowledged; they are fixed at source.
    TAX_POINT_FALLBACK = "tax_point_fallback", bi("Sales put in the period of their issue date (no tax point)",
                                                  "مبيعات أُدرجت بتاريخ إصدارها (لا يوجد تاريخ استحقاق الضريبة)")
    PERIOD_SHIFT = "period_shift", bi("Document reported in another period than its journal date",
                                      "مستند أُدرج في فترة غير فترة قيده")
    SAR_RATE_MISSING = "sar_rate_missing", bi("Foreign-currency sale without a SAR rate",
                                              "بيع بعملة أجنبية دون سعر صرف بالريال")
    PURCHASE_SAR_VAT_MISSING = "purchase_sar_vat_missing", bi("Foreign-currency bill without the SAR VAT of the "
                                                              "supplier", "فاتورة مورد بعملة أجنبية دون الضريبة بالريال")
    CORRECTING_ENTRY = "correcting_entry", bi("Entry left out as a correcting entry", "قيد استُبعد كقيد تصحيح")
    CORRECTION_MISMATCH = "correction_mismatch", bi("Correcting entries differ from the correction",
                                                    "قيود التصحيح لا تساوي فرق التصحيح")
    CORRECTION_WITHOUT_ENTRY = "correction_without_entry", bi("Correction without a correcting entry (blocking)",
                                                              "تصحيح دون قيد تصحيح (مانع)")
    CARRY_FORWARD_GAP = "carry_forward_gap", bi("Earlier period without an approved return (blocking)",
                                                "فترة سابقة بلا إقرار معتمد (مانع)")
    UNPAID_BILL_12_MONTHS = "unpaid_bill_12_months", bi("Input VAT on a bill unpaid after 12 months",
                                                        "ضريبة مدخلات على فاتورة لم تُسدد بعد 12 شهرًا")


BLOCKING_EXCEPTIONS = frozenset({ExceptionKind.TIE_OUT_DIFFERENCE, ExceptionKind.CORRECTION_WITHOUT_ENTRY,
                                 ExceptionKind.CARRY_FORWARD_GAP})


class VatExceptionAck(ReturnChild):
    """The reviewer's decision on one exception of a return (R15, R16)."""

    class Decision(models.TextChoices):
        TAGGED = "tagged", bi("Linked to a correction", "رُبط بتصحيح")
        NOT_RETURN_RELEVANT = "not_return_relevant", bi("Not relevant to the return", "لا يخص الإقرار")
        INCLUDED_BY_ADJUSTMENT = "included_by_adjustment", bi("Included by a manual adjustment", "أُدرج بتعديل يدوي")
        TAX_POINT_CONFIRMED = "tax_point_confirmed", bi("Period confirmed against the tax point",
                                                        "تأكدت الفترة من تاريخ استحقاق الضريبة")
        CONFIRMED_CORRECTION = "confirmed_correction", bi("Confirmed as a correcting entry", "تأكد أنه قيد تصحيح")
        DIFFERENCE_EXPLAINED = "difference_explained", bi("Difference explained", "تم تفسير الفرق")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    vat_return = models.ForeignKey(VatReturn, on_delete=models.PROTECT, related_name="exception_acks",
                                   verbose_name=bi("VAT return", "الإقرار"))
    kind = models.CharField(bi("Exception", "الاستثناء"), max_length=28, choices=ExceptionKind.choices)
    reference = models.CharField(bi("Reference", "المرجع"), max_length=60)
    decision = models.CharField(bi("Decision", "القرار"), max_length=24, choices=Decision.choices)
    reason_en = models.CharField(bi("Reason (English)", "السبب (بالإنجليزية)"), max_length=500, blank=True)
    reason_ar = models.CharField(bi("Reason (Arabic)", "السبب (بالعربية)"), max_length=500)
    acknowledged_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
                                        verbose_name=bi("Acknowledged by", "أقرّه"))
    acknowledged_at = models.DateTimeField(bi("Acknowledged at", "تاريخ الإقرار"))

    class Meta:
        verbose_name = bi("VAT exception acknowledgement", "إقرار استثناء ضريبي")
        verbose_name_plural = bi("VAT exception acknowledgements", "إقرارات الاستثناءات الضريبية")
        constraints = [models.UniqueConstraint(fields=["vat_return", "kind", "reference"], name="vat_ack_once")]


# --- registered ledger documents (3.2, 3.5) ----------------------------------------------------------

class DocStatus(models.TextChoices):
    DRAFT = "draft", bi("Draft", "مسودة")
    SUBMITTED = "submitted", bi("Submitted", "مقدَّم")
    POSTED = "posted", bi("Posted", "مرحّل")
    REVERSED = "reversed", bi("Reversed", "معكوس")


class TaxDocument(ServiceWritten):
    """A registered ledger document of this module: drafted and submitted by one person, approved (posted) by
    another (R34, access R35), corrected only by reversal (R36)."""

    Status = DocStatus
    deletable_statuses = ()

    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=DocStatus.choices, default=DocStatus.DRAFT)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
                                   verbose_name=bi("Created by", "أنشأه"))
    created_at = models.DateTimeField(bi("Created at", "تاريخ الإنشاء"))
    modified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=bi("Last modified by", "آخر من عدّله"))
    modified_at = models.DateTimeField(bi("Last modified at", "تاريخ آخر تعديل"), null=True, blank=True)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+", verbose_name=bi("Submitted by", "قدّمه"))
    submitted_at = models.DateTimeField(bi("Submitted at", "تاريخ التقديم"), null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=bi("Approved by", "اعتمده"))
    approved_at = models.DateTimeField(bi("Approved at", "تاريخ الاعتماد"), null=True, blank=True)
    journal_entry = models.ForeignKey("ledger.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                      related_name="+", verbose_name=bi("Journal entry", "القيد اليومي"))
    reversal_entry = models.ForeignKey("ledger.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                       related_name="+", verbose_name=bi("Reversal entry", "قيد العكس"))

    class Meta:
        abstract = True

    def check_change(self):
        stored = type(self)._base_manager.filter(pk=self.pk).values("status", "journal_entry_id").first() \
            if self.pk else None
        if stored is None:
            return
        changed = changed_fields(self)
        if not changed:
            return
        if stored["status"] == DocStatus.REVERSED:
            raise error("A reversed tax document never changes.", "لا يتغير المستند الضريبي المعكوس.", code="doc_locked")
        if stored["status"] == DocStatus.POSTED:
            ok = (changed <= {"journal_entry_id"} and stored["journal_entry_id"] is None) or (
                changed <= {"status", "reversal_entry_id"} and self.status == DocStatus.REVERSED
                and self.reversal_entry_id is not None)
            if not ok:
                raise error("A posted tax document is corrected only by reversal (R36).",
                            "يُصحَّح المستند الضريبي المرحّل بالعكس فقط.", code="doc_locked")


class ImportVatRecord(TaxDocument):
    """VAT paid at customs on an import (3.2, R22, T3). Paid by bank in this slice (``customs_broker``: OQ)."""

    class Attribution(models.TextChoices):
        TAXABLE = "taxable", bi("Used for taxable supplies", "مستخدم لتوريدات خاضعة")
        EXEMPT = "exempt", bi("Used for exempt supplies", "مستخدم لتوريدات معفاة")
        MIXED = "mixed", bi("Mixed use (to cost until Art. 51 ratios are built)", "استخدام مختلط (إلى التكلفة)")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="import_vat_records",
                               verbose_name=bi("Entity", "الكيان"))
    customs_declaration_no = models.CharField(bi("Customs declaration number", "رقم البيان الجمركي"), max_length=40)
    declaration_date = models.DateField(bi("Declaration date", "تاريخ البيان"))
    customs_value_sar = models.DecimalField(bi("Customs value (SAR)", "القيمة الجمركية (ريال)"), **MONEY)
    # C13: the import VAT base is the customs value plus customs duties and other charges [verify] (box 8 Amount).
    customs_duty_sar = models.DecimalField(bi("Customs duties (SAR)", "الرسوم الجمركية (ريال)"), default=ZERO, **MONEY)
    other_charges_sar = models.DecimalField(bi("Other charges in the VAT base (SAR)",
                                               "رسوم أخرى ضمن وعاء الضريبة (ريال)"), default=ZERO, **MONEY)
    vat_paid_sar = models.DecimalField(bi("VAT paid (SAR)", "الضريبة المدفوعة (ريال)"), **MONEY)
    attribution = models.CharField(bi("VAT attribution", "إسناد الضريبة"), max_length=8, choices=Attribution.choices)
    debit_account = models.ForeignKey("ledger.Account", null=True, blank=True, on_delete=models.PROTECT,
                                      related_name="+", verbose_name=bi("Cost or asset account", "حساب التكلفة أو الأصل"))
    bank_account = models.ForeignKey("ledger.Account", on_delete=models.PROTECT, related_name="+",
                                     verbose_name=bi("Bank account (GL)", "الحساب البنكي (الأستاذ)"))
    declaration_file = models.FileField(bi("Customs declaration", "البيان الجمركي"), upload_to="tax/imports/%Y/")

    class Meta:
        verbose_name = bi("import VAT record", "سجل ضريبة الاستيراد")
        verbose_name_plural = bi("import VAT records", "سجلات ضريبة الاستيراد")
        constraints = [models.UniqueConstraint(fields=["entity", "customs_declaration_no"],
                                               name="import_vat_declaration_once")]

    @property
    def vat_base_sar(self):
        return self.customs_value_sar + self.customs_duty_sar + self.other_charges_sar

    def __str__(self):
        return f"{self.entity.code} customs {self.customs_declaration_no} ({self.status})"


class TaxPayment(TaxDocument):
    """Payment of a VAT return (3.5, T2: Dr 2200 / Cr bank). VAT only in this slice."""

    class Kind(models.TextChoices):
        VAT = "vat", bi("VAT", "ضريبة القيمة المضافة")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="tax_payments",
                               verbose_name=bi("Entity", "الكيان"))
    kind = models.CharField(bi("Tax", "الضريبة"), max_length=10, choices=Kind.choices, default=Kind.VAT)
    vat_return = models.ForeignKey(VatReturn, on_delete=models.PROTECT, related_name="payments",
                                   verbose_name=bi("VAT return", "الإقرار"))
    amount_sar = models.DecimalField(bi("Amount (SAR)", "المبلغ (ريال)"), **MONEY)
    payment_date = models.DateField(bi("Payment date", "تاريخ الدفع"))
    bank_account = models.ForeignKey("ledger.Account", on_delete=models.PROTECT, related_name="+",
                                     verbose_name=bi("Bank account (GL)", "الحساب البنكي (الأستاذ)"))
    sadad_bill_number = models.CharField(bi("SADAD bill number", "رقم فاتورة سداد"), max_length=30, blank=True)
    bank_reference = models.CharField(bi("Bank reference", "مرجع البنك"), max_length=60, blank=True)

    class Meta:
        verbose_name = bi("tax payment", "دفعة ضريبية")
        verbose_name_plural = bi("tax payments", "الدفعات الضريبية")
        constraints = [models.CheckConstraint(condition=models.Q(amount_sar__gt=0), name="tax_payment_positive")]

    def __str__(self):
        return f"{self.entity.code} VAT payment {self.amount_sar} ({self.status})"


class TaxRefund(TaxDocument):
    """VAT refund received from ZATCA (3.5, T4: Dr bank / Cr 1310)."""

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="tax_refunds",
                               verbose_name=bi("Entity", "الكيان"))
    vat_return = models.ForeignKey(VatReturn, on_delete=models.PROTECT, related_name="refunds",
                                   verbose_name=bi("VAT return", "الإقرار"))
    amount_sar = models.DecimalField(bi("Amount (SAR)", "المبلغ (ريال)"), **MONEY)
    received_on = models.DateField(bi("Received on", "تاريخ الاستلام"))
    bank_account = models.ForeignKey("ledger.Account", on_delete=models.PROTECT, related_name="+",
                                     verbose_name=bi("Bank account (GL)", "الحساب البنكي (الأستاذ)"))
    zatca_reference = models.CharField(bi("ZATCA reference", "مرجع الهيئة"), max_length=60)

    class Meta:
        verbose_name = bi("VAT refund", "استرداد ضريبة القيمة المضافة")
        verbose_name_plural = bi("VAT refunds", "استردادات ضريبة القيمة المضافة")
        constraints = [models.CheckConstraint(condition=models.Q(amount_sar__gt=0), name="tax_refund_positive")]


# --- corrections (3.2, R26 to R28) -------------------------------------------------------------------

class VatCorrection(ServiceWritten):
    """An error found in a filed return (R27): next return (box 14), amendment (voluntary disclosure) or a
    large overstatement claimed in a next return."""

    class Treatment(models.TextChoices):
        NEXT_RETURN = "next_return", bi("Corrected in the next return (box 14)", "يُصحَّح في الإقرار التالي (الخانة 14)")
        AMENDMENT = "amendment", bi("Amendment by voluntary disclosure", "تعديل بإفصاح طوعي")
        OVERSTATEMENT_NEXT_RETURN = "overstatement_next_return", bi("Overstatement claimed in a next return",
                                                                    "زيادة مُقرّة تُسترد في إقرار تالٍ")

    class Status(models.TextChoices):
        OPEN = "open", bi("Open", "مفتوح")
        ASSIGNED = "assigned", bi("Assigned to a return", "أُسند إلى إقرار")
        FILED = "filed", bi("Filed", "مقدَّم")
        CANCELLED = "cancelled", bi("Cancelled", "ملغى")

    deletable_statuses = ()

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="vat_corrections",
                               verbose_name=bi("Entity", "الكيان"))
    original_return = models.ForeignKey(VatReturn, on_delete=models.PROTECT, related_name="corrections",
                                        verbose_name=bi("Return corrected", "الإقرار المصحَّح"))
    discovered_on = models.DateField(bi("Discovered on", "تاريخ الاكتشاف"))
    description_en = models.TextField(bi("Description (English)", "الوصف (بالإنجليزية)"))
    description_ar = models.TextField(bi("Description (Arabic)", "الوصف (بالعربية)"))
    tax_difference_sar = models.DecimalField(bi("Tax difference (SAR, + = underdeclared)",
                                                "فرق الضريبة (ريال، موجب = نقص في الإقرار)"), **MONEY)
    treatment = models.CharField(bi("Treatment", "المعالجة"), max_length=28, choices=Treatment.choices)
    notify_by = models.DateField(bi("Notify ZATCA by", "إبلاغ الهيئة قبل"), null=True, blank=True)
    correcting_entries = models.ManyToManyField("ledger.JournalEntry", blank=True, related_name="+",
                                                verbose_name=bi("Correcting entries", "قيود التصحيح"))
    target_return = models.ForeignKey(VatReturn, null=True, blank=True, on_delete=models.PROTECT,
                                      related_name="assigned_corrections",
                                      verbose_name=bi("Return carrying it", "الإقرار الذي يتضمنه"))
    voluntary_disclosure_ref = models.CharField(bi("Voluntary disclosure reference", "مرجع الإفصاح الطوعي"),
                                                max_length=60, blank=True)
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.OPEN)
    cancel_reason = models.CharField(bi("Cancelled because", "سبب الإلغاء"), max_length=500, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
                                   verbose_name=bi("Created by", "أنشأه"))
    created_at = models.DateTimeField(bi("Created at", "تاريخ الإنشاء"))

    class Meta:
        verbose_name = bi("VAT correction", "تصحيح ضريبي")
        verbose_name_plural = bi("VAT corrections", "التصحيحات الضريبية")

    def check_change(self):
        stored = type(self)._base_manager.filter(pk=self.pk).values("status", "target_return_id").first() \
            if self.pk else None
        if not stored:
            return
        changed = changed_fields(self)
        if not changed:
            return
        if stored["status"] in (self.Status.FILED, self.Status.CANCELLED):
            raise error("A filed or cancelled correction never changes.", "لا يتغير التصحيح المقدَّم أو الملغى.",
                        code="correction_locked")
        # Review item 9: once the return carrying it is approved, only its filing changes it.
        if stored["target_return_id"] and VatReturn._base_manager.filter(
                pk=stored["target_return_id"], status__in=LOCKED_STATUSES).exists():
            if not (changed <= {"status", "voluntary_disclosure_ref"} and self.status == self.Status.FILED):
                raise error("This correction is carried by an approved return; it is locked with it.",
                            "يتضمن هذا التصحيحَ إقرارٌ معتمد؛ فهو مقفل معه.", code="correction_locked")

    def __str__(self):
        return f"{self.entity.code} correction {self.tax_difference_sar} ({self.treatment}, {self.status})"


# --- deadline alerts (R13) ----------------------------------------------------------------------------

class TaxAlert(ServiceWritten):
    """An in-app deadline alert (R13, R27). Written by the ``tax_deadline_alerts`` job; append-only."""

    class Kind(models.TextChoices):
        VAT_DUE_SOON = "vat_due_soon", bi("VAT return due soon", "اقتراب موعد الإقرار الضريبي")
        VAT_OVERDUE = "vat_overdue", bi("VAT return overdue", "تأخر تقديم الإقرار الضريبي")
        VAT_PAYMENT_OVERDUE = "vat_payment_overdue", bi("VAT payment overdue", "تأخر سداد الضريبة")
        CORRECTION_NOTIFY = "correction_notify", bi("Voluntary disclosure due", "موعد الإفصاح الطوعي")

    deletable_statuses = ()

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="tax_alerts",
                               verbose_name=bi("Entity", "الكيان"))
    kind = models.CharField(bi("Alert", "التنبيه"), max_length=24, choices=Kind.choices)
    schedule = models.ForeignKey(VatReturnSchedule, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    correction = models.ForeignKey(VatCorrection, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    alert_date = models.DateField(bi("Alert date", "تاريخ التنبيه"))
    due_date = models.DateField(bi("Due date", "تاريخ الاستحقاق"))
    message_en = models.CharField(bi("Message (English)", "الرسالة (بالإنجليزية)"), max_length=500)
    message_ar = models.CharField(bi("Message (Arabic)", "الرسالة (بالعربية)"), max_length=500)
    exposure_sar = models.DecimalField(bi("Estimated late-payment exposure (SAR)", "تقدير غرامة التأخير (ريال)"),
                                       null=True, blank=True, **MONEY)
    # C17 (PSG §14): late filing 5-25% of the tax that should have been reported; incorrect or amended return 25%
    # of the difference. Estimates for display only, never posted.
    late_filing_min_sar = models.DecimalField(bi("Late-filing penalty from (SAR)", "غرامة التأخر في التقديم من (ريال)"),
                                              null=True, blank=True, **MONEY)
    late_filing_max_sar = models.DecimalField(bi("Late-filing penalty up to (SAR)", "غرامة التأخر في التقديم حتى (ريال)"),
                                              null=True, blank=True, **MONEY)
    incorrect_return_sar = models.DecimalField(bi("Incorrect-return penalty (SAR)", "غرامة الإقرار الخاطئ (ريال)"),
                                               null=True, blank=True, **MONEY)
    created_at = models.DateTimeField(bi("Created at", "تاريخ الإنشاء"))

    class Meta:
        verbose_name = bi("tax alert", "تنبيه ضريبي")
        verbose_name_plural = bi("tax alerts", "التنبيهات الضريبية")
        ordering = ["-alert_date", "entity"]
        constraints = [
            models.UniqueConstraint(fields=["entity", "kind", "schedule", "alert_date"],
                                    condition=models.Q(schedule__isnull=False), name="tax_alert_once_per_period_day"),
            models.UniqueConstraint(fields=["entity", "kind", "correction", "alert_date"],
                                    condition=models.Q(correction__isnull=False),
                                    name="tax_alert_once_per_correction_day"),
        ]

    def check_change(self):
        if self.pk is not None and type(self)._base_manager.filter(pk=self.pk).exists():
            raise error(*APPEND_ONLY, code="append_only")
