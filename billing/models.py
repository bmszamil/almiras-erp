"""Billing (docs/specs/billing.md): contracts, versions, schedules, runs, corrections and secondments.

Every business model has an ``entity`` FK (CLAUDE.md, R1). Business logic lives in ``billing.services``; the
guards here only keep approved versions, generated periods and append-only rows from being changed outside
those services (R7, R10, R38, R41). PostgreSQL triggers (migration 0002) repeat the guards for raw SQL.

Money is ``Decimal``: contract-currency amounts with up to 3 decimals, never more than the currency's own minor
units (D11, R37, checked by ``billing.services``). Billing books nothing: invoicing posts the documents (§5).
"""
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from core.models import Currency, Entity, bi

CONTRACT_MONEY = dict(max_digits=21, decimal_places=3)


class BillingError(ValidationError):
    """A broken billing rule; the message carries English and Arabic ("English / العربية").

    ``retryable`` is True when nothing is wrong with the request and the same call will succeed later (today only
    ``run_in_progress``: a billing run holds the entity's lock). Callers that retry with an attempt limit (the HR
    delivery job) should not count such a refusal as a failed attempt."""

    def __init__(self, message, code=None, params=None, retryable=False):
        super().__init__(message, code=code, params=params)
        self.code = code
        self.retryable = retryable


def error(en, ar, code=None, retryable=False):
    return BillingError(f"{en} / {ar}", code=code, retryable=retryable)


SERVICE_ONLY = ("This billing record changes only through billing.services.",
                "لا يتغير سجل الفوترة هذا إلا من خلال خدمات الفوترة.")
APPROVED_IMMUTABLE = ("Approved contract versions, their lines and price steps are never edited or deleted (R38).",
                      "لا تُعدَّل نسخ العقد المعتمدة وبنودها وخطوات أسعارها ولا تُحذف.")
APPEND_ONLY = ("This record is append-only.", "هذا السجل للإضافة فقط ولا يُعدَّل.")


class ServiceOnlyQuerySet(models.QuerySet):
    """Bulk writes skip save()/delete(), so they are refused; services change one row at a time."""

    def update(self, **kwargs):
        raise error(*SERVICE_ONLY, code="service_only")

    def delete(self):
        raise error(*SERVICE_ONLY, code="service_only")


class Tracked(models.Model):
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
                                   verbose_name=bi("Created by", "أنشأه"))
    created_at = models.DateTimeField(bi("Created at", "تاريخ الإنشاء"), auto_now_add=True)
    updated_at = models.DateTimeField(bi("Updated at", "تاريخ التحديث"), auto_now=True)
    modified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=bi("Last modified by", "آخر من عدّله"))
    source = models.CharField(bi("Source", "المصدر"), max_length=60, default="human",
                              help_text=bi("human, import, system:billing or agent:<name>",
                                           "شخص أو استيراد أو نظام الفوترة أو وكيل ذكاء اصطناعي"))

    class Meta:
        abstract = True


class Guarded(models.Model):
    """Saves and deletes only through billing.services (``obj._service_write = True`` right before the save)."""

    objects = ServiceOnlyQuerySet.as_manager()

    class Meta:
        abstract = True

    def _check_write(self):
        allowed, self._service_write = getattr(self, "_service_write", False), False
        if not allowed:
            raise error(*SERVICE_ONLY, code="service_only")

    def save(self, *args, **kwargs):
        self._check_write()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self._check_write()
        return super().delete(*args, **kwargs)


# --- choices ---------------------------------------------------------------------------------------

class ContractType(models.TextChoices):
    LEASE = "lease", bi("Lease", "إيجار عقار")
    VEHICLE_RENTAL = "vehicle_rental", bi("Vehicle rental", "تأجير سيارات")
    SECONDMENT = "secondment", bi("Staff secondment", "إعارة موظفين")
    SERVICE = "service", bi("Service", "خدمات")
    OTHER = "other", bi("Other", "أخرى")


class Frequency(models.TextChoices):
    MONTHLY = "monthly", bi("Monthly", "شهري")
    QUARTERLY = "quarterly", bi("Quarterly", "ربع سنوي")
    SEMIANNUAL = "semiannual", bi("Semi-annual", "نصف سنوي")
    ANNUAL = "annual", bi("Annual", "سنوي")


FREQUENCY_MONTHS = {"monthly": 1, "quarterly": 3, "semiannual": 6, "annual": 12}


class Timing(models.TextChoices):
    ADVANCE = "advance", bi("In advance", "مقدمًا")
    ARREARS = "arrears", bi("In arrears", "مؤخرًا")


class Anchor(models.TextChoices):
    CALENDAR = "calendar", bi("Calendar periods", "فترات تقويمية")
    CONTRACT_START = "contract_start", bi("From contract start", "من بداية العقد")


class LeaseUse(models.TextChoices):
    RESIDENTIAL = "residential", bi("Residential", "سكني")
    COMMERCIAL = "commercial", bi("Commercial", "تجاري")


class VatCategory(models.TextChoices):
    STANDARD = "S", bi("Standard rated", "خاضع للنسبة الأساسية")
    ZERO = "Z", bi("Zero rated", "خاضع للنسبة الصفرية")
    EXEMPT = "E", bi("Exempt", "معفى")
    OUT_OF_SCOPE = "O", bi("Out of scope", "خارج نطاق الضريبة")


# --- numbering -------------------------------------------------------------------------------------

