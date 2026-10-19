"""Payroll, slice 1: KSA monthly payroll end to end (docs/specs/payroll.md).

Every business model has the employing ``entity`` (CLAUDE.md, R1). Business logic lives in ``payroll.services``;
the guards here keep calculated, approved and append-only records from being changed outside those services
(R7, R13). PostgreSQL triggers (migration 0002) repeat the guards for writes that bypass the ORM.
Money is ``Decimal``: functional-currency (SAR) amounts 2 dp; a remuneration payslip's foreign currency up to 3 dp,
checked against that currency's own decimals (CLAUDE.md rule 7, R42). Loans carry no interest, profit, fee or
penalty field in any entity (R26, R48).
"""
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from core.models import Currency, Entity, bi
from hr.crypto import EncryptedTextField

ZERO = Decimal("0")


class PayrollError(ValidationError):
    """A broken payroll rule; the message carries English and Arabic ("English / العربية")."""

    def __init__(self, message, code=None, params=None):
        super().__init__(message, code=code, params=params)
        self.code = code

    def text(self, language="en"):
        parts = []
        for m in self.messages:
            en, sep, ar = m.rpartition(" / ")
            parts.append((ar if language == "ar" else en) if sep else m)
        return " ".join(parts)


def error(en, ar, code=None):
    return PayrollError(f"{en} / {ar}", code=code)


SERVICE_ONLY = ("This payroll record changes only through payroll.services.",
                "لا يتغير سجل الرواتب هذا إلا من خلال خدمات الرواتب.")
APPEND_ONLY = ("These payroll records are append-only.", "سجلات الرواتب هذه للإضافة فقط.")


class ServiceOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise error(*SERVICE_ONLY, code="service_only")

    def delete(self):
        raise error(*SERVICE_ONLY, code="service_only")


class AppendOnlyQuerySet(ServiceOnlyQuerySet):
    def update(self, **kwargs):
        raise error(*APPEND_ONLY, code="append_only")

    def delete(self):
        raise error(*APPEND_ONLY, code="append_only")


class ServiceWritten(models.Model):
    """Saved and deleted only by payroll.services, which set ``_service_write`` right before the one write."""

    objects = ServiceOnlyQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        allowed, self._service_write = getattr(self, "_service_write", False), False
        if not allowed:
            raise error(*SERVICE_ONLY, code="service_only")
        for f in self._meta.concrete_fields:
            if isinstance(f, models.DecimalField) and isinstance(getattr(self, f.attname), float):
                raise error("Amounts are Decimal, never float (R5).", "المبالغ أرقام عشرية لا أعداد عشرية ثنائية.",
                            code="float_rejected")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if not getattr(self, "_service_delete", False):
            raise error(*SERVICE_ONLY, code="service_only")
        return super().delete(*args, **kwargs)


class AppendOnly(models.Model):
    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.pk:
            raise error(*APPEND_ONLY, code="append_only")
        allowed, self._service_write = getattr(self, "_service_write", False), False
        if not allowed:
            raise error(*SERVICE_ONLY, code="service_only")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise error(*APPEND_ONLY, code="append_only")


def _money(label_en, label_ar, **kw):
    return models.DecimalField(bi(label_en, label_ar), max_digits=18, decimal_places=2, **kw)


def _txn(label_en, label_ar, **kw):
    return models.DecimalField(bi(label_en, label_ar), max_digits=21, decimal_places=3, **kw)


def _user(label_en, label_ar, null=True):
    return models.ForeignKey(settings.AUTH_USER_MODEL, null=null, blank=null, on_delete=models.PROTECT,
                             related_name="+", verbose_name=bi(label_en, label_ar))


class Tracked(models.Model):
    created_by = _user("Created by", "أنشأه", null=False)
    created_at = models.DateTimeField(bi("Created at", "تاريخ الإنشاء"), auto_now_add=True)
    modified_by = _user("Last modified by", "آخر من عدّله")
    modified_at = models.DateTimeField(bi("Last modified at", "تاريخ آخر تعديل"), null=True, blank=True)

    class Meta:
        abstract = True


class PayrollGroup(models.TextChoices):
    # R44 (P10): in_country = ksa_employee and local_entity_employee; out_of_country = remuneration.
    IN_COUNTRY = "in_country", bi("In-country payroll", "رواتب داخل المملكة")
    OUT_OF_COUNTRY = "out_of_country", bi("Out-of-country remuneration", "مكافآت خارج المملكة")


# --- settings (3.1) --------------------------------------------------------------------------------

class PayrollSettings(ServiceWritten):
    class SalaryBank(models.TextChoices):
        # P15: the five salary banks; codes as banking.Bank.
        RIYAD = "riyad", bi("Riyad Bank", "بنك الرياض")
        SNB = "snb", bi("Saudi National Bank", "البنك الأهلي السعودي")
        ALRAJHI = "alrajhi", bi("Al Rajhi Bank", "مصرف الراجحي")
        ALJAZIRA = "aljazira", bi("Bank AlJazira", "بنك الجزيرة")
        ALINMA = "alinma", bi("Alinma Bank", "مصرف الإنماء")

    class DayBasis(models.TextChoices):
        THIRTY = "thirty", bi("30-day month (Labour Law Art. 2)", "شهر من 30 يومًا (المادة 2)")
        CALENDAR = "calendar", bi("Calendar days", "أيام التقويم")

    entity = models.OneToOneField(Entity, on_delete=models.PROTECT, related_name="payroll_settings",
                                  verbose_name=bi("Entity", "الكيان"))
    enabled = models.BooleanField(bi("Payroll enabled", "الرواتب مفعلة"), default=False)
    pay_period = models.CharField(bi("Pay period", "فترة الرواتب"), max_length=20, default="calendar_month",
                                  choices=[("calendar_month", bi("Calendar month", "شهر ميلادي"))])
    default_pay_day = models.PositiveSmallIntegerField(bi("Default pay day", "يوم الصرف الافتراضي"), default=27)
    input_cutoff_day = models.PositiveSmallIntegerField(bi("In-country cut-off day", "يوم الإقفال داخل المملكة"),
                                                        default=27)
    out_of_country_cutoff_day = models.PositiveSmallIntegerField(
        bi("Out-of-country cut-off day (following month)", "يوم الإقفال خارج المملكة (الشهر التالي)"), default=6)
    salary_bank = models.CharField(bi("Salary bank", "بنك الرواتب"), max_length=10, choices=SalaryBank.choices,
                                   blank=True)
    day_basis = models.CharField(bi("Day basis", "أساس الأيام"), max_length=8, choices=DayBasis.choices,
                                 default=DayBasis.THIRTY)
    day_basis_ref = models.CharField(bi("Calendar-day basis reference", "مرجع اعتماد أيام التقويم"), max_length=200,
                                     blank=True)
    salary_bank_account = models.ForeignKey("ledger.Account", on_delete=models.PROTECT, related_name="+",
                                            verbose_name=bi("Salary bank account", "حساب بنك الرواتب"))
    wps_format = models.CharField(bi("WPS file format", "صيغة ملف حماية الأجور"), max_length=30, blank=True)
    wps_bank_code = EncryptedTextField(bi("Establishment ID at the bank", "رقم المنشأة لدى البنك"),
                                       plain_max_length=40, blank=True, default="")
    employer_bank_iban = EncryptedTextField(bi("Employer IBAN", "آيبان صاحب العمل"), plain_max_length=34, blank=True,
                                            default="")
    gosi_oh_rate_override = models.DecimalField(bi("GOSI occupational hazards rate override %",
                                                   "نسبة الأخطار المهنية المعدلة %"), max_digits=5, decimal_places=2,
                                                null=True, blank=True)
    gosi_oh_override_ref = models.CharField(bi("GOSI decision reference", "مرجع قرار التأمينات"), max_length=200,
                                            blank=True)
    # GOSI Regs Art. 22(3) (report 2026-10-19): the Governor's decision has a start and an end date.
    gosi_oh_override_from = models.DateField(bi("Override from", "التعديل من"), null=True, blank=True)
    gosi_oh_override_to = models.DateField(bi("Override to", "التعديل حتى"), null=True, blank=True)
    # GOSI Regs Art. 17(c): the Governor may exempt in-kind housing.
    gosi_housing_in_kind_exempt_ref = models.CharField(bi("GOSI in-kind housing exemption reference",
                                                          "مرجع إعفاء السكن العيني من التأمينات"), max_length=200,
                                                       blank=True)
    updated_by = _user("Updated by", "حدّثه")
    updated_at = models.DateTimeField(bi("Updated at", "تاريخ التحديث"), auto_now=True)

    class Meta:
        verbose_name = bi("payroll settings", "إعدادات الرواتب")
        verbose_name_plural = bi("payroll settings", "إعدادات الرواتب")

    def __str__(self):
        return self.entity.code


