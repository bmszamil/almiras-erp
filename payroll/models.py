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
    """Review 6: bulk writes skip save() and delete(); they are refused, also through ``_base_manager`` (every
    payroll model sets ``base_manager_name = "objects"``). Django's own save() and delete() do not go through these
    methods."""

    def update(self, **kwargs):
        raise error(*SERVICE_ONLY, code="service_only")

    def delete(self):
        raise error(*SERVICE_ONLY, code="service_only")

    def bulk_create(self, objs, *args, **kwargs):
        raise error(*SERVICE_ONLY, code="service_only")

    def bulk_update(self, objs, fields, *args, **kwargs):
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
    # C-C1: name of the total-deductions column in the salary file ([CC-verify] per bank, OQ-10).
    wps_deductions_field = models.CharField(bi("WPS deductions field name", "اسم حقل الاستقطاعات في ملف الرواتب"),
                                            max_length=30, default="DEDUCTIONS")
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
    # Slice 2 (OQ-7 default): the expense account of an approved loan write-off on exit (data, CFO to confirm);
    # empty = 5100 Salaries and benefits.
    loan_write_off_account = models.ForeignKey("ledger.Account", null=True, blank=True, on_delete=models.PROTECT,
                                               related_name="+", verbose_name=bi("Loan write-off expense account",
                                                                                 "حساب مصروف شطب القروض"))
    # Egypt (research §2.3, §2.5): the governorate of the work location; Universal Health Insurance applies where the
    # governorate is live (parameter uhi.governorates_live). Entity-wide default (one work location per entity).
    work_governorate = models.CharField(bi("Work location governorate", "محافظة مقر العمل"), max_length=40, blank=True)
    updated_by = _user("Updated by", "حدّثه")
    updated_at = models.DateTimeField(bi("Updated at", "تاريخ التحديث"), auto_now=True)

    class Meta:
        base_manager_name = "objects"  # review 6: no unguarded _base_manager bulk writes
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
        # Egypt (Labour Law 14/2025 Art. 113/114, research rows unverified): a maintenance (nafaqa) court order ranks
        # first, at most 50% of the wage.
        NAFAQA = "nafaqa", bi("Maintenance (nafaqa) court order", "حكم نفقة")

    class Art106Case(models.TextChoices):
        INVENTORY_CLOSING = "inventory_closing", bi("Inventory, balance sheets, closing (106(1))",
                                                    "الجرد وإعداد الميزانية وإقفال الحسابات (106(1))")
        OTHER = "other", bi("Another Art. 106 case", "حالة أخرى من المادة 106")

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
    # C-A7: which Art. 106 case; inventory and closing work (106(1)) is at most 30 days a year. Empty = another case.
    art106_case = models.CharField(bi("Art. 106 case", "حالة المادة 106"), max_length=20, blank=True,
                                   choices=Art106Case.choices)
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
    final_settlement = models.ForeignKey("FinalSettlement", null=True, blank=True, on_delete=models.PROTECT,
                                         related_name="+", verbose_name=bi("Taken by final settlement",
                                                                           "أخذته المخالصة النهائية"))

    class Meta:
        base_manager_name = "objects"  # review 6: no unguarded _base_manager bulk writes
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
        base_manager_name = "objects"  # review 6: no unguarded _base_manager bulk writes
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
        base_manager_name = "objects"  # review 6: no unguarded _base_manager bulk writes
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
        base_manager_name = "objects"  # review 6: no unguarded _base_manager bulk writes
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
        # Slice 2: a reversed salary payment reopens the run's net pay.
        ("paid", "approved"),
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
    # Review 6: hash of the stored payslips and lines written by the calculation; approval refuses if they changed.
    stored_hash = models.CharField(bi("Stored payslips hash", "بصمة القسائم المخزنة"), max_length=64, blank=True)
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
    # C-A10 (Labour Law Art. 109): an off-cycle run paying the wage of an annual leave in advance, before it starts.
    purpose = models.CharField(bi("Purpose", "الغرض"), max_length=16, blank=True,
                               choices=[("leave_advance", bi("Annual leave pay in advance (Art. 109)",
                                                             "أجر الإجازة السنوية مقدمًا (المادة 109)"))])
    leave_request = models.ForeignKey("hr.LeaveRequest", null=True, blank=True, on_delete=models.PROTECT,
                                      related_name="+", verbose_name=bi("Annual leave", "الإجازة السنوية"))
    # Slice 3 (R70, OQ-49): ``period_closed`` when an off-cycle run of the out-of-country group or of Egypt is dated
    # the first day of the next open period because its own period is closed.
    entry_date_reason = models.CharField(bi("Entry date reason", "سبب تاريخ القيد"), max_length=30, blank=True,
                                         default="")

    class Meta:
        base_manager_name = "objects"  # review 6: no unguarded _base_manager bulk writes
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
    # Review 8: keyed hash of the IBAN at calculation; a WPS file lists the employee as an exception if it changed.
    iban_hash = models.CharField(bi("IBAN hash at calculation", "بصمة الآيبان عند الاحتساب"), max_length=64, blank=True)
    termination_ref = models.PositiveIntegerField(bi("Approved termination at calculation", "إنهاء الخدمة المعتمد عند الاحتساب"),
                                                  null=True, blank=True)
    # Egypt: the month's taxable salary (after the employee's social insurance share) used by the cumulative salary
    # tax withholding of the year.
    taxable_income = _money("Taxable income of the month", "الدخل الخاضع للضريبة عن الشهر", default=ZERO)
    # Compliance C1 (slice 3 review): the irregular part of it (bonuses, one-off earnings), taxed once, not annualised.
    taxable_irregular = _money("Irregular taxable income of the month", "الدخل غير المنتظم الخاضع للضريبة عن الشهر",
                               default=ZERO)

    class Meta:
        base_manager_name = "objects"  # review 6: no unguarded _base_manager bulk writes
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
        # C-B7: the employee share the month's net pay could not cover is still booked to 2340 in full: Dr 1230
        # (receivable from the employee, new law) and recovered later by a catch-up deduction (Cr 1230).
        GOSI_UNCOLLECTED = "gosi_uncollected", bi("Employee GOSI share advanced by the employer",
                                                  "حصة الموظف في التأمينات المدفوعة عنه مقدمًا")
        LOAN_INSTALLMENT = "loan_installment", bi("Loan installment (interest-free)", "قسط قرض حسن")
        DEFERRED_SALE_INSTALLMENT = "deferred_sale_installment", bi("Bai' ajel installment", "قسط البيع الآجل")
        EMPLOYER_GOSI = "employer_gosi", bi("Employer GOSI", "حصة صاحب العمل في التأمينات")
        EOSB_ACCRUAL = "eosb_accrual", bi("End-of-service provision", "مخصص نهاية الخدمة")
        EOSB_RELEASE = "eosb_release", bi("End-of-service provision released", "تحرير مخصص نهاية الخدمة")
        # Egypt (jurisdiction EG; research rows unverified, OQ-28): social insurance (Law 148/2019), salary tax (Law
        # 91/2005 as amended), Martyrs' Fund (Law 4/2021), Universal Health Insurance (Law 2/2018).
        SI_EMPLOYEE = "si_employee", bi("Social insurance, employee share", "التأمينات الاجتماعية، حصة العامل")
        SI_EMPLOYER = "si_employer", bi("Social insurance, employer share", "التأمينات الاجتماعية، حصة صاحب العمل")
        SALARY_TAX = "salary_tax", bi("Salary tax withheld", "ضريبة كسب العمل المستقطعة")
        MARTYRS_FUND = "martyrs_fund", bi("Martyrs' and Victims' Fund", "صندوق تكريم الشهداء والضحايا")
        UHI_EMPLOYEE = "uhi_employee", bi("Universal health insurance, employee", "التأمين الصحي الشامل، حصة العامل")
        UHI_EMPLOYER = "uhi_employer", bi("Universal health insurance, employer", "التأمين الصحي الشامل، حصة صاحب العمل")

    # Gross = positive earnings - reductions; net = gross - deductions; employer costs are not on the net.
    EARNINGS = frozenset({"earning", "overtime", "one_off_earning", "bonus", "retro", "maternity_top_up",
                          "maternity_support"})
    REDUCTIONS = frozenset({"unpaid_deduction", "sick_deduction", "maternity_suspended"})
    DEDUCTIONS = frozenset({"deduction", "employee_gosi", "gosi_catch_up", "loan_installment",
                            "deferred_sale_installment", "si_employee", "salary_tax", "martyrs_fund", "uhi_employee"})
    EMPLOYER = frozenset({"employer_gosi", "eosb_accrual", "eosb_release", "gosi_uncollected", "si_employer",
                          "uhi_employer"})

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
        base_manager_name = "objects"  # review 6: no unguarded _base_manager bulk writes
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
        base_manager_name = "objects"  # review 6: no unguarded _base_manager bulk writes
        verbose_name = bi("EOSB provision entry", "قيد مخصص نهاية الخدمة")
        verbose_name_plural = bi("EOSB provision entries", "قيود مخصص نهاية الخدمة")
        ordering = ["employee", "date", "id"]