class ContractSequence(models.Model):
    """Gap-free contract numbers per (entity, year), locked until the approving transaction ends (R8, OQ-14)."""

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    year = models.PositiveIntegerField(bi("Year", "السنة"))
    last_number = models.PositiveIntegerField(bi("Last number", "آخر رقم"), default=0)

    class Meta:
        verbose_name = bi("contract sequence", "تسلسل العقود")
        constraints = [models.UniqueConstraint(fields=["entity", "year"], name="billing_contract_sequence")]


# --- advance and deposit terms (3.4) ---------------------------------------------------------------

class AdvanceTerm(Tracked, Guarded):
    class Recovery(models.TextChoices):
        OFFSET = "offset_against_invoices", bi("Offset against invoices", "تُخصم من الفواتير")
        NONE = "none", bi("Not recovered from invoices", "لا تُخصم من الفواتير")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    amount = models.DecimalField(bi("Amount", "المبلغ"), **CONTRACT_MONEY)
    currency = models.ForeignKey(Currency, to_field="code", on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("Currency", "العملة"))
    due_date = models.DateField(bi("Due date", "تاريخ الاستحقاق"))
    recovery = models.CharField(bi("Recovery", "طريقة الاسترداد"), max_length=30, choices=Recovery.choices)
    notes_en = models.TextField(bi("Notes (English)", "ملاحظات (بالإنجليزية)"), blank=True)
    notes_ar = models.TextField(bi("Notes (Arabic)", "ملاحظات (بالعربية)"), blank=True)

    class Meta:
        verbose_name = bi("advance term", "شرط الدفعة المقدمة")
        verbose_name_plural = bi("advance terms", "شروط الدفعات المقدمة")


class DepositTerm(Tracked, Guarded):
    """Memo only: no invoice, no posting (R31)."""

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    amount = models.DecimalField(bi("Amount", "المبلغ"), **CONTRACT_MONEY)
    currency = models.ForeignKey(Currency, to_field="code", on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("Currency", "العملة"))
    due_date = models.DateField(bi("Due date", "تاريخ الاستحقاق"))
    refundable = models.BooleanField(bi("Refundable", "قابل للاسترداد"), default=True)
    refund_conditions_en = models.TextField(bi("Refund conditions (English)", "شروط الاسترداد (بالإنجليزية)"), blank=True)
    refund_conditions_ar = models.TextField(bi("Refund conditions (Arabic)", "شروط الاسترداد (بالعربية)"), blank=True)

    class Meta:
        verbose_name = bi("deposit term", "شرط التأمين")
        verbose_name_plural = bi("deposit terms", "شروط التأمين")


# --- contract (3.1) --------------------------------------------------------------------------------