class PayrollSequence(models.Model):
    """Gap-free numbers per (entity, kind, year), locked with select_for_update (R6, OQ-27)."""

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    kind = models.CharField(bi("Kind", "النوع"), max_length=4)
    year = models.PositiveIntegerField(bi("Year", "السنة"))
    last_number = models.PositiveIntegerField(bi("Last number", "آخر رقم"), default=0)

    class Meta:
        verbose_name = bi("payroll sequence", "تسلسل الرواتب")
        verbose_name_plural = bi("payroll sequences", "تسلسلات الرواتب")
        constraints = [models.UniqueConstraint(fields=["entity", "kind", "year"], name="payroll_one_sequence")]


# --- inputs (3.2) ----------------------------------------------------------------------------------

class PayrollInput(Tracked, ServiceWritten):
    class Kind(models.TextChoices):
        OVERTIME_HOURS = "overtime_hours", bi("Overtime hours", "ساعات العمل الإضافي")
        UNPAID_DAYS = "unpaid_days", bi("Unpaid days (absence)", "أيام بدون أجر (غياب)")
        ONE_OFF_EARNING = "one_off_earning", bi("One-off earning", "استحقاق لمرة واحدة")
        BONUS = "bonus", bi("Bonus", "مكافأة")
        ONE_OFF_DEDUCTION = "one_off_deduction", bi("One-off deduction", "استقطاع لمرة واحدة")
        MATERNITY_SUPPORT = "maternity_support", bi("Temporary maternity support", "دعم مؤقت أثناء إجازة الوضع")
        # Never accepted in monthly runs: listed so the refusal is explicit (Art. 109(2); R54).
        LEAVE_ENCASHMENT = "leave_encashment", bi("Leave encashment", "بدل نقدي عن الإجازة")
        DISCIPLINARY_FINE = "disciplinary_fine", bi("Disciplinary fine", "جزاء تأديبي")

    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        APPROVED = "approved", bi("Approved", "معتمد")
        REJECTED = "rejected", bi("Rejected", "مرفوض")

    class DeductionBasis(models.TextChoices):
        WRITTEN_CONSENT = "written_consent", bi("Written consent (Art. 92)", "موافقة كتابية (المادة 92)")
        COURT_ORDER = "court_order", bi("Court order (Art. 92(6))", "حكم قضائي (المادة 92(6))")
        DAMAGE_ART91 = "damage_art91", bi("Damage recovery (Art. 91)", "تعويض إتلاف (المادة 91)")
        STATUTORY_OTHER = "statutory_other", bi("Other statutory deduction", "استقطاع نظامي آخر")

    EARNING_KINDS = (Kind.ONE_OFF_EARNING, Kind.BONUS, Kind.MATERNITY_SUPPORT)

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    employee = models.ForeignKey("hr.Employee", on_delete=models.PROTECT, related_name="payroll_inputs",
                                 verbose_name=bi("Employee", "الموظف"))
    period = models.CharField(bi("Work period (YYYY-MM)", "فترة العمل"), max_length=7)
    payroll_period = models.CharField(bi("Paid in period", "تُصرف في فترة"), max_length=7, blank=True)
    kind = models.CharField(bi("Kind", "النوع"), max_length=20, choices=Kind.choices)
    reference = models.CharField(bi("Reference", "المرجع"), max_length=40, blank=True)
    quantity = models.DecimalField(bi("Quantity (hours or days)", "الكمية (ساعات أو أيام)"), max_digits=8,
                                   decimal_places=2, null=True, blank=True)
    amount = _txn("Amount", "المبلغ", null=True, blank=True)
    component = models.ForeignKey("hr.SalaryComponent", null=True, blank=True, on_delete=models.PROTECT,
                                  related_name="+", verbose_name=bi("Component", "المكوّن"))
    reason_en = models.TextField(bi("Reason (English)", "السبب (بالإنجليزية)"), blank=True)
    reason_ar = models.TextField(bi("Reason (Arabic)", "السبب (بالعربية)"))
    overtime_date = models.DateField(bi("Overtime date", "تاريخ العمل الإضافي"), null=True, blank=True)
    art106_exceptional = models.BooleanField(bi("Art. 106 exceptional case", "حالة استثنائية وفق المادة 106"),
                                             default=False)
    worker_consent_ref = models.CharField(bi("Worker consent above 720 hours", "موافقة العامل فوق 720 ساعة"),
                                          max_length=200, blank=True)
    deduction_basis = models.CharField(bi("Deduction basis", "أساس الاستقطاع"), max_length=20,
                                       choices=DeductionBasis.choices, blank=True)
    consent_ref = models.CharField(bi("Written consent reference", "مرجع الموافقة الكتابية"), max_length=200,
                                   blank=True)
    legal_clearance_ref = models.CharField(bi("Written legal clearance", "الإذن القانوني المكتوب"), max_length=200,
                                           blank=True)
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.DRAFT)
    approved_by = _user("Approved by", "اعتمده")
    approved_at = models.DateTimeField(bi("Approved at", "تاريخ الاعتماد"), null=True, blank=True)
    # Review 9: set when an approved run takes the input (cleared if that run is reversed); a run takes the approved
    # inputs of its period or earlier that no approved run has taken yet.
    taken_run = models.ForeignKey("PayrollRun", null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                                  verbose_name=bi("Taken by run", "أخذه المسير"))

    class Meta:
        verbose_name = bi("payroll input", "مدخل رواتب")
        verbose_name_plural = bi("payroll inputs", "مدخلات الرواتب")
        ordering = ["entity", "period", "id"]
        constraints = [models.UniqueConstraint(fields=["entity", "employee", "period", "kind", "reference"],
                                               condition=~Q(reference=""), name="payroll_input_once")]

    def __str__(self):
        return f"{self.employee.employee_code} {self.kind} {self.period}"