class GosiCatchUpNotice(ServiceWritten):
    """R58 (P19(j)): the employee is told that a missed GOSI share of an earlier month is deducted later."""

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    employee = models.ForeignKey("hr.Employee", on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("Employee", "الموظف"))
    # Re-review 5: a final settlement recovering an advanced share has its notice too (then ``run`` is empty).
    run = models.ForeignKey(PayrollRun, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                            verbose_name=bi("Run", "المسير"))
    final_settlement = models.ForeignKey("FinalSettlement", null=True, blank=True, on_delete=models.PROTECT,
                                         related_name="+", verbose_name=bi("Final settlement", "المخالصة النهائية"))
    period_of_origin = models.CharField(bi("Period of origin", "فترة المنشأ"), max_length=7)
    amount = _money("Amount", "المبلغ")
    text_en = models.TextField(bi("Text (English)", "النص (بالإنجليزية)"))
    text_ar = models.TextField(bi("Text (Arabic)", "النص (بالعربية)"))
    notified_on = models.DateField(bi("Notified on", "تاريخ الإبلاغ"), null=True, blank=True)
    channel = models.CharField(bi("Channel", "وسيلة الإبلاغ"), max_length=60, blank=True)
    recorded_by = _user("Recorded by", "سجّله")
    created_at = models.DateTimeField(bi("Created at", "تاريخ الإنشاء"), auto_now_add=True)

    class Meta:
        base_manager_name = "objects"  # review 6: no unguarded _base_manager bulk writes
        verbose_name = bi("GOSI catch-up notice", "إشعار استدراك التأمينات")
        verbose_name_plural = bi("GOSI catch-up notices", "إشعارات استدراك التأمينات")
        constraints = [models.CheckConstraint(condition=Q(run__isnull=False) | Q(final_settlement__isnull=False),
                                              name="payroll_notice_has_document")]


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
        WRITTEN_OFF = "written_off", bi("Written off on exit", "مشطوب عند انتهاء الخدمة")

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
    status = models.CharField(bi("Status", "الحالة"), max_length=12, choices=Status.choices, default=Status.DRAFT)
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
        base_manager_name = "objects"  # review 6: no unguarded _base_manager bulk writes
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
        # Re-review 8: what the final dues did not cover stays on 1230 after exit (write-off: OQ-7).
        OUTSTANDING_AFTER_EXIT = "outstanding_after_exit", bi("Outstanding after exit", "قائم بعد انتهاء الخدمة")
        # Slice 2 (OQ-7 default): the outstanding-after-exit part written off by an approved LoanWriteOff.
        WRITTEN_OFF = "written_off", bi("Written off", "مشطوب")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    loan = models.ForeignKey(EmployeeLoan, on_delete=models.PROTECT, related_name="installments",
                             verbose_name=bi("Loan", "القرض"))
    schedule_version = models.PositiveIntegerField(bi("Schedule version", "نسخة الجدول"))
    period = models.CharField(bi("Period", "الفترة"), max_length=7)
    amount = _money("Amount", "المبلغ")
    deducted_amount = _money("Deducted", "المستقطع", default=ZERO)
    status = models.CharField(bi("Status", "الحالة"), max_length=24, choices=Status.choices, default=Status.PLANNED)
    run = models.ForeignKey(PayrollRun, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                            verbose_name=bi("Run", "المسير"))
    carried_from = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                                     verbose_name=bi("Remainder of", "باقي القسط"))

    class Meta:
        base_manager_name = "objects"  # review 6: no unguarded _base_manager bulk writes
        verbose_name = bi("loan installment", "قسط قرض")
        verbose_name_plural = bi("loan installments", "أقساط القروض")
        ordering = ["loan", "period", "id"]
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name="payroll_installment_positive")]


# --- bai' ajel (3.11; sale through invoicing R112, not built) ----------------------------------------

class AssetType(models.TextChoices):
    """Compliance A3: the kind of asset a bai' ajel contract sells (recorded with the acquisition)."""
    VEHICLE = "vehicle", bi("Vehicle", "مركبة")
    EQUIPMENT = "equipment", bi("Equipment or appliance", "معدات أو أجهزة")
    ELECTRONICS = "electronics", bi("Electronics", "إلكترونيات")
    FURNITURE = "furniture", bi("Furniture", "أثاث")
    OTHER_MOVABLE = "other_movable", bi("Other movable goods", "منقولات أخرى")
    REAL_ESTATE = "real_estate", bi("Real estate (refused: RETT)", "عقار (مرفوض: ضريبة التصرفات العقارية)")


class DeferredSaleContract(Tracked, ServiceWritten):
    """Bai' ajel employee financing (R47 to R49). The sale itself is an invoicing document (invoicing R112, E28),
    reached through ``payroll.invoicing_port``. No interest, penalty, late-fee or rate field exists."""

    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        TERMS_SUBMITTED = "terms_submitted", bi("Terms submitted", "الشروط مقدمة")
        TERMS_APPROVED = "terms_approved", bi("Terms approved", "الشروط معتمدة")
        ASSET_ACQUIRED = "asset_acquired", bi("Asset bought and possessed", "الأصل مشترى ومقبوض")
        # Slice 2 (R47 as amended, invoicing R112): the sale is approved and its invoice issued; it posts when ZATCA
        # reports it, and only then is the contract sold and its installments deducted.
        SALE_INVOICED = "sale_invoiced", bi("Sale approved, invoice awaiting ZATCA reporting",
                                            "البيع معتمد والفاتورة بانتظار الإبلاغ للهيئة")
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
    # Review 11 (R47 step 3, 3.11): the company buys the asset in its own name (a posted bill debiting 1250 at the
    # cost) and takes possession before the sale; the sale is refused otherwise.
    purchase_bill = models.ForeignKey("invoicing.PurchaseDocument", null=True, blank=True, on_delete=models.PROTECT,
                                      related_name="+", verbose_name=bi("Purchase bill", "فاتورة الشراء"))
    # Compliance A3 (2026-10-20): what is sold, recorded with the acquisition; real estate is refused (its sale is
    # VAT-exempt and subject to RETT, invoicing R125).
    asset_type = models.CharField(bi("Asset type", "نوع الأصل"), max_length=20, choices=AssetType.choices, blank=True)
    possession_date = models.DateField(bi("Possession date", "تاريخ القبض"), null=True, blank=True)
    possession_evidence_ref = models.CharField(bi("Possession evidence", "مستند القبض"), max_length=200, blank=True)
    sale_date = models.DateField(bi("Sale date", "تاريخ البيع"), null=True, blank=True)
    status = models.CharField(bi("Status", "الحالة"), max_length=16, choices=Status.choices, default=Status.DRAFT)
    submitted_by = _user("Submitted by", "قدّمه")
    terms_approved_by = _user("Terms approved by", "اعتمد الشروط")
    terms_approved_at = models.DateTimeField(bi("Terms approved at", "تاريخ اعتماد الشروط"), null=True, blank=True)

    class Meta:
        base_manager_name = "objects"  # review 6: no unguarded _base_manager bulk writes
        verbose_name = bi("bai' ajel contract", "عقد بيع آجل")
        verbose_name_plural = bi("bai' ajel contracts", "عقود البيع الآجل")
        # Re-review 7: one purchase bill backs one contract (one asset is sold once).
        constraints = [models.UniqueConstraint(fields=["purchase_bill"], condition=Q(purchase_bill__isnull=False),
                                               name="payroll_sale_one_purchase_bill")]

    @property
    def sale_price(self):
        return self.cost + self.profit_amount

    @property
    def receivable(self):
        """P20(h): VAT on the sale price is charged to and paid by the employee."""
        return self.sale_price + self.vat_amount


class DeferredSaleRebate(Tracked, ServiceWritten):
    """[Reg] Compliance A8 (invoicing R128): an early-settlement rebate (ibra') of part of a sold contract's profit not
    yet recognised, prepared by the payroll officer and approved by the sale approver (``payroll.approve_deferred_sale``,
    A, P; never the preparer or the employee). Approval asks invoicing for the simplified credit note through the
    port and reduces the planned instalments by its gross (rebate + VAT), from the last one back. The price never
    increases; whether a rebate is granted is for the Sharia board (OQ-33)."""

    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        APPROVED = "approved", bi("Approved (credit note issued)", "معتمد (صدر الإشعار الدائن)")
        # Review 2026-10-20b (finding 2): ZATCA rejected the credit note; the instalments were restored. Approved
        # again (a new credit note, four eyes again) or discarded.
        REJECTED = "rejected", bi("Credit note rejected: approve again or discard",
                                  "رُفض الإشعار الدائن: يُعتمد مجددًا أو يُلغى")
        DISCARDED = "discarded", bi("Discarded", "ملغى")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    contract = models.ForeignKey(DeferredSaleContract, on_delete=models.PROTECT, related_name="rebates",
                                 verbose_name=bi("Contract", "العقد"))
    amount = _money("Profit waived (net of VAT)", "الربح المُبرأ منه (قبل الضريبة)")
    vat_amount = _money("VAT credited", "الضريبة المخفضة", default=ZERO)
    reason_ar = models.TextField(bi("Reason (Arabic)", "السبب (بالعربية)"))
    reason_en = models.TextField(bi("Reason (English)", "السبب (بالإنجليزية)"), blank=True)
    request_ref = models.CharField(bi("Employee's written request", "طلب الموظف المكتوب"), max_length=200)
    credit_note_ref = models.CharField(bi("Credit note (invoicing)", "الإشعار الدائن"), max_length=60, blank=True)
    # Review 2026-10-20b (finding 2): the credit note's state as last read through the port (issued, final,
    # rejected) and the planned instalments the approval reduced ([{"period", "amount"}]), restored on a rejection.
    credit_note_status = models.CharField(bi("Credit note status", "حالة الإشعار الدائن"), max_length=10, blank=True)
    installment_cuts = models.JSONField(bi("Instalments reduced", "الأقساط المخفضة"), default=list, blank=True)
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.DRAFT)
    # Review 2026-10-20c (finding 4): written by the approval on the locked row, checked by invoicing's call through
    # ``invoicing_port.rebate_problems`` and cleared when the approval is done (empty otherwise).
    approval_token = models.CharField(bi("Approval token (one-time)", "رمز الاعتماد (لمرة واحدة)"), max_length=64,
                                      blank=True, editable=False)
    approved_by = _user("Approved by", "اعتمده")
    approved_at = models.DateTimeField(bi("Approved at", "تاريخ الاعتماد"), null=True, blank=True)

    class Meta:
        base_manager_name = "objects"
        verbose_name = bi("bai' ajel rebate (ibra')", "إبراء بيع آجل")
        verbose_name_plural = bi("bai' ajel rebates (ibra')", "إبراءات البيع الآجل")
        constraints = [models.UniqueConstraint(fields=["contract"], condition=Q(status__in=("draft", "rejected")),
                                               name="payroll_one_open_rebate")]

    @property
    def gross_amount(self):
        return self.amount + self.vat_amount