class Contract(Tracked, Guarded):
    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        APPROVED = "approved", bi("Approved", "معتمد")
        ACTIVE = "active", bi("Active", "ساري")
        ENDED = "ended", bi("Ended", "منتهٍ")
        TERMINATED = "terminated", bi("Terminated", "منهى")

    class TerminationKind(models.TextChoices):
        EARLY = "early_termination", bi("Early termination", "إنهاء مبكر")
        CANCELLATION = "cancellation", bi("Cancellation", "إلغاء")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="billing_contracts",
                               verbose_name=bi("Entity", "الكيان"))
    number = models.CharField(bi("Number", "الرقم"), max_length=40, blank=True)
    contract_type = models.CharField(bi("Contract type", "نوع العقد"), max_length=20, choices=ContractType.choices)
    customer = models.ForeignKey("invoicing.Customer", on_delete=models.PROTECT, related_name="billing_contracts",
                                 verbose_name=bi("Customer", "العميل"))
    counterparty_entity = models.ForeignKey(
        Entity, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
        verbose_name=bi("Counterparty entity", "الكيان الطرف المقابل"),
        help_text=bi("Copied from the customer's group entity; set = intercompany (R3)",
                     "يُنسخ من كيان المجموعة المرتبط بالعميل؛ وجوده يعني عقدًا بين شركات المجموعة"),
    )
    title_en = models.CharField(bi("Title (English)", "العنوان (بالإنجليزية)"), max_length=200, blank=True)
    title_ar = models.CharField(bi("Title (Arabic)", "العنوان (بالعربية)"), max_length=200)
    description_en = models.TextField(bi("Description (English)", "الوصف (بالإنجليزية)"), blank=True)
    description_ar = models.TextField(bi("Description (Arabic)", "الوصف (بالعربية)"))
    currency = models.ForeignKey(Currency, to_field="code", on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("Currency", "العملة"))
    start_date = models.DateField(bi("Start date", "تاريخ البداية"))
    end_date = models.DateField(bi("End date", "تاريخ النهاية"), null=True, blank=True)
    billing_frequency = models.CharField(bi("Billing frequency", "دورية الفوترة"), max_length=12,
                                         choices=Frequency.choices)
    billing_timing = models.CharField(bi("Billing timing", "توقيت الفوترة"), max_length=8, choices=Timing.choices)
    period_anchor = models.CharField(bi("Period anchor", "بداية الفترات"), max_length=16, choices=Anchor.choices,
                                     default=Anchor.CALENDAR)
    bill_lead_days = models.PositiveSmallIntegerField(bi("Bill lead days", "أيام الفوترة المسبقة"), default=0)
    due_days = models.PositiveSmallIntegerField(bi("Due days", "أيام الاستحقاق"), null=True, blank=True)
    lease_use = models.CharField(bi("Lease use", "استخدام العقار"), max_length=12, choices=LeaseUse.choices,
                                 blank=True)
    property_ref = models.CharField(bi("Property", "العقار"), max_length=60, blank=True)
    unit_ref = models.CharField(bi("Unit", "الوحدة"), max_length=60, blank=True)
    ejar_contract_no = models.CharField(bi("Ejar contract number", "رقم عقد إيجار"), max_length=40, blank=True)
    vehicle_ref = models.CharField(bi("Vehicle (plate / asset code)", "السيارة (اللوحة / رمز الأصل)"), max_length=40,
                                   blank=True)
    advance_terms = models.ForeignKey(AdvanceTerm, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                                      verbose_name=bi("Advance terms", "شروط الدفعة المقدمة"))
    deposit_terms = models.ForeignKey(DepositTerm, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                                      verbose_name=bi("Deposit terms", "شروط التأمين"))
    status = models.CharField(bi("Status", "الحالة"), max_length=12, choices=Status.choices, default=Status.DRAFT)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+", verbose_name=bi("Submitted by", "قدّمه"))
    submitted_at = models.DateTimeField(bi("Submitted at", "تاريخ التقديم"), null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=bi("Approved by", "اعتمده"))
    approved_at = models.DateTimeField(bi("Approved at", "تاريخ الاعتماد"), null=True, blank=True)
    terminated_on = models.DateField(bi("Last billable day", "آخر يوم قابل للفوترة"), null=True, blank=True)
    termination_reason_en = models.TextField(bi("Termination reason (English)", "سبب الإنهاء (بالإنجليزية)"), blank=True)
    termination_reason_ar = models.TextField(bi("Termination reason (Arabic)", "سبب الإنهاء (بالعربية)"), blank=True)
    termination_kind = models.CharField(bi("Termination kind", "نوع الإنهاء"), max_length=20,
                                        choices=TerminationKind.choices, blank=True)
    current_version = models.ForeignKey("ContractVersion", null=True, blank=True, on_delete=models.PROTECT,
                                        related_name="+", verbose_name=bi("Current version", "النسخة الحالية"))
    requires_purification = models.BooleanField(bi("Requires purification", "يتطلب التطهير"), default=False)

    class Meta:
        verbose_name = bi("contract", "عقد")
        verbose_name_plural = bi("contracts", "العقود")
        ordering = ["entity", "number", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["entity", "number"], condition=~Q(number=""),
                                    name="billing_contract_number"),
            models.CheckConstraint(condition=Q(counterparty_entity__isnull=True) | ~Q(counterparty_entity=models.F("entity")),
                                   name="billing_contract_not_own_counterparty"),
        ]

    @property
    def is_intercompany(self):
        return self.counterparty_entity_id is not None

    def __str__(self):
        return self.number or f"{self.entity.code} contract draft {self.pk}"


# --- versions, lines, price steps (3.2, 3.3, 3.9) ------------------------------------------------------

class ContractVersion(Tracked, Guarded):
    class Kind(models.TextChoices):
        ORIGINAL = "original", bi("Original terms", "الشروط الأصلية")
        AMENDMENT = "amendment", bi("Amendment", "تعديل")
        TERMINATION = "termination", bi("Early termination", "إنهاء مبكر")
        CANCELLATION = "cancellation", bi("Cancellation", "إلغاء")

    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        SUBMITTED = "submitted", bi("Submitted", "مقدّمة")
        APPROVED = "approved", bi("Approved", "معتمدة")
        REJECTED = "rejected", bi("Rejected", "مرفوضة")
        WITHDRAWN = "withdrawn", bi("Withdrawn", "مسحوبة")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    contract = models.ForeignKey(Contract, on_delete=models.PROTECT, related_name="versions",
                                 verbose_name=bi("Contract", "العقد"))
    version_no = models.PositiveIntegerField(bi("Version", "رقم النسخة"))
    kind = models.CharField(bi("Kind", "النوع"), max_length=12, choices=Kind.choices)
    effective_date = models.DateField(bi("Effective date", "تاريخ السريان"))
    end_date = models.DateField(bi("End date", "تاريخ النهاية"), null=True, blank=True)
    billing_frequency = models.CharField(bi("Billing frequency", "دورية الفوترة"), max_length=12,
                                         choices=Frequency.choices)
    billing_timing = models.CharField(bi("Billing timing", "توقيت الفوترة"), max_length=8, choices=Timing.choices)
    period_anchor = models.CharField(bi("Period anchor", "بداية الفترات"), max_length=16, choices=Anchor.choices)
    due_days = models.PositiveSmallIntegerField(bi("Due days", "أيام الاستحقاق"), null=True, blank=True)
    reason_en = models.TextField(bi("Reason (English)", "السبب (بالإنجليزية)"), blank=True)
    reason_ar = models.TextField(bi("Reason (Arabic)", "السبب (بالعربية)"), blank=True)
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.DRAFT)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+", verbose_name=bi("Submitted by", "قدّمها"))
    submitted_at = models.DateTimeField(bi("Submitted at", "تاريخ التقديم"), null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=bi("Approved by", "اعتمدها"))
    approved_at = models.DateTimeField(bi("Approved at", "تاريخ الاعتماد"), null=True, blank=True)
    rejected_reason_en = models.TextField(bi("Rejection reason (English)", "سبب الرفض (بالإنجليزية)"), blank=True)
    rejected_reason_ar = models.TextField(bi("Rejection reason (Arabic)", "سبب الرفض (بالعربية)"), blank=True)

    class Meta:
        verbose_name = bi("contract version", "نسخة العقد")
        verbose_name_plural = bi("contract versions", "نسخ العقد")
        ordering = ["contract", "version_no"]
        constraints = [models.UniqueConstraint(fields=["contract", "version_no"], name="billing_version_no")]

    def _stored_status(self):
        if not self.pk:
            return None
        return type(self)._base_manager.filter(pk=self.pk).values_list("status", flat=True).first()

    def save(self, *args, **kwargs):
        if self._stored_status() in (self.Status.APPROVED, self.Status.REJECTED, self.Status.WITHDRAWN):
            self._service_write = False
            raise error(*APPROVED_IMMUTABLE, code="version_immutable")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self._stored_status() != self.Status.DRAFT:
            raise error(*APPROVED_IMMUTABLE, code="version_immutable")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.contract} v{self.version_no} ({self.status})"