# --- GOSI records entered by people (R61, R64, R65) ------------------------------------------------

class MaternityGosiCase(Tracked, ServiceWritten):
    """The GOSI maternity compensation case of an approved ``MATERNITY`` leave (hr 3.7 note; R61). Recorded by the
    hr_officer from GOSI; payroll reads it for every period inside the leave."""

    class Status(models.TextChoices):
        PENDING = "pending_gosi", bi("Pending GOSI decision", "بانتظار قرار التأمينات")
        CONFIRMED = "gosi_confirmed", bi("Confirmed by GOSI", "أكدته التأمينات")
        REFUSED = "gosi_refused", bi("Refused by GOSI", "رفضته التأمينات")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    employee = models.ForeignKey("hr.Employee", on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("Employee", "الموظف"))
    leave_request = models.OneToOneField("hr.LeaveRequest", on_delete=models.PROTECT, related_name="gosi_case",
                                         verbose_name=bi("Maternity leave", "إجازة الوضع"))
    status = models.CharField(bi("Status", "الحالة"), max_length=16, choices=Status.choices, default=Status.PENDING)
    gosi_reference = models.CharField(bi("GOSI reference", "مرجع التأمينات"), max_length=60, blank=True)
    confirmed_from = models.DateField(bi("Compensation from", "التعويض من"), null=True, blank=True)
    confirmed_to = models.DateField(bi("Compensation to", "التعويض حتى"), null=True, blank=True)
    monthly_amount = _money("GOSI monthly compensation", "التعويض الشهري من التأمينات", null=True, blank=True)
    refusal_reference = models.CharField(bi("GOSI refusal reference", "مرجع رفض التأمينات"), max_length=60, blank=True)
    medical_report_ref = models.CharField(bi("Medical report (Regs Art. 72)", "التقرير الطبي (المادة 72 من اللائحة)"),
                                          max_length=200, blank=True)
    legal_clearance_ref = models.CharField(bi("Written legal clearance for full salary", "الإذن القانوني المكتوب للأجر الكامل"),
                                           max_length=200, blank=True)
    clearance_approved_by = _user("Clearance approved by", "اعتمد الإذن")
    clearance_approved_at = models.DateTimeField(bi("Clearance approved at", "تاريخ اعتماد الإذن"), null=True,
                                                 blank=True)

    class Meta:
        verbose_name = bi("GOSI maternity case", "حالة تعويض أمومة لدى التأمينات")
        verbose_name_plural = bi("GOSI maternity cases", "حالات تعويض الأمومة لدى التأمينات")


class GosiAbroadPeriod(Tracked, ServiceWritten):
    """GOSI Regs Art. 23 (R64): a stay outside the Kingdom; OH stops for whole months strictly between the exit
    month and the return month unless the stay is for work."""

    class Purpose(models.TextChoices):
        PERSONAL = "personal", bi("Personal (not for work)", "شخصي (ليس للعمل)")
        WORK = "work", bi("For work", "للعمل")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    employee = models.ForeignKey("hr.Employee", on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("Employee", "الموظف"))
    exit_date = models.DateField(bi("Exit date", "تاريخ المغادرة"))
    reentry_date = models.DateField(bi("Re-entry date", "تاريخ العودة"), null=True, blank=True)
    purpose = models.CharField(bi("Purpose", "الغرض"), max_length=10, choices=Purpose.choices)

    class Meta:
        verbose_name = bi("stay abroad (GOSI)", "إقامة خارج المملكة (التأمينات)")
        verbose_name_plural = bi("stays abroad (GOSI)", "الإقامات خارج المملكة (التأمينات)")


class GosiVariableWageBase(Tracked, ServiceWritten):
    """GOSI Regs Art. 18(2) (R65): the GOSI amount of commission / profit-share pay is the previous year's monthly
    average actually paid; a new joiner takes a similar contributor's average."""

    class Method(models.TextChoices):
        PRIOR_YEAR = "prior_year_average", bi("Previous year's monthly average", "متوسط السنة السابقة الشهري")
        SIMILAR = "similar_contributor", bi("Similar contributor's average", "متوسط مشترك مماثل")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    employee = models.ForeignKey("hr.Employee", on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("Employee", "الموظف"))
    year = models.PositiveIntegerField(bi("Year applied", "السنة المطبقة"))
    amount = _money("Monthly GOSI amount", "المبلغ الشهري الخاضع للاشتراك")
    method = models.CharField(bi("Method", "الطريقة"), max_length=20, choices=Method.choices)
    source_note = models.CharField(bi("Source note", "مصدر الرقم"), max_length=300)

    class Meta:
        verbose_name = bi("GOSI variable wage base", "أساس الأجر المتغير للتأمينات")
        verbose_name_plural = bi("GOSI variable wage bases", "أسس الأجر المتغير للتأمينات")
        constraints = [models.UniqueConstraint(fields=["employee", "year"], name="payroll_variable_base_once")]


# --- runs, payslips (3.3, 3.4) ---------------------------------------------------------------------