class DeferredSaleInstallment(ServiceWritten):
    class Status(models.TextChoices):
        PLANNED = "planned", bi("Planned", "مخطط")
        DEDUCTED = "deducted", bi("Deducted", "مستقطع")
        SETTLED_IN_FINAL = "settled_in_final", bi("Settled in the final settlement", "سُدد في المخالصة النهائية")
        OUTSTANDING_AFTER_EXIT = "outstanding_after_exit", bi("Outstanding after exit", "قائم بعد انتهاء الخدمة")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    contract = models.ForeignKey(DeferredSaleContract, on_delete=models.PROTECT, related_name="installments",
                                 verbose_name=bi("Contract", "العقد"))
    period = models.CharField(bi("Period", "الفترة"), max_length=7)
    amount = _money("Amount", "المبلغ")
    deducted_amount = _money("Deducted", "المستقطع", default=ZERO)
    status = models.CharField(bi("Status", "الحالة"), max_length=24, choices=Status.choices, default=Status.PLANNED)
    run = models.ForeignKey(PayrollRun, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                            verbose_name=bi("Run", "المسير"))
    carried_from = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                                     verbose_name=bi("Remainder of", "باقي القسط"))

    class Meta:
        base_manager_name = "objects"  # review 6: no unguarded _base_manager bulk writes
        verbose_name = bi("bai' ajel installment", "قسط بيع آجل")
        verbose_name_plural = bi("bai' ajel installments", "أقساط البيع الآجل")
        ordering = ["contract", "period", "id"]


# --- final settlement (3.8; R28, R34, R49; E5; compliance C-A11, review 14) ---------------------------------