class VersionChild(Guarded):
    """Lines and price steps are written only while their version is a draft (R38)."""

    class Meta:
        abstract = True

    def _version_status(self):
        raise NotImplementedError

    def save(self, *args, **kwargs):
        if self._version_status() != ContractVersion.Status.DRAFT:
            self._service_write = False
            raise error(*APPROVED_IMMUTABLE, code="version_immutable")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self._version_status() != ContractVersion.Status.DRAFT:
            raise error(*APPROVED_IMMUTABLE, code="version_immutable")
        return super().delete(*args, **kwargs)


class SecondmentAssignment(Tracked, Guarded):
    """The billing side of one seconded person (3.6). Pseudonymous: ``employee_ref`` only, never a name or ID."""

    class EndSource(models.TextChoices):
        MANUAL = "manual", bi("Recorded by accounting staff", "سجله المحاسب")
        HR_EVENT = "hr_event", bi("From HR", "من الموارد البشرية")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    contract = models.ForeignKey(Contract, on_delete=models.PROTECT, related_name="secondments",
                                 verbose_name=bi("Contract", "العقد"))
    receiving_entity = models.ForeignKey(Entity, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                                         verbose_name=bi("Receiving entity", "الكيان المستقبِل"))
    hr_assignment_ref = models.CharField(bi("HR assignment", "مرجع الإعارة في الموارد البشرية"), max_length=60,
                                         null=True, blank=True)
    employee_ref = models.CharField(bi("Employee reference", "مرجع الموظف"), max_length=40)
    role_title_en = models.CharField(bi("Role title (English)", "المسمى (بالإنجليزية)"), max_length=200, blank=True)
    role_title_ar = models.CharField(bi("Role title (Arabic)", "المسمى (بالعربية)"), max_length=200)
    start_date = models.DateField(bi("Start date", "تاريخ البداية"))
    end_date = models.DateField(bi("End date", "تاريخ النهاية"), null=True, blank=True)
    end_recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                        related_name="+", verbose_name=bi("End recorded by", "سجّل النهاية"))
    end_recorded_at = models.DateTimeField(bi("End recorded at", "تاريخ تسجيل النهاية"), null=True, blank=True)
    end_source = models.CharField(bi("End source", "مصدر النهاية"), max_length=10, choices=EndSource.choices, blank=True)
    hr_linked_at = models.DateTimeField(bi("Linked to HR at", "تاريخ الربط بالموارد البشرية"), null=True, blank=True)
    hr_linked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+", verbose_name=bi("Linked to HR by", "ربطه بالموارد البشرية"))
    last_hr_end_date = models.DateField(bi("Last end date from HR", "آخر تاريخ نهاية من الموارد البشرية"), null=True,
                                        blank=True)

    class Meta:
        verbose_name = bi("secondment assignment", "إعارة موظف")
        verbose_name_plural = bi("secondment assignments", "إعارات الموظفين")
        constraints = [models.UniqueConstraint(fields=["entity", "hr_assignment_ref"],
                                               condition=Q(hr_assignment_ref__isnull=False),
                                               name="billing_secondment_hr_ref")]

    def __str__(self):
        return f"{self.contract} secondment {self.pk}"