class PayrollRun(Tracked, ServiceWritten):
    """[Reg] One monthly run of a payroll group (R6, R7, R11). Approval posts its entry (R11)."""

    class Kind(models.TextChoices):
        REGULAR = "regular", bi("Regular", "عادي")
        OFF_CYCLE = "off_cycle", bi("Off-cycle", "خارج الدورة")

    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        CALCULATED = "calculated", bi("Calculated", "محتسب")
        SUBMITTED = "submitted", bi("Submitted for review", "مقدّم للمراجعة")
        RETURNED = "returned", bi("Returned", "معاد")
        APPROVED = "approved", bi("Approved and posted", "معتمد ومرحّل")
        PAID = "paid", bi("Paid", "مدفوع")
        REVERSED = "reversed", bi("Reversed", "معكوس")

    # R7: the only moves; the PostgreSQL trigger repeats them.
    TRANSITIONS = frozenset({
        ("draft", "calculated"), ("calculated", "calculated"), ("calculated", "submitted"),
        ("submitted", "returned"), ("returned", "calculated"), ("submitted", "approved"), ("approved", "paid"),
        ("approved", "reversed"),
    })

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    payroll_group = models.CharField(bi("Payroll group", "مجموعة الرواتب"), max_length=16, choices=PayrollGroup.choices)
    number = models.CharField(bi("Number", "الرقم"), max_length=40, blank=True)
    period = models.CharField(bi("Period (YYYY-MM)", "الفترة"), max_length=7)
    kind = models.CharField(bi("Kind", "النوع"), max_length=10, choices=Kind.choices, default=Kind.REGULAR)
    reason_en = models.TextField(bi("Reason (English)", "السبب (بالإنجليزية)"), blank=True)
    reason_ar = models.TextField(bi("Reason (Arabic)", "السبب (بالعربية)"), blank=True)
    pay_date = models.DateField(bi("Pay date", "تاريخ الصرف"))
    entry_date = models.DateField(bi("Entry date", "تاريخ القيد"))
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.DRAFT)
    calc_version = models.PositiveIntegerField(bi("Calculation version", "نسخة الاحتساب"), default=0)
    inputs_hash = models.CharField(bi("Inputs hash", "بصمة المدخلات"), max_length=64, blank=True)
    calculated_by = _user("Calculated by", "احتسبه")
    calculated_at = models.DateTimeField(bi("Calculated at", "تاريخ الاحتساب"), null=True, blank=True)
    submitted_by = _user("Submitted by", "قدّمه")
    submitted_at = models.DateTimeField(bi("Submitted at", "تاريخ التقديم"), null=True, blank=True)
    approved_by = _user("Approved by", "اعتمده")
    approved_at = models.DateTimeField(bi("Approved at", "تاريخ الاعتماد"), null=True, blank=True)
    returned_reason_ar = models.TextField(bi("Return reason (Arabic)", "سبب الإعادة (بالعربية)"), blank=True)
    journal_entry = models.OneToOneField("ledger.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                         related_name="+", verbose_name=bi("Journal entry", "القيد"))
    reversal_entry = models.OneToOneField("ledger.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                          related_name="+", verbose_name=bi("Reversal entry", "قيد العكس"))
    reversed_by = _user("Reversed by", "عكسه")
    reversed_at = models.DateTimeField(bi("Reversed at", "تاريخ العكس"), null=True, blank=True)
    warnings = models.JSONField(bi("Warnings", "تنبيهات"), default=list, blank=True)
    # Review 13: per-employee notes of the run (employee codes), shown only with payroll.view_payslip; ``warnings``
    # holds counts.
    employee_notes = models.JSONField(bi("Employee notes", "ملاحظات الموظفين"), default=list, blank=True)
    parameter_ids = models.JSONField(bi("Parameters used", "المعايير المستخدمة"), default=list, blank=True)
    off_cycle_employees = models.ManyToManyField("hr.Employee", blank=True, related_name="+",
                                                 verbose_name=bi("Employees (off-cycle)", "الموظفون (خارج الدورة)"))

    class Meta:
        verbose_name = bi("payroll run", "مسير رواتب")
        verbose_name_plural = bi("payroll runs", "مسيرات الرواتب")
        ordering = ["entity", "-period", "payroll_group"]
        constraints = [
            # R6, R44: one live regular run per (entity, period, group); a reversed run may be replaced (R13).
            models.UniqueConstraint(fields=["entity", "period", "payroll_group"],
                                    condition=Q(kind="regular") & ~Q(status="reversed"), name="payroll_one_regular_run"),
            models.UniqueConstraint(fields=["entity", "number"], condition=~Q(number=""), name="payroll_run_number"),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            stored = type(self)._base_manager.filter(pk=self.pk).values_list("status", flat=True).first()
            if stored is not None and stored != self.status and (stored, self.status) not in self.TRANSITIONS:
                raise error(f"A payroll run does not move from {stored} to {self.status} (R7).",
                            f"لا ينتقل مسير الرواتب من {stored} إلى {self.status}.", code="run_status")
        elif self.status != self.Status.DRAFT:
            raise error("A payroll run is created as a draft (R7).", "يُنشأ مسير الرواتب مسودة.", code="run_status")
        super().save(*args, **kwargs)

    def __str__(self):
        return self.number or f"{self.entity.code} {self.period} {self.payroll_group} ({self.status})"


class Payslip(ServiceWritten):
    class NationalityClass(models.TextChoices):
        SAUDI = "saudi", bi("Saudi", "سعودي")
        NON_SAUDI = "non_saudi", bi("Non-Saudi", "غير سعودي")
        GCC = "gcc", bi("GCC national", "مواطن خليجي")

    class Maternity(models.TextChoices):
        NONE = "none", bi("None", "لا يوجد")
        PENDING = "pending_gosi", bi("Pending GOSI", "بانتظار التأمينات")
        CONFIRMED = "gosi_confirmed", bi("GOSI confirmed", "أكدته التأمينات")
        REFUSED = "gosi_refused", bi("GOSI refused", "رفضته التأمينات")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    run = models.ForeignKey(PayrollRun, on_delete=models.PROTECT, related_name="payslips",
                            verbose_name=bi("Run", "المسير"))
    employee = models.ForeignKey("hr.Employee", on_delete=models.PROTECT, related_name="payslips",
                                 verbose_name=bi("Employee", "الموظف"))
    contract = models.ForeignKey("hr.EmploymentContract", null=True, blank=True, on_delete=models.PROTECT,
                                 related_name="+", verbose_name=bi("Contract version", "نسخة العقد"))
    employment_basis = models.CharField(bi("Employment basis", "أساس التوظيف"), max_length=24)
    nationality_class = models.CharField(bi("Nationality class", "فئة الجنسية"), max_length=10,
                                         choices=NationalityClass.choices)
    cohort = models.CharField(bi("GOSI cohort", "فئة التأمينات"), max_length=10, blank=True)
    currency = models.ForeignKey(Currency, to_field="code", on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("Currency", "العملة"))
    fx_rate = models.DecimalField(bi("Rate to SAR", "سعر التحويل"), max_digits=20, decimal_places=10, default=1)
    paid_days = models.DecimalField(bi("Paid days", "الأيام المدفوعة"), max_digits=6, decimal_places=2, default=ZERO)
    gross = _money("Gross", "الإجمالي", default=ZERO)
    total_deductions = _money("Deductions", "الاستقطاعات", default=ZERO)
    net = _money("Net", "الصافي", default=ZERO)
    gross_txn = _txn("Gross (payslip currency)", "الإجمالي (عملة القسيمة)", default=ZERO)
    net_txn = _txn("Net (payslip currency)", "الصافي (عملة القسيمة)", default=ZERO)
    employer_gosi = _money("Employer GOSI", "حصة صاحب العمل في التأمينات", default=ZERO)
    employee_gosi = _money("Employee GOSI", "حصة الموظف في التأمينات", default=ZERO)
    eosb_accrual = _money("EOSB accrual", "استحقاق مكافأة نهاية الخدمة", default=ZERO)
    gosi_wage = _money("GOSI wage", "الأجر الخاضع للاشتراك", default=ZERO)
    gosi_wage_full = _money("Monthly GOSI wage (before partial-month proration)", "الأجر الشهري الخاضع للاشتراك",
                            default=ZERO)
    maternity_status = models.CharField(bi("Maternity status", "حالة الأمومة"), max_length=16,
                                        choices=Maternity.choices, default=Maternity.NONE)
    maternity_days = models.DecimalField(bi("Days covered by GOSI compensation", "أيام تعويض التأمينات"), max_digits=6,
                                         decimal_places=2, default=ZERO)
    gosi_benefit_amount = _money("GOSI compensation for those days", "تعويض التأمينات عن تلك الأيام", default=ZERO)
    maternity_top_up = _money("Maternity top-up", "فرق الأجر أثناء الأمومة", default=ZERO)
    pending_gosi_amount = _money("Salary carried pending GOSI", "أجر مُرحّل بانتظار التأمينات", default=ZERO)
    legal_clearance_ref = models.CharField(bi("Written legal clearance", "الإذن القانوني المكتوب"), max_length=200,
                                           blank=True)
    leave_balance_snapshot = models.DecimalField(bi("Annual leave balance", "رصيد الإجازة السنوية"), max_digits=7,
                                                 decimal_places=2, default=ZERO)
    loan_balance = _money("Loan balance after this run", "رصيد القرض بعد هذا المسير", default=ZERO)
    document_flag = models.CharField(bi("Document flag", "حالة المستندات"), max_length=10, blank=True)
    refused = models.BooleanField(bi("Calculation refused", "رُفض الاحتساب"), default=False)
    refusal = models.TextField(bi("Refusal reason (en/ar)", "سبب الرفض"), blank=True)
    warnings = models.JSONField(bi("Warnings", "تنبيهات"), default=list, blank=True)
    notes = models.JSONField(bi("Notes", "ملاحظات"), default=list, blank=True)
    input_ids = models.JSONField(bi("Inputs taken", "المدخلات المأخوذة"), default=list, blank=True)

    class Meta:
        verbose_name = bi("payslip", "قسيمة راتب")
        verbose_name_plural = bi("payslips", "قسائم الرواتب")
        ordering = ["run", "employee__employee_code"]
        constraints = [models.UniqueConstraint(fields=["run", "employee"], name="payroll_one_payslip")]

    def __str__(self):
        return f"{self.employee.employee_code} {self.run.period}"


class PayslipLine(ServiceWritten):
    class LineType(models.TextChoices):
        EARNING = "earning", bi("Earning", "استحقاق")
        OVERTIME = "overtime", bi("Overtime", "عمل إضافي")
        ONE_OFF_EARNING = "one_off_earning", bi("One-off earning", "استحقاق لمرة واحدة")
        BONUS = "bonus", bi("Bonus", "مكافأة")
        RETRO = "retro", bi("Retro difference", "فرق بأثر رجعي")
        MATERNITY_TOP_UP = "maternity_top_up", bi("Maternity top-up", "فرق الأجر أثناء الأمومة")
        MATERNITY_SUPPORT = "maternity_support", bi("Maternity support", "دعم أثناء الأمومة")
        UNPAID_DEDUCTION = "unpaid_deduction", bi("Unpaid days", "أيام بدون أجر")
        SICK_DEDUCTION = "sick_deduction", bi("Sick-leave reduction", "تخفيض الإجازة المرضية")
        MATERNITY_SUSPENDED = "maternity_suspended", bi("Salary suspended (GOSI compensation)",
                                                        "أجر موقوف (تعويض التأمينات)")
        DEDUCTION = "deduction", bi("Deduction", "استقطاع")
        EMPLOYEE_GOSI = "employee_gosi", bi("Employee GOSI", "حصة الموظف في التأمينات")
        GOSI_CATCH_UP = "gosi_catch_up", bi("GOSI catch-up", "استدراك حصة التأمينات")
        LOAN_INSTALLMENT = "loan_installment", bi("Loan installment (interest-free)", "قسط قرض حسن")
        DEFERRED_SALE_INSTALLMENT = "deferred_sale_installment", bi("Bai' ajel installment", "قسط البيع الآجل")
        EMPLOYER_GOSI = "employer_gosi", bi("Employer GOSI", "حصة صاحب العمل في التأمينات")
        EOSB_ACCRUAL = "eosb_accrual", bi("End-of-service provision", "مخصص نهاية الخدمة")
        EOSB_RELEASE = "eosb_release", bi("End-of-service provision released", "تحرير مخصص نهاية الخدمة")

    # Gross = positive earnings - reductions; net = gross - deductions; employer costs are not on the net.
    EARNINGS = frozenset({"earning", "overtime", "one_off_earning", "bonus", "retro", "maternity_top_up",
                          "maternity_support"})
    REDUCTIONS = frozenset({"unpaid_deduction", "sick_deduction", "maternity_suspended"})
    DEDUCTIONS = frozenset({"deduction", "employee_gosi", "gosi_catch_up", "loan_installment",
                            "deferred_sale_installment"})
    EMPLOYER = frozenset({"employer_gosi", "eosb_accrual", "eosb_release"})

    class Branch(models.TextChoices):
        ANNUITIES = "annuities", bi("Annuities", "المعاشات")
        SANED = "saned", bi("Unemployment insurance (SANED)", "التأمين ضد التعطل عن العمل (ساند)")
        OH = "occupational_hazards", bi("Occupational hazards and additional compensations",
                                        "الأخطار المهنية والتعويضات الإضافية")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    payslip = models.ForeignKey(Payslip, on_delete=models.PROTECT, related_name="lines",
                                verbose_name=bi("Payslip", "القسيمة"))
    line_type = models.CharField(bi("Line type", "نوع البند"), max_length=28, choices=LineType.choices)
    component = models.ForeignKey("hr.SalaryComponent", null=True, blank=True, on_delete=models.PROTECT,
                                  related_name="+", verbose_name=bi("Component", "المكوّن"))
    gosi_branch = models.CharField(bi("GOSI branch", "فرع التأمينات"), max_length=24, choices=Branch.choices,
                                   blank=True)
    quantity = models.DecimalField(bi("Quantity", "الكمية"), max_digits=10, decimal_places=2, null=True, blank=True)
    rate = models.DecimalField(bi("Rate", "المعدل"), max_digits=12, decimal_places=4, null=True, blank=True)
    amount = _money("Amount (SAR)", "المبلغ (ريال)")
    currency = models.ForeignKey(Currency, to_field="code", on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("Currency", "العملة"))
    amount_txn = _txn("Amount (payslip currency)", "المبلغ (عملة القسيمة)")
    account = models.ForeignKey("ledger.Account", on_delete=models.PROTECT, related_name="+",
                                verbose_name=bi("Account", "الحساب"))
    description_en = models.CharField(bi("Description (English)", "الوصف (بالإنجليزية)"), max_length=300)
    description_ar = models.CharField(bi("Description (Arabic)", "الوصف (بالعربية)"), max_length=300)
    source_ref = models.CharField(bi("Source", "المصدر"), max_length=80, blank=True)
    origin_period = models.CharField(bi("Period of origin", "فترة المنشأ"), max_length=7, blank=True)

    class Meta:
        verbose_name = bi("payslip line", "بند قسيمة")
        verbose_name_plural = bi("payslip lines", "بنود القسائم")
        ordering = ["payslip", "id"]
        constraints = [models.CheckConstraint(condition=Q(amount__gte=0) & Q(amount_txn__gte=0),
                                              name="payroll_line_amount_positive")]


class EosbProvisionEntry(AppendOnly):
    """Append-only sub-ledger of 2500 (3.5; R24, R25). Employee balance = sum of amount."""

    class Kind(models.TextChoices):
        OPENING = "opening", bi("Opening", "افتتاحي")
        ACCRUAL = "accrual", bi("Accrual", "استحقاق")
        RELEASE = "release", bi("Release", "تحرير")
        UTILISATION = "utilisation", bi("Utilisation", "استخدام")
        REVERSAL = "reversal", bi("Reversal", "عكس")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    employee = models.ForeignKey("hr.Employee", on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("Employee", "الموظف"))
    date = models.DateField(bi("Date", "التاريخ"))
    kind = models.CharField(bi("Kind", "النوع"), max_length=12, choices=Kind.choices)
    amount = _money("Amount (signed)", "المبلغ (بإشارته)")
    run = models.ForeignKey(PayrollRun, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                            verbose_name=bi("Run", "المسير"))
    required_provision = _money("Required provision", "المخصص المطلوب", null=True, blank=True)
    created_at = models.DateTimeField(bi("Created at", "تاريخ الإنشاء"), auto_now_add=True)

    class Meta:
        verbose_name = bi("EOSB provision entry", "قيد مخصص نهاية الخدمة")
        verbose_name_plural = bi("EOSB provision entries", "قيود مخصص نهاية الخدمة")
        ordering = ["employee", "date", "id"]


class GosiCatchUpNotice(ServiceWritten):
    """R58 (P19(j)): the employee is told that a missed GOSI share of an earlier month is deducted later."""

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    employee = models.ForeignKey("hr.Employee", on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("Employee", "الموظف"))
    run = models.ForeignKey(PayrollRun, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Run", "المسير"))
    period_of_origin = models.CharField(bi("Period of origin", "فترة المنشأ"), max_length=7)
    amount = _money("Amount", "المبلغ")
    text_en = models.TextField(bi("Text (English)", "النص (بالإنجليزية)"))
    text_ar = models.TextField(bi("Text (Arabic)", "النص (بالعربية)"))
    notified_on = models.DateField(bi("Notified on", "تاريخ الإبلاغ"), null=True, blank=True)
    channel = models.CharField(bi("Channel", "وسيلة الإبلاغ"), max_length=60, blank=True)
    recorded_by = _user("Recorded by", "سجّله")
    created_at = models.DateTimeField(bi("Created at", "تاريخ الإنشاء"), auto_now_add=True)

    class Meta:
        verbose_name = bi("GOSI catch-up notice", "إشعار استدراك التأمينات")
        verbose_name_plural = bi("GOSI catch-up notices", "إشعارات استدراك التأمينات")


# --- loans (3.6) -----------------------------------------------------------------------------------

class EmployeeLoan(Tracked, ServiceWritten):
    """[Reg] Interest-free loan or salary advance (R26, R27, R46). No interest, profit, fee or penalty field."""

    class Kind(models.TextChoices):
        LOAN = "loan", bi("Interest-free loan (qard hasan)", "قرض حسن")
        SALARY_ADVANCE = "salary_advance", bi("Salary advance", "سلفة على الراتب")

    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        SUBMITTED = "submitted", bi("Submitted", "مقدّم")
        ACTIVE = "active", bi("Approved and disbursed", "معتمد ومصروف")
        SETTLED = "settled", bi("Settled", "مسدد")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    employee = models.ForeignKey("hr.Employee", on_delete=models.PROTECT, related_name="loans",
                                 verbose_name=bi("Employee", "الموظف"))
    number = models.CharField(bi("Number", "الرقم"), max_length=40, blank=True)
    kind = models.CharField(bi("Kind", "النوع"), max_length=16, choices=Kind.choices)
    principal = _money("Principal", "أصل القرض")
    disbursement_date = models.DateField(bi("Disbursement date", "تاريخ الصرف"))
    disbursement_account = models.ForeignKey("ledger.Account", on_delete=models.PROTECT, related_name="+",
                                             verbose_name=bi("Paid from account", "يُصرف من حساب"))
    purpose_en = models.TextField(bi("Purpose (English)", "الغرض (بالإنجليزية)"), blank=True)
    purpose_ar = models.TextField(bi("Purpose (Arabic)", "الغرض (بالعربية)"))
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.DRAFT)
    schedule_version = models.PositiveIntegerField(bi("Schedule version", "نسخة الجدول"), default=1)
    interest_free = models.BooleanField(bi("Interest-free", "بدون فوائد"), default=True, editable=False)
    cap_basic_at_approval = _money("Basic salary at approval", "الراتب الأساسي عند الاعتماد", null=True, blank=True)
    cap_amount = _money("Cap (3 x basic)", "الحد (3 رواتب أساسية)", null=True, blank=True)
    submitted_by = _user("Submitted by", "قدّمه")
    submitted_at = models.DateTimeField(bi("Submitted at", "تاريخ التقديم"), null=True, blank=True)
    approved_by = _user("Approved by", "اعتمده")
    approved_at = models.DateTimeField(bi("Approved at", "تاريخ الاعتماد"), null=True, blank=True)
    journal_entry = models.OneToOneField("ledger.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                         related_name="+", verbose_name=bi("Disbursement entry", "قيد الصرف"))

    class Meta:
        verbose_name = bi("employee loan", "قرض موظف")
        verbose_name_plural = bi("employee loans", "قروض الموظفين")
        constraints = [models.CheckConstraint(condition=Q(interest_free=True), name="payroll_loan_interest_free"),
                       models.CheckConstraint(condition=Q(principal__gt=0), name="payroll_loan_principal_positive")]

    def __str__(self):
        return self.number or f"{self.employee.employee_code} loan {self.pk}"


class LoanInstallment(ServiceWritten):
    class Status(models.TextChoices):
        PROPOSED = "proposed", bi("Proposed (schedule awaiting approval)", "مقترح (جدول بانتظار الاعتماد)")
        PLANNED = "planned", bi("Planned", "مخطط")
        DEDUCTED = "deducted", bi("Deducted", "مستقطع")
        SUPERSEDED = "superseded", bi("Superseded by a new schedule", "استُبدل بجدول جديد")
        SETTLED_IN_FINAL = "settled_in_final", bi("Settled in the final settlement", "سُدد في المخالصة النهائية")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    loan = models.ForeignKey(EmployeeLoan, on_delete=models.PROTECT, related_name="installments",
                             verbose_name=bi("Loan", "القرض"))
    schedule_version = models.PositiveIntegerField(bi("Schedule version", "نسخة الجدول"))
    period = models.CharField(bi("Period", "الفترة"), max_length=7)
    amount = _money("Amount", "المبلغ")
    deducted_amount = _money("Deducted", "المستقطع", default=ZERO)
    status = models.CharField(bi("Status", "الحالة"), max_length=18, choices=Status.choices, default=Status.PLANNED)
    run = models.ForeignKey(PayrollRun, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                            verbose_name=bi("Run", "المسير"))
    carried_from = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                                     verbose_name=bi("Remainder of", "باقي القسط"))

    class Meta:
        verbose_name = bi("loan installment", "قسط قرض")
        verbose_name_plural = bi("loan installments", "أقساط القروض")
        ordering = ["loan", "period", "id"]
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name="payroll_installment_positive")]


# --- bai' ajel (3.11; sale through invoicing R112, not built) ----------------------------------------

class DeferredSaleContract(Tracked, ServiceWritten):
    """Bai' ajel employee financing (R47 to R49). The sale itself is an invoicing document (invoicing R112, E28),
    reached through ``payroll.invoicing_port``. No interest, penalty, late-fee or rate field exists."""

    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        TERMS_SUBMITTED = "terms_submitted", bi("Terms submitted", "الشروط مقدمة")
        TERMS_APPROVED = "terms_approved", bi("Terms approved", "الشروط معتمدة")
        SOLD = "sold", bi("Sold (invoice posted)", "مبيع (الفاتورة مرحّلة)")
        SETTLED = "settled", bi("Settled", "مسدد")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    employee = models.ForeignKey("hr.Employee", on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("Employee", "الموظف"))
    number = models.CharField(bi("Number", "الرقم"), max_length=40, blank=True)
    asset_description_en = models.TextField(bi("Asset (English)", "الأصل (بالإنجليزية)"), blank=True)
    asset_description_ar = models.TextField(bi("Asset (Arabic)", "الأصل (بالعربية)"))
    term_months = models.PositiveSmallIntegerField(bi("Term (months)", "المدة (أشهر)"))
    cost = _money("Cost", "التكلفة")
    profit_amount = _money("Fixed profit", "الربح الثابت")
    vat_amount = _money("VAT charged to the employee", "ضريبة القيمة المضافة على الموظف", default=ZERO)
    first_period = models.CharField(bi("First installment period", "فترة القسط الأول"), max_length=7)
    consent_ref = models.CharField(bi("Signed consent to salary deductions", "الموافقة الموقعة على الاستقطاع"),
                                   max_length=200, blank=True)
    final_dues_undertaking = models.BooleanField(bi("Undertaking: deduct from final dues", "تعهد بالاستقطاع من المستحقات النهائية"),
                                                 default=False)
    sale_document_ref = models.CharField(bi("Sale invoice (invoicing)", "فاتورة البيع"), max_length=60, blank=True)
    status = models.CharField(bi("Status", "الحالة"), max_length=16, choices=Status.choices, default=Status.DRAFT)
    submitted_by = _user("Submitted by", "قدّمه")
    terms_approved_by = _user("Terms approved by", "اعتمد الشروط")
    terms_approved_at = models.DateTimeField(bi("Terms approved at", "تاريخ اعتماد الشروط"), null=True, blank=True)

    class Meta:
        verbose_name = bi("bai' ajel contract", "عقد بيع آجل")
        verbose_name_plural = bi("bai' ajel contracts", "عقود البيع الآجل")

    @property
    def sale_price(self):
        return self.cost + self.profit_amount

    @property
    def receivable(self):
        """P20(h): VAT on the sale price is charged to and paid by the employee."""
        return self.sale_price + self.vat_amount


class DeferredSaleInstallment(ServiceWritten):
    class Status(models.TextChoices):
        PLANNED = "planned", bi("Planned", "مخطط")
        DEDUCTED = "deducted", bi("Deducted", "مستقطع")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    contract = models.ForeignKey(DeferredSaleContract, on_delete=models.PROTECT, related_name="installments",
                                 verbose_name=bi("Contract", "العقد"))
    period = models.CharField(bi("Period", "الفترة"), max_length=7)
    amount = _money("Amount", "المبلغ")
    deducted_amount = _money("Deducted", "المستقطع", default=ZERO)
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.PLANNED)
    run = models.ForeignKey(PayrollRun, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                            verbose_name=bi("Run", "المسير"))
    carried_from = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                                     verbose_name=bi("Remainder of", "باقي القسط"))

    class Meta:
        verbose_name = bi("bai' ajel installment", "قسط بيع آجل")
        verbose_name_plural = bi("bai' ajel installments", "أقساط البيع الآجل")
        ordering = ["contract", "period", "id"]