class FinalSettlement(Tracked, ServiceWritten):
    """[Reg] The leaver's final settlement (R34, E5): salary to the last working day net of what the runs paid, leave
    encashment, the EOSB award (hr snapshot), notice pay, a separate settlement (mutual agreement), GOSI of the leaving
    month on days of service (Regs Art. 20(2)), loan and bai' ajel recovery from the final dues without the monthly
    caps (P17), due within 7 days, or 14 when the worker ended the contract (Labour Law Art. 88). Drafted by hr's
    termination approval (hr R38) through ``payroll.hr_port``."""

    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        CALCULATED = "calculated", bi("Calculated", "محتسبة")
        SUBMITTED = "submitted", bi("Submitted", "مقدّمة")
        RETURNED = "returned", bi("Returned", "معادة")
        APPROVED = "approved", bi("Approved and posted", "معتمدة ومرحّلة")
        PAID = "paid", bi("Paid", "مدفوعة")
        DISCARDED = "discarded", bi("Discarded (termination withdrawn)", "ملغاة (سُحب إنهاء الخدمة)")

    TRANSITIONS = frozenset({
        ("draft", "calculated"), ("calculated", "calculated"), ("calculated", "submitted"), ("submitted", "returned"),
        ("returned", "calculated"), ("submitted", "approved"), ("approved", "paid"), ("draft", "draft"),
        ("calculated", "draft"), ("returned", "draft"), ("draft", "discarded"), ("calculated", "discarded"),
        ("returned", "discarded"), ("submitted", "discarded"),
        # Slice 2: a reversed payment reopens the settlement's net.
        ("paid", "approved"),
    })

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    employee = models.ForeignKey("hr.Employee", on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("Employee", "الموظف"))
    termination = models.OneToOneField("hr.Termination", on_delete=models.PROTECT, related_name="payroll_settlement",
                                       verbose_name=bi("Termination", "إنهاء الخدمة"))
    settlement_input = models.ForeignKey("hr.FinalSettlementInput", on_delete=models.PROTECT, related_name="+",
                                         verbose_name=bi("HR settlement input", "مدخلات المخالصة من الموارد البشرية"))
    eosb_calculation = models.ForeignKey("hr.EosbCalculation", null=True, blank=True, on_delete=models.PROTECT,
                                         related_name="+", verbose_name=bi("EOSB calculation", "حساب المكافأة"))
    number = models.CharField(bi("Number", "الرقم"), max_length=40, blank=True)
    kind = models.CharField(bi("Termination kind", "نوع الإنهاء"), max_length=30)
    last_working_day = models.DateField(bi("Last working day", "آخر يوم عمل"))
    period = models.CharField(bi("Leaving month", "شهر الترك"), max_length=7)
    worker_initiated = models.BooleanField(bi("Ended by the worker", "أنهاه العامل"), default=False)
    due_date = models.DateField(bi("Due by (Art. 88)", "موعد السداد (المادة 88)"), null=True, blank=True)
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.DRAFT)
    gross = _money("Gross", "الإجمالي", default=ZERO)
    total_deductions = _money("Deductions", "الاستقطاعات", default=ZERO)
    net = _money("Net", "الصافي", default=ZERO)
    eosb_award = _money("EOSB award", "مكافأة نهاية الخدمة", default=ZERO)
    eosb_provision_balance = _money("EOSB provision balance used", "رصيد مخصص المكافأة المستخدم", default=ZERO)
    separate_settlement_amount = _money("Separate settlement (mutual agreement)", "تسوية منفصلة (اتفاق الطرفين)",
                                        default=ZERO)
    written_consent_ref = models.CharField(bi("Written consent to the separate settlement", "الموافقة الكتابية على التسوية"),
                                           max_length=200, blank=True)
    input_ids = models.JSONField(bi("Inputs taken", "المدخلات المأخوذة"), default=list, blank=True)
    warnings = models.JSONField(bi("Warnings", "تنبيهات"), default=list, blank=True)
    notes = models.JSONField(bi("Notes", "ملاحظات"), default=list, blank=True)
    calc_version = models.PositiveIntegerField(bi("Calculation version", "نسخة الاحتساب"), default=0)
    inputs_hash = models.CharField(bi("Calculation fingerprint", "بصمة الاحتساب"), max_length=64, blank=True)
    stored_hash = models.CharField(bi("Stored lines hash", "بصمة البنود المخزنة"), max_length=64, blank=True)
    calculated_by = _user("Calculated by", "احتسبها")
    calculated_at = models.DateTimeField(bi("Calculated at", "تاريخ الاحتساب"), null=True, blank=True)
    submitted_by = _user("Submitted by", "قدّمها")
    submitted_at = models.DateTimeField(bi("Submitted at", "تاريخ التقديم"), null=True, blank=True)
    approved_by = _user("Approved by", "اعتمدها")
    approved_at = models.DateTimeField(bi("Approved at", "تاريخ الاعتماد"), null=True, blank=True)
    returned_reason_ar = models.TextField(bi("Return reason (Arabic)", "سبب الإعادة (بالعربية)"), blank=True)
    journal_entry = models.OneToOneField("ledger.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                         related_name="+", verbose_name=bi("Journal entry", "القيد"))

    class Meta:
        base_manager_name = "objects"  # review 6: no unguarded _base_manager bulk writes
        verbose_name = bi("final settlement", "مخالصة نهائية")
        verbose_name_plural = bi("final settlements", "المخالصات النهائية")
        constraints = [models.UniqueConstraint(fields=["entity", "number"], condition=~Q(number=""),
                                               name="payroll_settlement_number")]

    def save(self, *args, **kwargs):
        if self.pk:
            stored = type(self)._base_manager.filter(pk=self.pk).values_list("status", flat=True).first()
            if stored is not None and stored != self.status and (stored, self.status) not in self.TRANSITIONS:
                raise error(f"A final settlement does not move from {stored} to {self.status}.",
                            f"لا تنتقل المخالصة النهائية من {stored} إلى {self.status}.", code="settlement_status")
        super().save(*args, **kwargs)

    def __str__(self):
        return self.number or f"{self.employee.employee_code} final settlement ({self.status})"


class FinalSettlementLine(ServiceWritten):
    class LineType(models.TextChoices):
        SALARY_TO_LAST_DAY = "salary_to_last_day", bi("Salary to the last working day", "الأجر حتى آخر يوم عمل")
        RETRO = "retro", bi("Earlier months (retro)", "أشهر سابقة (بأثر رجعي)")
        SALARY_REDUCTION = "salary_reduction", bi("Unpaid, sick or maternity reduction", "تخفيض بدون أجر أو مرضي أو أمومة")
        LEAVE_ENCASHMENT = "leave_encashment", bi("Leave encashment (Art. 111)", "بدل الإجازة (المادة 111)")
        EOSB = "eosb", bi("End-of-service award", "مكافأة نهاية الخدمة")
        NOTICE_PAY = "notice_pay", bi("Notice pay (Art. 76)", "بدل الإشعار (المادة 76)")
        SEPARATE_SETTLEMENT = "separate_settlement", bi("Separate settlement (mutual agreement)", "تسوية منفصلة (اتفاق الطرفين)")
        OTHER_EARNING = "other_earning", bi("Other earning", "استحقاق آخر")
        LOAN_RECOVERY = "loan_recovery", bi("Interest-free loan recovered", "استرداد القرض الحسن")
        DEFERRED_SALE_RECOVERY = "deferred_sale_recovery", bi("Bai' ajel balance recovered", "استرداد رصيد البيع الآجل")
        GOSI_RECOVERY = "gosi_recovery", bi("GOSI share recovered", "استرداد حصة التأمينات")
        EMPLOYEE_GOSI = "employee_gosi", bi("Employee GOSI", "حصة الموظف في التأمينات")
        OTHER_DEDUCTION = "other_deduction", bi("Other deduction", "استقطاع آخر")
        EMPLOYER_GOSI = "employer_gosi", bi("Employer GOSI", "حصة صاحب العمل في التأمينات")
        GOSI_UNCOLLECTED = "gosi_uncollected", bi("Employee GOSI share advanced by the employer",
                                                  "حصة الموظف في التأمينات المدفوعة عنه مقدمًا")

    EARNINGS = frozenset({"salary_to_last_day", "retro", "leave_encashment", "eosb", "notice_pay",
                          "separate_settlement", "other_earning"})
    REDUCTIONS = frozenset({"salary_reduction"})
    DEDUCTIONS = frozenset({"loan_recovery", "deferred_sale_recovery", "gosi_recovery", "employee_gosi",
                            "other_deduction"})
    EMPLOYER = frozenset({"employer_gosi", "gosi_uncollected"})

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    settlement = models.ForeignKey(FinalSettlement, on_delete=models.PROTECT, related_name="lines",
                                   verbose_name=bi("Final settlement", "المخالصة النهائية"))
    line_type = models.CharField(bi("Line type", "نوع البند"), max_length=24, choices=LineType.choices)
    component = models.ForeignKey("hr.SalaryComponent", null=True, blank=True, on_delete=models.PROTECT,
                                  related_name="+", verbose_name=bi("Component", "المكوّن"))
    gosi_branch = models.CharField(bi("GOSI branch", "فرع التأمينات"), max_length=24, blank=True)
    quantity = models.DecimalField(bi("Quantity", "الكمية"), max_digits=10, decimal_places=2, null=True, blank=True)
    rate = models.DecimalField(bi("Rate", "المعدل"), max_digits=12, decimal_places=4, null=True, blank=True)
    amount = _money("Amount", "المبلغ")
    account = models.ForeignKey("ledger.Account", on_delete=models.PROTECT, related_name="+",
                                verbose_name=bi("Account", "الحساب"))
    description_en = models.CharField(bi("Description (English)", "الوصف (بالإنجليزية)"), max_length=300)
    description_ar = models.CharField(bi("Description (Arabic)", "الوصف (بالعربية)"), max_length=300)
    source_ref = models.CharField(bi("Source", "المصدر"), max_length=80, blank=True)
    origin_period = models.CharField(bi("Period of origin", "فترة المنشأ"), max_length=7, blank=True)
    # Re-review 1: the payslip line type the line was computed as (blank for settlement-only lines such as leave
    # encashment, EOSB or recoveries), so later calculations net a period against what the settlement booked for it.
    slip_line_type = models.CharField(bi("Computed as", "احتُسب بوصفه"), max_length=28, blank=True)

    class Meta:
        base_manager_name = "objects"  # review 6: no unguarded _base_manager bulk writes
        verbose_name = bi("final settlement line", "بند المخالصة النهائية")
        verbose_name_plural = bi("final settlement lines", "بنود المخالصات النهائية")
        ordering = ["settlement", "id"]
        constraints = [models.CheckConstraint(condition=Q(amount__gte=0), name="payroll_settlement_line_positive")]


# --- payments (3.7) --------------------------------------------------------------------------------

class PayrollPayment(Tracked, ServiceWritten):
    """[Reg] Net salaries (E3, E8b) or GOSI (E2) paid from the bank, approved (= posted) by the approver (R29)."""

    class Kind(models.TextChoices):
        NET_SALARIES = "net_salaries", bi("Net salaries", "صافي الرواتب")
        GOSI = "gosi", bi("GOSI contributions", "اشتراكات التأمينات")
        FINAL_SETTLEMENT = "final_settlement", bi("Final settlement", "المخالصة النهائية")

    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        SUBMITTED = "submitted", bi("Submitted", "مقدّم")
        APPROVED = "approved", bi("Approved and posted", "معتمد ومرحّل")
        REVERSED = "reversed", bi("Reversed", "معكوس")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    kind = models.CharField(bi("Kind", "النوع"), max_length=20, choices=Kind.choices)
    run = models.ForeignKey(PayrollRun, null=True, blank=True, on_delete=models.PROTECT, related_name="payments",
                            verbose_name=bi("Run", "المسير"))
    gosi_period = models.CharField(bi("GOSI period", "فترة التأمينات"), max_length=7, blank=True)
    final_settlement = models.ForeignKey("FinalSettlement", null=True, blank=True, on_delete=models.PROTECT,
                                         related_name="payments", verbose_name=bi("Final settlement",
                                                                                  "المخالصة النهائية"))
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
    # Slice 2 (R43, OQ-30): the payment amount in SAR at the payment-date rate, the base of the WHT return row.
    wht_base_sar = _money("WHT base (SAR)", "وعاء ضريبة الاستقطاع (ريال)", default=ZERO)
    # Compliance B2, B3: per payee at approval: the gross remuneration this payment covers (the WHT base) and its WHT,
    # non-residents only (a person resident in KSA bears no WHT); the WHT return rows are these parts.
    wht_parts = models.JSONField(bi("WHT per payee", "ضريبة الاستقطاع لكل مستفيد"), default=list, blank=True)
    # Review 2026-10-20b (finding 3): the payslips a run payment pays (named at recording; empty = every payslip of
    # the run in the currency) and, at approval, what it paid on each ([{"payslip_id", "amount"}]). The WHT base of a
    # remuneration payee is their gross in the share of their own net this payment paid.
    payslip_ids = models.JSONField(bi("Payslips paid", "القسائم المدفوعة"), default=list, blank=True)
    payslip_parts = models.JSONField(bi("Paid per payslip", "المدفوع لكل قسيمة"), default=list, blank=True)
    bank_reference = models.CharField(bi("Bank reference", "المرجع البنكي"), max_length=60, blank=True)
    # C-B7: e.g. a GOSI payment above the ERP contributions, up to the recorded GOSI invoice (difference flagged).
    warnings = models.JSONField(bi("Warnings", "تنبيهات"), default=list, blank=True)
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.DRAFT)
    submitted_by = _user("Submitted by", "قدّمه")
    submitted_at = models.DateTimeField(bi("Submitted at", "تاريخ التقديم"), null=True, blank=True)
    approved_by = _user("Approved by", "اعتمده")
    approved_at = models.DateTimeField(bi("Approved at", "تاريخ الاعتماد"), null=True, blank=True)
    journal_entry = models.OneToOneField("ledger.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                         related_name="+", verbose_name=bi("Journal entry", "القيد"))
    # Slice 2: a payment reversed through ledger.reverse_entry (never edited or deleted).
    reversal_entry = models.OneToOneField("ledger.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                          related_name="+", verbose_name=bi("Reversal entry", "قيد العكس"))
    reversed_by = _user("Reversed by", "عكسه")
    reversed_at = models.DateTimeField(bi("Reversed at", "تاريخ العكس"), null=True, blank=True)
    reversal_reason = models.TextField(bi("Reversal reason", "سبب العكس"), blank=True)

    class Meta:
        base_manager_name = "objects"  # review 6: no unguarded _base_manager bulk writes
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
    # Set by the service from its clock (the 10-year burn nomination counts from it; tests move the clock).
    generated_at = models.DateTimeField(bi("Generated at", "تاريخ الإنشاء"))
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
        base_manager_name = "objects"  # review 6: no unguarded _base_manager bulk writes
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
    # Review 15: the hash of the bank-processed file uploaded to Mudad, as recorded.
    bank_processed_sha256 = models.CharField(bi("Bank-processed file SHA-256", "بصمة ملف البنك المعالج"), max_length=64,
                                             blank=True)
    recorded_by = _user("Recorded by", "سجّله", null=False)
    created_at = models.DateTimeField(bi("Created at", "تاريخ الإنشاء"), auto_now_add=True)

    class Meta:
        base_manager_name = "objects"  # review 6: no unguarded _base_manager bulk writes
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
        base_manager_name = "objects"  # review 6: no unguarded _base_manager bulk writes
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
        LEAVE_ADVANCE = "leave_advance", bi("Annual leave pay due in advance", "أجر إجازة سنوية مستحق مقدمًا")
        FINAL_SETTLEMENT_DUE = "final_settlement_due", bi("Final settlement due (Art. 88)",
                                                          "موعد المخالصة النهائية (المادة 88)")
        MATERNITY_DECIDED = "maternity_decided", bi("Maternity case decided: carried days to settle",
                                                    "قرار حالة الأمومة: أيام مرحّلة للتسوية")
        BAI_AJEL_REBATE = "bai_ajel_rebate", bi("Bai' ajel rebate credit note rejected",
                                                "رُفض الإشعار الدائن لإبراء بيع آجل")
        MUDAD_JUSTIFICATION = "mudad_justification", bi("Mudad justification due", "موعد مبرر مدد")

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


# =====================================================================================================
# Slice 2 (docs/specs/payroll.md §10.8)
# =====================================================================================================

class SettlementCorrection(Tracked, ServiceWritten):
    """[Reg] A correction of an approved (or paid) final settlement: difference lines only, approved by a second
    person, posted as its own entry through the ledger registry. The settlement, its lines and its entry are never
    edited (CLAUDE.md rule 1); the correction adds to (or takes from) what the settlement booked."""

    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        SUBMITTED = "submitted", bi("Submitted", "مقدّم")
        RETURNED = "returned", bi("Returned", "معاد")
        APPROVED = "approved", bi("Approved and posted", "معتمد ومرحّل")
        DISCARDED = "discarded", bi("Discarded", "ملغى")

    TRANSITIONS = frozenset({("draft", "submitted"), ("submitted", "returned"), ("returned", "submitted"),
                             ("submitted", "approved"), ("draft", "discarded"), ("returned", "discarded"),
                             ("returned", "draft")})

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    settlement = models.ForeignKey("FinalSettlement", on_delete=models.PROTECT, related_name="corrections",
                                   verbose_name=bi("Final settlement", "المخالصة النهائية"))
    employee = models.ForeignKey("hr.Employee", on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("Employee", "الموظف"))
    number = models.CharField(bi("Number", "الرقم"), max_length=40, blank=True)
    reason_en = models.TextField(bi("Reason (English)", "السبب (بالإنجليزية)"), blank=True)
    reason_ar = models.TextField(bi("Reason (Arabic)", "السبب (بالعربية)"))
    written_consent_ref = models.CharField(bi("Written consent (separate settlement)", "الموافقة الكتابية (تسوية منفصلة)"),
                                           max_length=200, blank=True)
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.DRAFT)
    net_change = _money("Change of the net due to the employee", "التغير في الصافي المستحق للموظف", default=ZERO)
    warnings = models.JSONField(bi("Warnings", "تنبيهات"), default=list, blank=True)
    stored_hash = models.CharField(bi("Stored lines hash", "بصمة البنود المخزنة"), max_length=64, blank=True)
    submitted_by = _user("Submitted by", "قدّمه")
    submitted_at = models.DateTimeField(bi("Submitted at", "تاريخ التقديم"), null=True, blank=True)
    approved_by = _user("Approved by", "اعتمده")
    approved_at = models.DateTimeField(bi("Approved at", "تاريخ الاعتماد"), null=True, blank=True)
    returned_reason_ar = models.TextField(bi("Return reason (Arabic)", "سبب الإعادة (بالعربية)"), blank=True)
    journal_entry = models.OneToOneField("ledger.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                         related_name="+", verbose_name=bi("Journal entry", "القيد"))
    # Compliance C1: an increase of what is due to the leaver is due at once (the settlement's Art. 88 period has
    # run): the approval day, with an alert to the payroll officers and approvers.
    due_date = models.DateField(bi("Increase due by", "تاريخ استحقاق الزيادة"), null=True, blank=True)

    class Meta:
        base_manager_name = "objects"
        verbose_name = bi("final settlement correction", "تصحيح مخالصة نهائية")
        verbose_name_plural = bi("final settlement corrections", "تصحيحات المخالصات النهائية")
        constraints = [models.UniqueConstraint(fields=["entity", "number"], condition=~Q(number=""),
                                               name="payroll_correction_number"),
                       # Review fix 6: one open correction per settlement, also under concurrency.
                       models.UniqueConstraint(fields=["settlement"],
                                               condition=Q(status__in=("draft", "submitted", "returned")),
                                               name="payroll_one_open_correction")]

    def save(self, *args, **kwargs):
        if self.pk:
            stored = type(self)._base_manager.filter(pk=self.pk).values_list("status", flat=True).first()
            if stored is not None and stored != self.status and (stored, self.status) not in self.TRANSITIONS:
                raise error(f"A settlement correction does not move from {stored} to {self.status}.",
                            f"لا ينتقل تصحيح المخالصة من {stored} إلى {self.status}.", code="correction_status")
        super().save(*args, **kwargs)

    def __str__(self):
        return self.number or f"{self.settlement} correction ({self.status})"


class SettlementCorrectionLine(ServiceWritten):
    class Direction(models.TextChoices):
        INCREASE = "increase", bi("Increase", "زيادة")
        DECREASE = "decrease", bi("Decrease", "نقص")

    # Line types that may be corrected (FinalSettlementLine types); recoveries of loans, bai' ajel and GOSI shares
    # and their installments are not corrected here (their sub-ledgers moved at approval).
    EARNINGS = frozenset({"salary_to_last_day", "retro", "leave_encashment", "eosb", "notice_pay",
                          "separate_settlement", "other_earning"})
    DEDUCTIONS = frozenset({"other_deduction", "employee_gosi"})
    EMPLOYER = frozenset({"employer_gosi"})
    ALLOWED = EARNINGS | DEDUCTIONS | EMPLOYER

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    correction = models.ForeignKey(SettlementCorrection, on_delete=models.PROTECT, related_name="lines",
                                   verbose_name=bi("Correction", "التصحيح"))
    line_type = models.CharField(bi("Line type", "نوع البند"), max_length=24,
                                 choices=[(c, c) for c in sorted(ALLOWED)])
    direction = models.CharField(bi("Direction", "الاتجاه"), max_length=8, choices=Direction.choices)
    amount = _money("Difference", "الفرق")
    account = models.ForeignKey("ledger.Account", on_delete=models.PROTECT, related_name="+",
                                verbose_name=bi("Account", "الحساب"))
    component = models.ForeignKey("hr.SalaryComponent", null=True, blank=True, on_delete=models.PROTECT,
                                  related_name="+", verbose_name=bi("Component", "المكوّن"))
    gosi_branch = models.CharField(bi("GOSI branch", "فرع التأمينات"), max_length=24, blank=True)
    description_en = models.CharField(bi("Description (English)", "الوصف (بالإنجليزية)"), max_length=300)
    description_ar = models.CharField(bi("Description (Arabic)", "الوصف (بالعربية)"), max_length=300)

    class Meta:
        base_manager_name = "objects"
        verbose_name = bi("settlement correction line", "بند تصحيح المخالصة")
        verbose_name_plural = bi("settlement correction lines", "بنود تصحيح المخالصات")
        ordering = ["correction", "id"]
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name="payroll_correction_line_positive")]

    @property
    def sign(self):
        return 1 if self.direction == self.Direction.INCREASE else -1