class ContractLine(Tracked, VersionChild):
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    contract = models.ForeignKey(Contract, on_delete=models.PROTECT, related_name="lines",
                                 verbose_name=bi("Contract", "العقد"))
    version = models.ForeignKey(ContractVersion, on_delete=models.PROTECT, related_name="lines",
                                verbose_name=bi("Version", "النسخة"))
    line_key = models.UUIDField(bi("Line key", "مفتاح البند"), default=uuid.uuid4)
    line_no = models.PositiveIntegerField(bi("Line", "البند"))
    description_en = models.CharField(bi("Description (English)", "الوصف (بالإنجليزية)"), max_length=300, blank=True)
    description_ar = models.CharField(bi("Description (Arabic)", "الوصف (بالعربية)"), max_length=300)
    amount = models.DecimalField(bi("Amount per full period", "المبلغ لكل فترة كاملة"), **CONTRACT_MONEY)
    unit_code = models.CharField(bi("Unit", "الوحدة"), max_length=3, default="MON")
    vat_category = models.CharField(bi("VAT category", "فئة الضريبة"), max_length=1, choices=VatCategory.choices)
    vat_exemption_reason = models.ForeignKey("invoicing.VatExemptionReason", null=True, blank=True,
                                             on_delete=models.PROTECT, related_name="+",
                                             verbose_name=bi("Exemption reason", "سبب الإعفاء"))
    income_type = models.ForeignKey("invoicing.IncomeType", on_delete=models.PROTECT, related_name="+",
                                    verbose_name=bi("Income type", "نوع الإيراد"))
    income_account = models.ForeignKey("ledger.Account", on_delete=models.PROTECT, related_name="+",
                                       verbose_name=bi("Income account", "حساب الإيراد"))
    secondment = models.ForeignKey(SecondmentAssignment, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="lines", verbose_name=bi("Secondment", "الإعارة"))

    class Meta:
        verbose_name = bi("contract line", "بند العقد")
        verbose_name_plural = bi("contract lines", "بنود العقد")
        ordering = ["version", "line_no"]
        constraints = [
            models.UniqueConstraint(fields=["version", "line_no"], name="billing_line_no"),
            models.UniqueConstraint(fields=["version", "line_key"], name="billing_line_key"),
        ]

    def _version_status(self):
        return ContractVersion._base_manager.filter(pk=self.version_id).values_list("status", flat=True).first()

    def __str__(self):
        return f"{self.version} line {self.line_no}"


class ContractPriceStep(VersionChild):
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    line = models.ForeignKey(ContractLine, on_delete=models.PROTECT, related_name="price_steps",
                             verbose_name=bi("Line", "البند"))
    effective_from = models.DateField(bi("Effective from", "ساري من"))
    new_amount = models.DecimalField(bi("New amount", "المبلغ الجديد"), **CONTRACT_MONEY)

    class Meta:
        verbose_name = bi("price step", "خطوة سعرية")
        verbose_name_plural = bi("price steps", "الخطوات السعرية")
        ordering = ["line", "effective_from"]
        constraints = [models.UniqueConstraint(fields=["line", "effective_from"], name="billing_price_step_date")]

    def _version_status(self):
        return ContractVersion._base_manager.filter(lines__pk=self.line_id).values_list("status", flat=True).first()


# --- schedule (3.5) --------------------------------------------------------------------------------

class BillingPeriod(Guarded):
    class Status(models.TextChoices):
        PLANNED = "planned", bi("Planned", "مخطط")
        GENERATED = "generated", bi("Generated", "تم إصدار الفاتورة")
        HELD = "held", bi("Held", "معلّق")
        FAILED = "failed", bi("Failed", "متعثر")
        CANCELLED = "cancelled", bi("Cancelled", "ملغى")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    contract = models.ForeignKey(Contract, on_delete=models.PROTECT, related_name="periods",
                                 verbose_name=bi("Contract", "العقد"))
    period_start = models.DateField(bi("Period start", "بداية الفترة"))
    period_end = models.DateField(bi("Period end", "نهاية الفترة"))
    full_period_days = models.PositiveSmallIntegerField(
        bi("Days in the full period", "أيام الفترة الكاملة"),
        help_text=bi("Denominator of the actual-days proration (R16)", "مقام التناسب بالأيام الفعلية"))
    bill_date = models.DateField(bi("Bill date", "تاريخ الفوترة"))
    proration_factor = models.DecimalField(bi("Proration factor", "معامل التناسب"), max_digits=9, decimal_places=6)
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.PLANNED)
    needs_release = models.BooleanField(
        bi("Needs human release", "يحتاج إلى إطلاق يدوي"), default=False,
        help_text=bi("Failed after a ZATCA rejection: retried only after a person releases it (R12)",
                     "تعثر بعد رفض الهيئة: لا يُعاد إلا بعد إطلاقه يدويًا"))
    hold_reason_en = models.CharField(bi("Hold reason (English)", "سبب التعليق (بالإنجليزية)"), max_length=300, blank=True)
    hold_reason_ar = models.CharField(bi("Hold reason (Arabic)", "سبب التعليق (بالعربية)"), max_length=300, blank=True)
    failure_reason_en = models.CharField(bi("Failure reason (English)", "سبب التعثر (بالإنجليزية)"), max_length=300,
                                         blank=True)
    failure_reason_ar = models.CharField(bi("Failure reason (Arabic)", "سبب التعثر (بالعربية)"), max_length=300,
                                         blank=True)
    error_code = models.CharField(bi("Error code", "رمز الخطأ"), max_length=60, blank=True)
    consecutive_failures = models.PositiveSmallIntegerField(bi("Consecutive failed runs", "مرات التعثر المتتالية"),
                                                            default=0)
    sales_document = models.ForeignKey("invoicing.SalesDocument", null=True, blank=True, on_delete=models.PROTECT,
                                       related_name="+", verbose_name=bi("Invoice", "الفاتورة"))
    superseded_documents = models.ManyToManyField("invoicing.SalesDocument", blank=True, related_name="+",
                                                  verbose_name=bi("ZATCA-rejected earlier invoices",
                                                                  "فواتير سابقة رفضتها الهيئة"))
    billed_version = models.ForeignKey(ContractVersion, null=True, blank=True, on_delete=models.PROTECT,
                                       related_name="+", verbose_name=bi("Billed version", "النسخة المفوترة"))
    invoice_lines = models.JSONField(bi("Invoice lines handed to invoicing", "بنود الفاتورة المسلّمة للفوترة"),
                                     null=True, blank=True)
    generated_at = models.DateTimeField(bi("Generated at", "تاريخ الإصدار"), null=True, blank=True)
    created_at = models.DateTimeField(bi("Created at", "تاريخ الإنشاء"), auto_now_add=True)
    updated_at = models.DateTimeField(bi("Updated at", "تاريخ التحديث"), auto_now=True)

    # The fields fixed once a period is generated (R10, R41c, R52); only the R12 rejection path changes it.
    FROZEN_WHEN_GENERATED = ("contract_id", "entity_id", "period_start", "period_end", "full_period_days", "bill_date",
                             "proration_factor", "billed_version_id", "invoice_lines")

    class Meta:
        verbose_name = bi("billing period", "فترة فوترة")
        verbose_name_plural = bi("billing periods", "فترات الفوترة")
        ordering = ["contract", "period_start"]
        constraints = [models.UniqueConstraint(fields=["contract", "period_start"], name="billing_period_start")]

    def save(self, *args, **kwargs):
        if self.pk:
            stored = type(self)._base_manager.filter(pk=self.pk).values(
                "status", "sales_document_id", *self.FROZEN_WHEN_GENERATED).first()
            if stored and stored["status"] == self.Status.GENERATED:
                frozen_changed = any(stored[f] != getattr(self, f) for f in self.FROZEN_WHEN_GENERATED)
                to_failed = self.status == self.Status.FAILED and self.sales_document_id is None
                same_doc = self.status == self.Status.GENERATED and stored["sales_document_id"] == self.sales_document_id
                if frozen_changed or not (to_failed or same_doc):
                    self._service_write = False
                    raise error("A generated billing period is never changed or billed again (R10, R11).",
                                "لا تُعدَّل فترة فوترة صدرت فاتورتها ولا تُفوتر مرة أخرى.", code="period_generated")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise error("Billing periods are never deleted; they are cancelled.", "لا تُحذف فترات الفوترة؛ بل تُلغى.",
                    code="period_delete")

    def __str__(self):
        return f"{self.contract} {self.period_start}..{self.period_end} ({self.status})"