# --- payments (3.7) --------------------------------------------------------------------------------

class PayrollPayment(Tracked, ServiceWritten):
    """[Reg] Net salaries (E3, E8b) or GOSI (E2) paid from the bank, approved (= posted) by the approver (R29)."""

    class Kind(models.TextChoices):
        NET_SALARIES = "net_salaries", bi("Net salaries", "صافي الرواتب")
        GOSI = "gosi", bi("GOSI contributions", "اشتراكات التأمينات")

    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        SUBMITTED = "submitted", bi("Submitted", "مقدّم")
        APPROVED = "approved", bi("Approved and posted", "معتمد ومرحّل")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    kind = models.CharField(bi("Kind", "النوع"), max_length=14, choices=Kind.choices)
    run = models.ForeignKey(PayrollRun, null=True, blank=True, on_delete=models.PROTECT, related_name="payments",
                            verbose_name=bi("Run", "المسير"))
    gosi_period = models.CharField(bi("GOSI period", "فترة التأمينات"), max_length=7, blank=True)
    payment_date = models.DateField(bi("Payment date", "تاريخ الدفع"))
    bank_account = models.ForeignKey("ledger.Account", on_delete=models.PROTECT, related_name="+",
                                     verbose_name=bi("Bank account", "الحساب البنكي"))
    currency = models.ForeignKey(Currency, to_field="code", on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("Currency", "العملة"))
    amount = _txn("Amount (gross of WHT)", "المبلغ (قبل ضريبة الاستقطاع)")
    wht_category = models.ForeignKey("invoicing.WhtCategory", null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+", verbose_name=bi("WHT category", "فئة ضريبة الاستقطاع"))
    wht_amount = _txn("WHT (payment currency)", "ضريبة الاستقطاع (عملة الدفع)", default=ZERO)
    wht_amount_sar = _money("WHT (SAR)", "ضريبة الاستقطاع (ريال)", default=ZERO)
    bank_reference = models.CharField(bi("Bank reference", "المرجع البنكي"), max_length=60, blank=True)
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.DRAFT)
    submitted_by = _user("Submitted by", "قدّمه")
    submitted_at = models.DateTimeField(bi("Submitted at", "تاريخ التقديم"), null=True, blank=True)
    approved_by = _user("Approved by", "اعتمده")
    approved_at = models.DateTimeField(bi("Approved at", "تاريخ الاعتماد"), null=True, blank=True)
    journal_entry = models.OneToOneField("ledger.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                         related_name="+", verbose_name=bi("Journal entry", "القيد"))

    class Meta:
        verbose_name = bi("payroll payment", "دفعة رواتب")
        verbose_name_plural = bi("payroll payments", "دفعات الرواتب")
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name="payroll_payment_positive")]