class LoanWriteOff(Tracked, ServiceWritten):
    """[Reg] OQ-7 default (slice 2): the part of an interest-free loan the final dues did not cover
    (``outstanding_after_exit``) written off: Dr expense / Cr 1230, approved under the entity's matrix (Managing
    Partner in Almiras, the CEO elsewhere; payroll.approvals), logged. No interest, fee or charge ever (R26)."""

    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        SUBMITTED = "submitted", bi("Submitted", "مقدّم")
        APPROVED = "approved", bi("Approved and posted", "معتمد ومرحّل")
        REJECTED = "rejected", bi("Rejected", "مرفوض")

    TRANSITIONS = frozenset({("draft", "submitted"), ("submitted", "approved"), ("submitted", "rejected"),
                             ("draft", "rejected")})

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    loan = models.ForeignKey(EmployeeLoan, on_delete=models.PROTECT, related_name="write_offs",
                             verbose_name=bi("Loan", "القرض"))
    employee = models.ForeignKey("hr.Employee", on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("Employee", "الموظف"))
    number = models.CharField(bi("Number", "الرقم"), max_length=40, blank=True)
    amount = _money("Amount written off", "المبلغ المشطوب")
    expense_account = models.ForeignKey("ledger.Account", on_delete=models.PROTECT, related_name="+",
                                        verbose_name=bi("Expense account", "حساب المصروف"))
    reason_en = models.TextField(bi("Reason (English)", "السبب (بالإنجليزية)"), blank=True)
    reason_ar = models.TextField(bi("Reason (Arabic)", "السبب (بالعربية)"))
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.DRAFT)
    submitted_by = _user("Submitted by", "قدّمه")
    submitted_at = models.DateTimeField(bi("Submitted at", "تاريخ التقديم"), null=True, blank=True)
    approved_by = _user("Approved by", "اعتمده")
    approved_at = models.DateTimeField(bi("Approved at", "تاريخ الاعتماد"), null=True, blank=True)
    rejected_reason_ar = models.TextField(bi("Rejection reason (Arabic)", "سبب الرفض (بالعربية)"), blank=True)
    journal_entry = models.OneToOneField("ledger.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                         related_name="+", verbose_name=bi("Journal entry", "القيد"))

    class Meta:
        base_manager_name = "objects"
        verbose_name = bi("loan write-off", "شطب قرض")
        verbose_name_plural = bi("loan write-offs", "شطب القروض")
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name="payroll_write_off_positive"),
                       models.UniqueConstraint(fields=["loan"], condition=Q(status__in=("draft", "submitted",
                                                                                        "approved")),
                                               name="payroll_one_open_write_off")]

    def save(self, *args, **kwargs):
        if self.pk:
            stored = type(self)._base_manager.filter(pk=self.pk).values_list("status", flat=True).first()
            if stored is not None and stored != self.status and (stored, self.status) not in self.TRANSITIONS:
                raise error(f"A loan write-off does not move from {stored} to {self.status}.",
                            f"لا ينتقل شطب القرض من {stored} إلى {self.status}.", code="write_off_status")
        super().save(*args, **kwargs)