# --- runs (3.7) -------------------------------------------------------------------------------------

class BillingRun(Guarded):
    class Trigger(models.TextChoices):
        SCHEDULER = "scheduler", bi("Scheduler", "المجدول")
        MANUAL = "manual", bi("Manual", "يدوي")

    class Status(models.TextChoices):
        RUNNING = "running", bi("Running", "قيد التنفيذ")
        COMPLETED = "completed", bi("Completed", "مكتمل")
        COMPLETED_WITH_ERRORS = "completed_with_errors", bi("Completed with errors", "مكتمل مع أخطاء")
        SKIPPED = "skipped", bi("Skipped (no billing membership)", "تم التخطي (لا عضوية لنظام الفوترة)")
        ABORTED = "aborted", bi("Aborted", "متوقف")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    run_date = models.DateField(bi("Run date", "تاريخ الدورة"))
    trigger = models.CharField(bi("Trigger", "طريقة التشغيل"), max_length=10, choices=Trigger.choices)
    started_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
                                   verbose_name=bi("Started by (system user)", "بدأها (مستخدم النظام)"))
    initiated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+", verbose_name=bi("Initiated by", "طلبها"))
    started_at = models.DateTimeField(bi("Started at", "وقت البدء"), auto_now_add=True)
    finished_at = models.DateTimeField(bi("Finished at", "وقت الانتهاء"), null=True, blank=True)
    status = models.CharField(bi("Status", "الحالة"), max_length=24, choices=Status.choices, default=Status.RUNNING)
    generated_count = models.PositiveIntegerField(bi("Generated", "صدرت"), default=0)
    skipped_count = models.PositiveIntegerField(bi("Skipped (already billed)", "تم تخطيها (مفوترة)"), default=0)
    held_count = models.PositiveIntegerField(bi("Held", "معلّقة"), default=0)
    failed_count = models.PositiveIntegerField(bi("Failed", "متعثرة"), default=0)
    sync_count = models.PositiveIntegerField(bi("Documents read back", "مستندات تمت قراءة حالتها"), default=0)
    messages = models.JSONField(bi("Run messages (en/ar)", "رسائل الدورة"), default=list, blank=True)

    class Meta:
        verbose_name = bi("billing run", "دورة فوترة")
        verbose_name_plural = bi("billing runs", "دورات الفوترة")
        ordering = ["-started_at", "-pk"]

    def save(self, *args, **kwargs):
        if self.pk:
            stored = type(self)._base_manager.filter(pk=self.pk).values_list("status", flat=True).first()
            if stored != self.Status.RUNNING:
                self._service_write = False
                raise error(*APPEND_ONLY, code="append_only")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise error(*APPEND_ONLY, code="append_only")