# --- WPS and Mudad (3.9) ---------------------------------------------------------------------------

class WpsFile(ServiceWritten):
    """A generated salary file. Kept indefinitely, encrypted (P19(m), R31); after 10 years it is only nominated for a
    human "burn" decision (hr R67), never deleted automatically."""

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    run = models.ForeignKey(PayrollRun, on_delete=models.PROTECT, related_name="wps_files",
                            verbose_name=bi("Run", "المسير"))
    format_code = models.CharField(bi("Format", "الصيغة"), max_length=30)
    format_parameter = models.ForeignKey("hr.StatutoryParameter", null=True, blank=True, on_delete=models.PROTECT,
                                         related_name="+", verbose_name=bi("Format parameter", "معيار الصيغة"))
    generated_by = _user("Generated by", "أنشأه", null=False)
    generated_at = models.DateTimeField(bi("Generated at", "تاريخ الإنشاء"), auto_now_add=True)
    sha256 = models.CharField(bi("SHA-256", "البصمة"), max_length=64)
    employee_count = models.PositiveIntegerField(bi("Employees", "عدد الموظفين"))
    total_amount = _money("Total", "الإجمالي")
    exceptions = models.JSONField(bi("Exceptions", "الاستثناءات"), default=list, blank=True)
    content = EncryptedTextField(bi("File (encrypted)", "الملف (مشفر)"))
    bank_processed_content = EncryptedTextField(bi("Bank-processed file (encrypted)", "ملف البنك المعالج (مشفر)"),
                                                blank=True, default="")
    bank_processed_sha256 = models.CharField(bi("Bank-processed SHA-256", "بصمة ملف البنك"), max_length=64, blank=True)
    bank_processed_received_at = models.DateTimeField(bi("Bank file received at", "تاريخ استلام ملف البنك"), null=True,
                                                      blank=True)
    nominated_for_burn_at = models.DateTimeField(bi("Nominated for a burn decision", "رُشح لقرار الإتلاف"), null=True,
                                                 blank=True)

    class Meta:
        verbose_name = bi("WPS file", "ملف حماية الأجور")
        verbose_name_plural = bi("WPS files", "ملفات حماية الأجور")

    def delete(self, *args, **kwargs):
        raise error("WPS files are kept indefinitely; destruction is a human burn decision (P19(m), R31).",
                    "تُحفظ ملفات حماية الأجور دون حد زمني؛ والإتلاف قرار بشري.", code="retention")