class EmployeeCostItem(Tracked, ServiceWritten):
    """R63 (P19(c)): a cost paid outside payroll that belongs to one employee and month (medical insurance, iqama
    renewal, work permit, visa, other government fees, other benefits), referencing its source document of the same
    entity. Posts nothing (the source document booked the cost); feeds only the fully loaded secondment cost report."""

    class Kind(models.TextChoices):
        MEDICAL_INSURANCE = "medical_insurance", bi("Medical insurance", "التأمين الطبي")
        IQAMA_RENEWAL = "iqama_renewal", bi("Iqama (residence) renewal and fees", "تجديد الإقامة ورسومها")
        WORK_PERMIT = "work_permit", bi("Work permit fee", "رسوم رخصة العمل")
        VISA = "visa", bi("Visa fees", "رسوم التأشيرات")
        OTHER_GOVERNMENT_FEE = "other_government_fee", bi("Other government fee", "رسوم حكومية أخرى")
        OTHER_BENEFIT = "other_benefit", bi("Other benefit", "مزايا أخرى")

    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        APPROVED = "approved", bi("Approved", "معتمد")
        REJECTED = "rejected", bi("Rejected", "مرفوض")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    employee = models.ForeignKey("hr.Employee", on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("Employee", "الموظف"))
    month = models.CharField(bi("Month (YYYY-MM)", "الشهر"), max_length=7)
    kind = models.CharField(bi("Kind", "النوع"), max_length=24, choices=Kind.choices)
    amount = _money("Amount", "المبلغ")
    description_en = models.CharField(bi("Description (English)", "الوصف (بالإنجليزية)"), max_length=300, blank=True)
    description_ar = models.CharField(bi("Description (Arabic)", "الوصف (بالعربية)"), max_length=300)
    source_bill = models.ForeignKey("invoicing.PurchaseDocument", null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=bi("Source bill", "فاتورة المصدر"))
    source_entry = models.ForeignKey("ledger.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+", verbose_name=bi("Source journal entry", "قيد المصدر"))
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.DRAFT)
    approved_by = _user("Approved by", "اعتمده")
    approved_at = models.DateTimeField(bi("Approved at", "تاريخ الاعتماد"), null=True, blank=True)

    class Meta:
        base_manager_name = "objects"
        verbose_name = bi("employee cost item", "بند تكلفة موظف")
        verbose_name_plural = bi("employee cost items", "بنود تكاليف الموظفين")
        ordering = ["entity", "month", "id"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="payroll_cost_item_positive"),
            models.CheckConstraint(condition=(Q(source_bill__isnull=False) & Q(source_entry__isnull=True))
                                   | (Q(source_bill__isnull=True) & Q(source_entry__isnull=False)),
                                   name="payroll_cost_item_one_source"),
        ]


class SecondmentCostReport(Tracked, ServiceWritten):
    """R35, R63 (P19(c), billing R71): the fully loaded cost of each secondment for one month, prepared by the payroll
    officer after the month's run is approved and approved by the approver (A, not the preparer). Billing reads only
    the approved report (``payroll.billing_port``). A later correction is a new version; the earlier one is
    superseded. Codes only, no personal data beyond ``employee_code``."""

    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        SUBMITTED = "submitted", bi("Submitted", "مقدّم")
        APPROVED = "approved", bi("Approved", "معتمد")
        SUPERSEDED = "superseded", bi("Superseded by a later version", "استُبدل بنسخة لاحقة")
        DISCARDED = "discarded", bi("Discarded", "ملغى")

    TRANSITIONS = frozenset({("draft", "submitted"), ("submitted", "draft"), ("submitted", "approved"),
                             ("approved", "superseded"), ("draft", "discarded"), ("draft", "draft")})

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    period = models.CharField(bi("Period (YYYY-MM)", "الفترة"), max_length=7)
    version = models.PositiveIntegerField(bi("Version", "النسخة"), default=1)
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.DRAFT)
    total = _money("Total fully loaded cost", "إجمالي التكلفة الكاملة", default=ZERO)
    warnings = models.JSONField(bi("Warnings", "تنبيهات"), default=list, blank=True)
    stored_hash = models.CharField(bi("Stored lines hash", "بصمة البنود المخزنة"), max_length=64, blank=True)
    submitted_by = _user("Submitted by", "قدّمه")
    submitted_at = models.DateTimeField(bi("Submitted at", "تاريخ التقديم"), null=True, blank=True)
    approved_by = _user("Approved by", "اعتمده")
    approved_at = models.DateTimeField(bi("Approved at", "تاريخ الاعتماد"), null=True, blank=True)

    class Meta:
        base_manager_name = "objects"
        verbose_name = bi("secondment cost report", "تقرير تكلفة الإعارة")
        verbose_name_plural = bi("secondment cost reports", "تقارير تكلفة الإعارة")
        constraints = [models.UniqueConstraint(fields=["entity", "period", "version"], name="payroll_cost_report_version"),
                       models.UniqueConstraint(fields=["entity", "period"], condition=Q(status="approved"),
                                               name="payroll_one_approved_cost_report")]

    def save(self, *args, **kwargs):
        if self.pk:
            stored = type(self)._base_manager.filter(pk=self.pk).values_list("status", flat=True).first()
            if stored is not None and stored != self.status and (stored, self.status) not in self.TRANSITIONS:
                raise error(f"A cost report does not move from {stored} to {self.status}.",
                            f"لا ينتقل تقرير التكلفة من {stored} إلى {self.status}.", code="cost_report_status")
        super().save(*args, **kwargs)


class SecondmentCostLine(ServiceWritten):
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    report = models.ForeignKey(SecondmentCostReport, on_delete=models.PROTECT, related_name="lines",
                               verbose_name=bi("Report", "التقرير"))
    assignment = models.ForeignKey("hr.Assignment", on_delete=models.PROTECT, related_name="+",
                                   verbose_name=bi("Secondment", "الإعارة"))
    employee_code = models.CharField(bi("Employee code", "رقم الموظف"), max_length=40)
    receiving_entity = models.ForeignKey(Entity, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                                         verbose_name=bi("Receiving entity", "الكيان المستقبل"))
    days = models.PositiveSmallIntegerField(bi("Days on secondment", "أيام الإعارة"))
    days_in_month = models.PositiveSmallIntegerField(bi("Days in the month", "أيام الشهر"))
    gross = _money("Gross pay and benefits through payroll", "إجمالي الأجر والمزايا عبر الرواتب", default=ZERO)
    employer_gosi = _money("Employer GOSI", "حصة صاحب العمل في التأمينات", default=ZERO)
    eosb_accrual = _money("EOSB accrual", "استحقاق مكافأة نهاية الخدمة", default=ZERO)
    cost_items = models.JSONField(bi("Employee cost items by kind", "بنود تكاليف الموظف حسب النوع"), default=dict)
    cost_items_total = _money("Employee cost items", "بنود تكاليف الموظف", default=ZERO)
    total = _money("Fully loaded cost", "التكلفة الكاملة", default=ZERO)
    warnings = models.JSONField(bi("Warnings", "تنبيهات"), default=list, blank=True)

    class Meta:
        base_manager_name = "objects"
        verbose_name = bi("secondment cost line", "بند تكلفة الإعارة")
        verbose_name_plural = bi("secondment cost lines", "بنود تكلفة الإعارة")
        ordering = ["report", "employee_code", "id"]


class GosiFine(Tracked, ServiceWritten):
    """[Reg] E14 (R37): a GOSI late-payment fine recorded from GOSI's invoice: Dr 5930 / Cr 2340, approved by the
    approver (A, P). Never interest, never purified; the ERP only shows an estimate (2% per month or part, Law Art.
    9(3)) next to the GOSI figure. Tax penalties never use 5930 (P20(f))."""

    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        SUBMITTED = "submitted", bi("Submitted", "مقدّم")
        APPROVED = "approved", bi("Approved and posted", "معتمد ومرحّل")

    TRANSITIONS = frozenset({("draft", "submitted"), ("submitted", "approved"), ("submitted", "draft")})

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    period = models.CharField(bi("Contribution period (YYYY-MM)", "فترة الاشتراكات"), max_length=7)
    fine_date = models.DateField(bi("Fine date (GOSI invoice)", "تاريخ الغرامة (فاتورة التأمينات)"))
    amount = _money("Fine per GOSI", "الغرامة وفق التأمينات")
    estimate = _money("ERP estimate", "تقدير النظام", default=ZERO)
    gosi_invoice_ref = models.CharField(bi("GOSI invoice reference", "مرجع فاتورة التأمينات"), max_length=60)
    note_en = models.TextField(bi("Note (English)", "ملاحظة (بالإنجليزية)"), blank=True)
    note_ar = models.TextField(bi("Note (Arabic)", "ملاحظة (بالعربية)"), blank=True)
    number = models.CharField(bi("Number", "الرقم"), max_length=40, blank=True)
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.DRAFT)
    submitted_by = _user("Submitted by", "قدّمه")
    approved_by = _user("Approved by", "اعتمده")
    approved_at = models.DateTimeField(bi("Approved at", "تاريخ الاعتماد"), null=True, blank=True)
    journal_entry = models.OneToOneField("ledger.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                         related_name="+", verbose_name=bi("Journal entry", "القيد"))

    class Meta:
        base_manager_name = "objects"
        verbose_name = bi("GOSI late-payment fine", "غرامة تأخير التأمينات")
        verbose_name_plural = bi("GOSI late-payment fines", "غرامات تأخير التأمينات")
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name="payroll_gosi_fine_positive")]

    def save(self, *args, **kwargs):
        if self.pk:
            stored = type(self)._base_manager.filter(pk=self.pk).values_list("status", flat=True).first()
            if stored is not None and stored != self.status and (stored, self.status) not in self.TRANSITIONS:
                raise error(f"A GOSI fine does not move from {stored} to {self.status}.",
                            f"لا تنتقل غرامة التأمينات من {stored} إلى {self.status}.", code="fine_status")
        super().save(*args, **kwargs)


# =====================================================================================================
# Slice 3 (docs/specs/payroll.md §11): Mudad justifications (R79 to R81) and liability reports (R75 to R78)
# =====================================================================================================