class BillingRunItem(Guarded):
    class Outcome(models.TextChoices):
        GENERATED = "generated", bi("Generated", "صدرت")
        SKIPPED = "skipped_already_billed", bi("Skipped: already billed", "تم التخطي: مفوترة مسبقًا")
        HELD = "held", bi("Held", "معلّقة")
        FAILED = "failed", bi("Failed", "متعثرة")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    run = models.ForeignKey(BillingRun, on_delete=models.PROTECT, related_name="items", verbose_name=bi("Run", "الدورة"))
    billing_period = models.ForeignKey(BillingPeriod, on_delete=models.PROTECT, related_name="run_items",
                                       verbose_name=bi("Billing period", "فترة الفوترة"))
    outcome = models.CharField(bi("Outcome", "النتيجة"), max_length=24, choices=Outcome.choices)
    sales_document = models.ForeignKey("invoicing.SalesDocument", null=True, blank=True, on_delete=models.PROTECT,
                                       related_name="+", verbose_name=bi("Invoice", "الفاتورة"))
    error_code = models.CharField(bi("Error code", "رمز الخطأ"), max_length=60, blank=True)
    message_en = models.TextField(bi("Message (English)", "الرسالة (بالإنجليزية)"))
    message_ar = models.TextField(bi("Message (Arabic)", "الرسالة (بالعربية)"))
    warnings = models.JSONField(bi("Invoicing warnings (en/ar)", "تحذيرات الفوترة"), default=list, blank=True)
    created_at = models.DateTimeField(bi("Created at", "تاريخ الإنشاء"), auto_now_add=True)

    class Meta:
        verbose_name = bi("billing run item", "بند دورة فوترة")
        verbose_name_plural = bi("billing run items", "بنود دورات الفوترة")
        ordering = ["run", "pk"]

    def save(self, *args, **kwargs):
        if self.pk:
            self._service_write = False
            raise error(*APPEND_ONLY, code="append_only")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise error(*APPEND_ONLY, code="append_only")


# --- audit (3.8) -----------------------------------------------------------------------------------

class ContractEvent(Guarded):
    class Action(models.TextChoices):
        CREATED = "created", bi("Created", "أُنشئ")
        SUBMITTED = "submitted_for_approval", bi("Submitted for approval", "قُدّم للاعتماد")
        APPROVED = "approved", bi("Approved", "اعتُمد")
        RETURNED = "returned", bi("Returned", "أُعيد")
        ACTIVATED = "activated", bi("Activated", "أصبح ساريًا")
        AMENDED = "amended", bi("Amended", "عُدّل")
        SECONDMENT_ENDED = "secondment_ended", bi("Secondment ended", "انتهت الإعارة")
        SECONDMENT_REOPENED = "secondment_reopened", bi("Secondment reopened", "أعيد فتح الإعارة")
        SECONDMENT_LINKED = "secondment_linked", bi("Secondment linked to HR", "رُبطت الإعارة بالموارد البشرية")
        ENDED = "ended", bi("Ended", "انتهى")
        TERMINATED = "terminated", bi("Terminated", "أُنهي")
        PERIOD_RELEASED = "period_released", bi("Period released", "أُطلقت فترة")
        VERSION_SUBMITTED = "version_submitted", bi("Version submitted", "قُدّمت نسخة")
        VERSION_APPROVED = "version_approved", bi("Version approved", "اعتُمدت نسخة")
        VERSION_REJECTED = "version_rejected", bi("Version rejected", "رُفضت نسخة")
        VERSION_WITHDRAWN = "version_withdrawn", bi("Version withdrawn", "سُحبت نسخة")
        CANCELLED = "cancelled", bi("Cancelled", "أُلغي")
        CORRECTIONS_COMPUTED = "corrections_computed", bi("Corrections computed", "حُسبت التصحيحات")
        CORRECTION_DRAFTED = "correction_drafted", bi("Correction drafted", "أُعدت مسودة تصحيح")
        CORRECTION_SUBMITTED = "correction_submitted", bi("Correction submitted", "قُدّم التصحيح")
        CORRECTION_DISCARDED = "correction_discarded", bi("Correction discarded", "استُبعد التصحيح")
        CORRECTION_SUPERSEDED = "correction_superseded", bi("Correction superseded", "حلّ تصحيح آخر محل التصحيح")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    contract = models.ForeignKey(Contract, on_delete=models.PROTECT, related_name="events",
                                 verbose_name=bi("Contract", "العقد"))
    action = models.CharField(bi("Action", "الإجراء"), max_length=30, choices=Action.choices)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
                              verbose_name=bi("Actor", "المنفّذ"))
    at = models.DateTimeField(bi("At", "الوقت"), auto_now_add=True)
    detail = models.JSONField(bi("Detail", "التفاصيل"), default=dict, blank=True)

    class Meta:
        verbose_name = bi("contract event", "حدث عقد")
        verbose_name_plural = bi("contract events", "أحداث العقود")
        ordering = ["contract", "pk"]

    def save(self, *args, **kwargs):
        if self.pk:
            self._service_write = False
            raise error(*APPEND_ONLY, code="append_only")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise error(*APPEND_ONLY, code="append_only")


# --- corrections (3.10) ----------------------------------------------------------------------------