class MudadSubmission(ServiceWritten):
    class Status(models.TextChoices):
        SUBMITTED = "submitted", bi("Submitted", "مرفوع")
        ACCEPTED = "accepted", bi("Accepted", "مقبول")
        REJECTED = "rejected", bi("Rejected", "مرفوض")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    run = models.ForeignKey(PayrollRun, on_delete=models.PROTECT, related_name="mudad_submissions",
                            verbose_name=bi("Run", "المسير"))
    wps_file = models.ForeignKey(WpsFile, on_delete=models.PROTECT, related_name="+", verbose_name=bi("File", "الملف"))
    submitted_on = models.DateField(bi("Submitted on", "تاريخ الرفع"))
    reference = models.CharField(bi("Mudad reference", "مرجع مدد"), max_length=60)
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices)
    message_en = models.TextField(bi("Message (English)", "الرسالة (بالإنجليزية)"), blank=True)
    message_ar = models.TextField(bi("Message (Arabic)", "الرسالة (بالعربية)"), blank=True)
    justifications = models.JSONField(bi("Justifications", "المبررات"), default=list, blank=True)
    recorded_by = _user("Recorded by", "سجّله", null=False)
    created_at = models.DateTimeField(bi("Created at", "تاريخ الإنشاء"), auto_now_add=True)

    class Meta:
        verbose_name = bi("Mudad submission", "رفع على مدد")
        verbose_name_plural = bi("Mudad submissions", "عمليات الرفع على مدد")