class MudadJustification(Tracked, ServiceWritten):
    """11.3.2: why a KSA employee of an approved in-country run was paid less, nothing or late (R79). Not registered in
    the ledger (it posts nothing). Rows are never deleted: a wrong one is ``withdrawn`` with a reason (R80). The Mudad
    portal options come from ``wps.mudad.justification_options`` **[verify: Mudad option list]**; the internal reasons (11.6) are
    payroll's."""

    class Kind(models.TextChoices):
        NON_PAYMENT = "non_payment", bi("Non-payment", "عدم صرف")
        PARTIAL_PAYMENT = "partial_payment", bi("Partial payment", "صرف جزئي")
        AMOUNT_DIFFERENCE = "amount_difference", bi("Amount difference", "اختلاف المبلغ")
        LATE_PAYMENT = "late_payment", bi("Late payment", "تأخر الصرف")
        # Compliance C11: a justification entered ahead for 2 to 12 coming months of one worker (no run line yet).
        PROACTIVE = "proactive", bi("Proactive (months ahead)", "مسبق (لأشهر قادمة)")

    class Reason(models.TextChoices):
        # Arabic labels [AR-review]; the likely Mudad category of each is [verify] (11.6).
        UNPAID_LEAVE = "unpaid_leave", bi("Leave without pay", "إجازة بدون أجر")
        SICK_LEAVE_REDUCED = "sick_leave_reduced", bi("Sick leave at reduced pay", "إجازة مرضية بأجر مخفض")
        MATERNITY_GOSI = "maternity_gosi", bi("Maternity leave (GOSI compensation)", "إجازة وضع (تعويض التأمينات)")
        JOINER_LEAVER = "joiner_leaver_proration", bi("Joined or left during the month", "التحاق أو انتهاء خلال الشهر")
        FINAL_SETTLEMENT = "final_settlement", bi("Paid in the final settlement", "تُصرف في التسوية النهائية")
        CONTRACT_SUSPENDED = "contract_suspended", bi("Contract suspended", "عقد موقوف")
        COURT_ORDER = "court_order_deduction", bi("Court-ordered deduction", "حسم بأمر قضائي")
        BANK_REJECTED = "bank_rejected", bi("Bank rejected the transfer", "رفض البنك للتحويل")
        IBAN_EXCEPTION = "iban_exception", bi("Invalid bank account", "حساب بنكي غير صالح")
        PAID_OFF_CYCLE = "paid_off_cycle", bi("Paid by an off-cycle run", "صُرف في مسير إضافي")
        PAYROLL_CORRECTION = "payroll_correction", bi("Payroll correction", "تصحيح مسير الرواتب")
        OTHER = "other", bi("Other", "أخرى")

    class Status(models.TextChoices):
        SUGGESTED = "suggested", bi("Suggested", "مقترح")
        DRAFT = "draft", bi("Draft", "مسودة")
        APPROVED = "approved", bi("Approved", "معتمد")
        SUBMITTED = "submitted", bi("Submitted to Mudad", "مرفوع على مدد")
        ACCEPTED = "accepted", bi("Accepted by Mudad", "مقبول من مدد")
        REJECTED = "rejected", bi("Rejected by Mudad", "مرفوض من مدد")
        WITHDRAWN = "withdrawn", bi("Withdrawn", "مسحوب")

    class WorkerResponse(models.TextChoices):
        NONE = "none", bi("No response", "لا رد")
        AGREED = "agreed", bi("Agreed", "موافق")
        DISPUTED = "disputed", bi("Disputed", "معترض")

    class Source(models.TextChoices):
        SYSTEM = "system", bi("Suggested by the system", "مقترح من النظام")
        MANUAL = "manual", bi("Entered by hand", "مُدخل يدويًا")
        IMPORT = "import", bi("Imported", "مستورد")

    TRANSITIONS = frozenset({
        ("suggested", "draft"), ("draft", "approved"), ("approved", "submitted"), ("submitted", "accepted"),
        ("submitted", "rejected"), ("suggested", "withdrawn"), ("draft", "withdrawn"), ("approved", "withdrawn"),
    })
    FINAL = ("accepted", "rejected", "withdrawn")
    LIVE = ("suggested", "draft", "approved", "submitted")
    # Slice 3 review 4: what may still change in each status (content is frozen from approval; nothing but Mudad's
    # answer after submission). Suggested and draft rows change freely, except who and what they are about.
    FIXED = frozenset({"id", "entity_id", "payslip_id", "run_id", "employee_id", "employee_code", "created_by_id",
                       "created_at", "source"})
    CHANGEABLE = {
        "approved": frozenset({"mudad_option_ar", "mudad_option_en", "wps_file_id", "status", "submitted_on",
                               "worker_response_due", "mudad_reference", "withdrawn_reason_ar", "mudad_notice_date",
                               "due_date", "modified_by_id", "modified_at"}),
        "submitted": frozenset({"status", "worker_response", "modified_by_id", "modified_at"}),
    }

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    employee = models.ForeignKey("hr.Employee", null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("Employee", "الموظف"))
    payslip = models.ForeignKey(Payslip, null=True, blank=True, on_delete=models.PROTECT,
                                related_name="mudad_justifications",
                                verbose_name=bi("Payslip (run line)", "القسيمة (بند المسير)"))
    run = models.ForeignKey(PayrollRun, null=True, blank=True, on_delete=models.PROTECT,
                            related_name="mudad_justifications", verbose_name=bi("Run", "المسير"))
    from_period = models.CharField(bi("From period (proactive)", "من فترة (مسبق)"), max_length=7, blank=True)
    to_period = models.CharField(bi("To period (proactive)", "إلى فترة (مسبق)"), max_length=7, blank=True)
    wps_file = models.ForeignKey(WpsFile, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("Exported in WPS file", "صُدّر في ملف حماية الأجور"))
    employee_code = models.CharField(bi("Employee code", "رقم الموظف"), max_length=30)
    kind = models.CharField(bi("Kind", "النوع"), max_length=20, choices=Kind.choices)
    reason = models.CharField(bi("Reason", "السبب"), max_length=30, choices=Reason.choices)
    # Compliance C3: the Mudad portal's drop-down option, as labelled there (from wps.mudad.justification_options,
    # [verify]); empty while the parameter is empty.
    mudad_option_ar = models.CharField(bi("Mudad portal option (Arabic)", "خيار بوابة مدد (بالعربية)"), max_length=200,
                                       blank=True)
    mudad_option_en = models.CharField(bi("Mudad portal option (English)", "خيار بوابة مدد (بالإنجليزية)"),
                                       max_length=200, blank=True)
    expected_amount = _money("Expected (contract wage)", "المتوقع (أجر العقد)")
    paid_amount = _money("Paid", "المدفوع")
    description_ar = models.TextField(bi("Justification (Arabic)", "المبرر (بالعربية)"), max_length=500)
    description_en = models.TextField(bi("Justification (English)", "المبرر (بالإنجليزية)"), max_length=500, blank=True)
    evidence_ref = models.CharField(bi("Evidence reference", "مرجع الإثبات"), max_length=200, null=True, blank=True)
    mudad_notice_date = models.DateField(bi("Mudad notice date", "تاريخ إشعار مدد"), null=True, blank=True)
    due_date = models.DateField(bi("Justification due (notice + 10 days)", "موعد المبرر (الإشعار + 10 أيام)"),
                                null=True, blank=True)
    submitted_on = models.DateField(bi("Submitted on", "تاريخ الرفع"), null=True, blank=True)
    worker_response_due = models.DateField(bi("Worker response due (submission + 3 days)",
                                              "موعد رد العامل (الرفع + 3 أيام)"), null=True, blank=True)
    worker_response = models.CharField(bi("Worker response", "رد العامل"), max_length=10, choices=WorkerResponse.choices,
                                       default=WorkerResponse.NONE)
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.DRAFT)
    source = models.CharField(bi("Source", "المصدر"), max_length=10, choices=Source.choices, default=Source.MANUAL)
    approved_by = _user("Approved by", "اعتمده")
    approved_at = models.DateTimeField(bi("Approved at", "تاريخ الاعتماد"), null=True, blank=True)
    mudad_reference = models.CharField(bi("Mudad reference", "مرجع مدد"), max_length=60, blank=True)
    withdrawn_reason_ar = models.TextField(bi("Withdrawal reason (Arabic)", "سبب السحب (بالعربية)"), blank=True)
    warnings = models.JSONField(bi("Warnings", "تنبيهات"), default=list, blank=True)
    # Review 8: an imported row whose text or evidence looks like health detail is flagged, not refused.
    health_flag = models.BooleanField(bi("Possible health detail (review)", "تفصيل صحي محتمل (للمراجعة)"),
                                      default=False)

    class Meta:
        base_manager_name = "objects"
        verbose_name = bi("Mudad justification", "مبرر مدد")
        verbose_name_plural = bi("Mudad justifications", "مبررات مدد")
        ordering = ["run", "employee_code", "id"]
        constraints = [
            models.UniqueConstraint(fields=["payslip", "reason"], condition=~Q(status="withdrawn"),
                                    name="payroll_mudad_justification_once"),
            models.CheckConstraint(condition=Q(expected_amount__gte=0) & Q(paid_amount__gte=0),
                                   name="payroll_mudad_justification_amounts"),
            models.UniqueConstraint(fields=["employee"],
                                    condition=Q(kind="proactive", status__in=("suggested", "draft", "approved",
                                                                              "submitted")),
                                    name="payroll_one_live_proactive_justification"),
            models.CheckConstraint(condition=Q(kind="proactive") | Q(payslip__isnull=False),
                                   name="payroll_justification_line_or_proactive"),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            stored = type(self)._base_manager.filter(pk=self.pk).values_list("status", flat=True).first()
            if stored in self.FINAL:
                raise error("An accepted, rejected or withdrawn Mudad justification is final (R80).",
                            "مبرر مدد المقبول أو المرفوض أو المسحوب نهائي.", code="justification_status")
            if stored is not None and stored != self.status and (stored, self.status) not in self.TRANSITIONS:
                raise error(f"A Mudad justification does not move from {stored} to {self.status} (R80).",
                            f"لا ينتقل مبرر مدد من {stored} إلى {self.status}.", code="justification_status")
            if stored is not None:
                row = type(self)._base_manager.filter(pk=self.pk).values().get()
                changed = {f.attname for f in self._meta.concrete_fields if row[f.attname] != getattr(self, f.attname)}
                allowed = self.CHANGEABLE.get(stored)
                if changed & self.FIXED or (allowed is not None and changed - allowed):
                    raise error("This Mudad justification is approved or submitted: its content is frozen (R80).",
                                "اعتُمد مبرر مدد هذا أو رُفع: محتواه مجمّد.", code="justification_frozen")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise error("Mudad justifications are never deleted; withdraw a wrong one with a reason (R80).",
                    "لا تُحذف مبررات مدد؛ يُسحب الخاطئ منها مع ذكر السبب.", code="retention")


class MudadJustificationExport(ServiceWritten):
    """R81: the bilingual list of a run's approved justifications as exported (CSV or HTML), stored encrypted and kept
    like WPS files (R31: indefinitely; after 10 years only nominated for a human burn decision)."""

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    run = models.ForeignKey(PayrollRun, on_delete=models.PROTECT, related_name="mudad_justification_exports",
                            verbose_name=bi("Run", "المسير"))
    format_code = models.CharField(bi("Format", "الصيغة"), max_length=10)
    generated_by = _user("Generated by", "أنشأه", null=False)
    generated_at = models.DateTimeField(bi("Generated at", "تاريخ الإنشاء"))
    sha256 = models.CharField(bi("SHA-256", "البصمة"), max_length=64)
    row_count = models.PositiveIntegerField(bi("Justifications", "عدد المبررات"))
    justification_ids = models.JSONField(bi("Justifications exported", "المبررات المصدّرة"), default=list, blank=True)
    warnings = models.JSONField(bi("Warnings", "تنبيهات"), default=list, blank=True)
    content = EncryptedTextField(bi("File (encrypted)", "الملف (مشفر)"))
    nominated_for_burn_at = models.DateTimeField(bi("Nominated for a burn decision", "رُشح لقرار الإتلاف"), null=True,
                                                 blank=True)

    class Meta:
        base_manager_name = "objects"
        verbose_name = bi("Mudad justification export", "تصدير مبررات مدد")
        verbose_name_plural = bi("Mudad justification exports", "تصديرات مبررات مدد")

    def delete(self, *args, **kwargs):
        raise error("Justification exports are kept like WPS files; destruction is a human burn decision (R31, R81).",
                    "تُحفظ تصديرات المبررات مثل ملفات حماية الأجور؛ والإتلاف قرار بشري.", code="retention")


class PayrollLiabilityReport(ServiceWritten):
    """11.3.3 (R75 to R78): a stored, read-only snapshot of the leave liability or the EOSB provision of one entity as
    at a date. Nothing is posted (hr OQ-6). Never edited once sealed (``sha256`` set): a later report of the same
    entity, kind and date supersedes it, the only change a sealed report takes."""

    class Kind(models.TextChoices):
        LEAVE = "leave", bi("Leave liability", "التزام الإجازات")
        EOSB = "eosb", bi("End-of-service provision", "مخصص مكافأة نهاية الخدمة")

    class Status(models.TextChoices):
        GENERATED = "generated", bi("Generated", "مُنشأ")
        SUPERSEDED = "superseded", bi("Superseded", "مُستبدل")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    kind = models.CharField(bi("Kind", "النوع"), max_length=10, choices=Kind.choices)
    as_of = models.DateField(bi("As at", "كما في"))
    currency = models.ForeignKey(Currency, to_field="code", on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("Currency", "العملة"))
    basis_en = models.TextField(bi("Basis and assumptions (English)", "الأساس والافتراضات (بالإنجليزية)"))
    basis_ar = models.TextField(bi("Basis and assumptions (Arabic)", "الأساس والافتراضات (بالعربية)"))
    parameter_ids = models.JSONField(bi("Parameters read", "المعايير المقروءة"), default=list, blank=True)
    contract_version_ids = models.JSONField(bi("Contract versions read", "نسخ العقود المقروءة"), default=list,
                                            blank=True)
    excluded = models.JSONField(bi("Excluded groups and counts", "الفئات المستبعدة وأعدادها"), default=dict, blank=True)
    total_days = models.DecimalField(bi("Total days", "مجموع الأيام"), max_digits=9, decimal_places=2, default=ZERO)
    total_amount = _money("Total", "الإجمالي", default=ZERO)
    account_2500_memo = _money("Account 2500 on the date (memo)", "رصيد الحساب 2500 في التاريخ (للعلم)", null=True,
                               blank=True)
    generated_by = _user("Generated by", "أنشأه", null=False)
    generated_at = models.DateTimeField(bi("Generated at", "تاريخ الإنشاء"))
    sha256 = models.CharField(bi("SHA-256 of header and lines", "بصمة الرأس والبنود"), max_length=64, blank=True)
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.GENERATED)
    superseded_by = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                                      verbose_name=bi("Superseded by", "استُبدل بـ"))

    class Meta:
        base_manager_name = "objects"
        verbose_name = bi("payroll liability report", "تقرير التزامات الرواتب")
        verbose_name_plural = bi("payroll liability reports", "تقارير التزامات الرواتب")
        ordering = ["entity", "kind", "-as_of", "-id"]
        constraints = [models.UniqueConstraint(fields=["entity", "kind", "as_of"], condition=Q(status="generated"),
                                               name="payroll_one_current_liability_report")]

    def save(self, *args, **kwargs):
        if self.pk:
            stored = type(self)._base_manager.filter(pk=self.pk).values().first()
            if stored is not None and stored["sha256"]:
                changed = {f.attname for f in self._meta.concrete_fields
                           if stored[f.attname] != getattr(self, f.attname)}
                ok = changed <= {"status", "superseded_by_id"} and self.status == "superseded"
                if "status" in changed:
                    ok = ok and stored["status"] == "generated"
                if "superseded_by_id" in changed:
                    ok = ok and stored["superseded_by_id"] is None
                if changed and not ok:
                    raise error("A liability report is never edited; a new report supersedes it (R75).",
                                "لا يُعدّل تقرير الالتزامات؛ يحل محله تقرير جديد.", code="report_locked")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise error("Liability reports are kept; a new one supersedes (R75).",
                    "تُحفظ تقارير الالتزامات؛ ويحل محلها تقرير جديد.", code="report_locked")