class BillingAdjustment(Guarded):
    class Kind(models.TextChoices):
        CREDIT = "credit", bi("Credit (over-billed)", "دائن (فوترة زائدة)")
        DEBIT = "debit", bi("Debit (under-billed)", "مدين (فوترة ناقصة)")

    class Status(models.TextChoices):
        WAITING_ORIGINAL = "waiting_original", bi("Waiting for the original invoice", "بانتظار الفاتورة الأصلية")
        DRAFTED = "drafted", bi("Draft note created", "أُنشئت مسودة الإشعار")
        SUBMITTED = "submitted", bi("Submitted", "مقدّم")
        ISSUED = "issued", bi("Issued, awaiting ZATCA", "صادر، بانتظار الهيئة")
        FINAL = "final", bi("Final", "نهائي")
        DISCARDED = "discarded", bi("Discarded", "مستبعد")
        SUPERSEDED = "superseded", bi("Superseded", "حلّ محله غيره")
        FAILED = "failed", bi("Rejected by ZATCA", "رفضته الهيئة")

    PENDING = ("waiting_original", "drafted", "submitted")
    # Counted in the billed position (3.11, R46b): pending corrections and issued or final notes.
    COUNTED = ("waiting_original", "drafted", "submitted", "issued", "final")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    contract = models.ForeignKey(Contract, on_delete=models.PROTECT, related_name="adjustments",
                                 verbose_name=bi("Contract", "العقد"))
    version = models.ForeignKey(ContractVersion, on_delete=models.PROTECT, related_name="+",
                                verbose_name=bi("Version", "النسخة"))
    trigger = models.CharField(bi("Trigger", "سبب الاحتساب"), max_length=60,
                               help_text=bi("version, secondment_end:<date>, secondment_reopened or recompute",
                                            "نسخة أو نهاية إعارة أو إعادة فتح أو إعادة احتساب"))
    billing_period = models.ForeignKey(BillingPeriod, on_delete=models.PROTECT, related_name="adjustments",
                                       verbose_name=bi("Billing period", "فترة الفوترة"))
    original_document = models.ForeignKey("invoicing.SalesDocument", on_delete=models.PROTECT, related_name="+",
                                          verbose_name=bi("Original invoice", "الفاتورة الأصلية"))
    kind = models.CharField(bi("Kind", "النوع"), max_length=6, choices=Kind.choices)
    lines = models.JSONField(bi("Lines (required, billed, delta)", "البنود (المطلوب، المفوتر، الفرق)"))
    supply_start = models.DateField(bi("Supply from", "التوريد من"))
    supply_end = models.DateField(bi("Supply to", "التوريد إلى"))
    document = models.ForeignKey("invoicing.SalesDocument", null=True, blank=True, on_delete=models.PROTECT,
                                 related_name="+", verbose_name=bi("Correction note", "إشعار التصحيح"))
    status = models.CharField(bi("Status", "الحالة"), max_length=18, choices=Status.choices,
                              default=Status.WAITING_ORIGINAL)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=bi("Reviewed by", "راجعه"))
    discard_reason_en = models.TextField(bi("Discard reason (English)", "سبب الاستبعاد (بالإنجليزية)"), blank=True)
    discard_reason_ar = models.TextField(bi("Discard reason (Arabic)", "سبب الاستبعاد (بالعربية)"), blank=True)
    message_en = models.TextField(bi("Last message (English)", "آخر رسالة (بالإنجليزية)"), blank=True)
    message_ar = models.TextField(bi("Last message (Arabic)", "آخر رسالة (بالعربية)"), blank=True)
    created_at = models.DateTimeField(bi("Created at", "تاريخ الإنشاء"), auto_now_add=True)
    updated_at = models.DateTimeField(bi("Updated at", "تاريخ التحديث"), auto_now=True)

    FROZEN = ("entity_id", "contract_id", "version_id", "trigger", "billing_period_id", "original_document_id", "kind",
              "lines", "supply_start", "supply_end")

    class Meta:
        verbose_name = bi("billing adjustment", "تصحيح فوترة")
        verbose_name_plural = bi("billing adjustments", "تصحيحات الفوترة")
        ordering = ["contract", "billing_period", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["billing_period", "original_document", "kind"],
                condition=Q(status__in=["waiting_original", "drafted", "submitted"]),
                name="billing_one_pending_adjustment",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            stored = type(self)._base_manager.filter(pk=self.pk).values(*self.FROZEN).first()
            if stored and any(stored[f] != getattr(self, f) for f in self.FROZEN):
                self._service_write = False
                raise error("Computed corrections are read-only (R42, OQ-26).", "التصحيحات المحسوبة للقراءة فقط.",
                            code="adjustment_frozen")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise error("Corrections are never deleted; they are discarded or superseded.",
                    "لا تُحذف التصحيحات؛ بل تُستبعد أو يحل غيرها محلها.", code="adjustment_delete")


# --- in-app notifications (R34, R51) -----------------------------------------------------------------

class BillingNotification(models.Model):
    class Kind(models.TextChoices):
        RUN_SUMMARY = "run_summary", bi("Run summary", "ملخص الدورة")
        FAILURE_ESCALATION = "failure_escalation", bi("Unresolved failures", "تعثرات غير محلولة")
        CORRECTIONS = "corrections", bi("Corrections summary", "ملخص التصحيحات")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
                                  verbose_name=bi("Recipient", "المستلم"))
    kind = models.CharField(bi("Kind", "النوع"), max_length=20, choices=Kind.choices)
    message_en = models.TextField(bi("Message (English)", "الرسالة (بالإنجليزية)"))
    message_ar = models.TextField(bi("Message (Arabic)", "الرسالة (بالعربية)"))
    run = models.ForeignKey(BillingRun, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    contract = models.ForeignKey(Contract, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(bi("Created at", "تاريخ الإنشاء"), auto_now_add=True)
    read_at = models.DateTimeField(bi("Read at", "تاريخ القراءة"), null=True, blank=True)

    class Meta:
        verbose_name = bi("billing notification", "إشعار الفوترة")
        verbose_name_plural = bi("billing notifications", "إشعارات الفوترة")
        ordering = ["-created_at", "-pk"]

    def message(self, language="en"):
        return self.message_ar if language == "ar" else self.message_en