class GosiReconciliation(ServiceWritten):
    """R23: the GOSI monthly invoice against the ERP contributions; reported, never posted."""

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    period = models.CharField(bi("Period", "الفترة"), max_length=7)
    gosi_invoice_amount = _money("GOSI invoice", "فاتورة التأمينات")
    erp_amount = _money("ERP contributions", "اشتراكات النظام")
    difference = _money("Difference", "الفرق")
    note_en = models.TextField(bi("Note (English)", "ملاحظة (بالإنجليزية)"), blank=True)
    note_ar = models.TextField(bi("Note (Arabic)", "ملاحظة (بالعربية)"), blank=True)
    recorded_by = _user("Recorded by", "سجّله", null=False)
    created_at = models.DateTimeField(bi("Created at", "تاريخ الإنشاء"), auto_now_add=True)

    class Meta:
        verbose_name = bi("GOSI reconciliation", "مطابقة التأمينات")
        verbose_name_plural = bi("GOSI reconciliations", "مطابقات التأمينات")


class PayrollAlert(models.Model):
    """In-app alert (R10, R22, R53, R61(b)). Text carries employee codes only, never names or IDs (R36)."""

    class Kind(models.TextChoices):
        MATERNITY_PENDING = "maternity_pending", bi("Maternity case pending at GOSI", "حالة أمومة بانتظار التأمينات")
        MUDAD_DEADLINE = "mudad_deadline", bi("Mudad upload due", "موعد الرفع على مدد")
        GOSI_REGISTRATION = "gosi_registration", bi("GOSI registration due", "موعد التسجيل في التأمينات")
        GOSI_PAYMENT = "gosi_payment", bi("GOSI payment due", "موعد سداد التأمينات")
        RETENTION = "retention", bi("Retention review", "مراجعة الحفظ")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="payroll_alerts",
                                  verbose_name=bi("Recipient", "المستلم"))
    kind = models.CharField(bi("Kind", "النوع"), max_length=20, choices=Kind.choices)
    key = models.CharField(bi("Dedup key", "مفتاح عدم التكرار"), max_length=120)
    text_en = models.TextField(bi("Text (English)", "النص (بالإنجليزية)"))
    text_ar = models.TextField(bi("Text (Arabic)", "النص (بالعربية)"))
    created_at = models.DateTimeField(bi("Created at", "تاريخ الإنشاء"), auto_now_add=True)

    class Meta:
        verbose_name = bi("payroll alert", "تنبيه رواتب")
        verbose_name_plural = bi("payroll alerts", "تنبيهات الرواتب")
        constraints = [models.UniqueConstraint(fields=["recipient", "kind", "key"], name="payroll_alert_once")]