class PayrollLiabilityReportLine(AppendOnly):
    """One employee of a liability report (append-only; codes only, no names or ID numbers)."""

    class Valuation(models.TextChoices):
        VALUED = "valued", bi("Valued", "مُقيّم")
        NOT_VALUED = "not_valued", bi("Not valued", "غير مُقيّم")
        EXCLUDED = "excluded", bi("Excluded", "مستبعد")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    report = models.ForeignKey(PayrollLiabilityReport, on_delete=models.PROTECT, related_name="lines",
                               verbose_name=bi("Report", "التقرير"))
    employee = models.ForeignKey("hr.Employee", on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("Employee", "الموظف"))
    employee_code = models.CharField(bi("Employee code", "رقم الموظف"), max_length=30)
    employment_basis = models.CharField(bi("Employment basis", "أساس التوظيف"), max_length=24)
    jurisdiction = models.CharField(bi("Jurisdiction", "الولاية القانونية"), max_length=2)
    valuation = models.CharField(bi("Valuation", "التقييم"), max_length=10, choices=Valuation.choices)
    reason_en = models.TextField(bi("Reason (English)", "السبب (بالإنجليزية)"), blank=True)
    reason_ar = models.TextField(bi("Reason (Arabic)", "السبب (بالعربية)"), blank=True)
    balance_days = models.DecimalField(bi("Ledger balance (days)", "رصيد السجل (أيام)"), max_digits=9, decimal_places=2,
                                       null=True, blank=True)
    pro_rata_days = models.DecimalField(bi("Pro rata of the open month (days)", "نسبة الشهر المفتوح (أيام)"),
                                        max_digits=9, decimal_places=2, null=True, blank=True)
    future_leave_days = models.DecimalField(bi("Approved leave after the date (days)",
                                               "إجازة معتمدة بعد التاريخ (أيام)"), max_digits=9, decimal_places=2,
                                            null=True, blank=True)
    untaken_days = models.DecimalField(bi("Untaken days", "الأيام غير المستخدمة"), max_digits=9, decimal_places=2,
                                       null=True, blank=True)
    daily_wage = _money("Daily wage", "الأجر اليومي", null=True, blank=True)
    service_start = models.DateField(bi("Service start", "بداية الخدمة"), null=True, blank=True)
    service_days = models.PositiveIntegerField(bi("Service days", "أيام الخدمة"), null=True, blank=True)
    excluded_days = models.PositiveIntegerField(bi("Excluded (suspended) days", "الأيام المستبعدة (الموقوفة)"),
                                                null=True, blank=True)
    wage_basis = _money("Wage basis", "أساس الأجر", null=True, blank=True)
    art84_full_award = _money("Art. 84 full award", "المكافأة الكاملة (المادة 84)", null=True, blank=True)
    art85_resignation_memo = _money("Art. 85 resignation (memo)", "الاستقالة وفق المادة 85 (للعلم)", null=True,
                                    blank=True)
    subledger_balance_memo = _money("EOSB sub-ledger balance (memo)", "رصيد سجل المخصص (للعلم)", null=True, blank=True)
    amount = _money("Amount", "المبلغ", default=ZERO)

    class Meta:
        base_manager_name = "objects"
        verbose_name = bi("liability report line", "بند تقرير الالتزامات")
        verbose_name_plural = bi("liability report lines", "بنود تقارير الالتزامات")
        ordering = ["report", "employee_code", "id"]
