"""HR (docs/specs/hr.md, as corrected by docs/compliance/hr-payroll-labour-law-2026-10-17.md).

Every business model has the employing ``entity`` (CLAUDE.md, R1). Business logic lives in ``hr.services``;
the guards here keep approved, append-only and used records from being changed outside those services
(R7, R15, R29, R40). PostgreSQL triggers (migration 0002) repeat the guards for writes that bypass the ORM.
Money is ``Decimal``: functional-currency amounts 2 dp; a remuneration contract's foreign currency up to 3 dp,
checked against that currency's own decimals (CLAUDE.md rule 7, R53). HR posts nothing to the ledger (§5).
"""
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q

from core.models import Currency, Entity, bi, validate_country_code

from .crypto import EncryptedDateField, EncryptedTextField

ZERO = Decimal("0")
# H14 (project-owner decision 2026-10-17, R56): allowed contract values and the ceiling for ksa_employee.
# Business decisions, not regulatory figures (the statutory minimum and the 5-year tier are parameters, R6).
ANNUAL_LEAVE_CONTRACT_DAYS = (21, 30)
ANNUAL_LEAVE_CEILING = Decimal("30")


class HrError(ValidationError):
    """A broken HR rule; the message carries English and Arabic ("English / العربية"). Arabic governs (Art. 9)."""

    def __init__(self, message, code=None, params=None):
        super().__init__(message, code=code, params=params)
        self.code = code

    def text(self, language="en"):
        """The message in one language (R46: the user's preferred language)."""
        parts = []
        for m in self.messages:
            en, sep, ar = m.rpartition(" / ")
            parts.append((ar if language == "ar" else en) if sep else m)
        return " ".join(parts)


def error(en, ar, code=None):
    return HrError(f"{en} / {ar}", code=code)


SERVICE_ONLY = ("This HR record changes only through hr.services.", "لا يتغير سجل الموارد البشرية هذا إلا من خلال خدمات الموارد البشرية.")
APPEND_ONLY = ("These HR records are append-only.", "سجلات الموارد البشرية هذه للإضافة فقط.")


class AppendOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise error(*APPEND_ONLY, code="append_only")

    def delete(self):
        raise error(*APPEND_ONLY, code="append_only")


class ServiceOnlyQuerySet(models.QuerySet):
    """Bulk update/delete would skip the model guards (R15, R29)."""

    def update(self, **kwargs):
        raise error(*SERVICE_ONLY, code="service_only")

    def delete(self):
        raise error(*SERVICE_ONLY, code="service_only")


class Source(models.TextChoices):
    """No ``agent`` value: agents never read or draft HR records (R13, OQ-12)."""

    HUMAN = "human", bi("Person", "شخص")
    IMPORT = "import", bi("Import", "استيراد")
    SYSTEM = "system", bi("System", "النظام")


class Tracked(models.Model):
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
                                   verbose_name=bi("Created by", "أنشأه"))
    created_at = models.DateTimeField(bi("Created at", "تاريخ الإنشاء"), auto_now_add=True)
    modified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=bi("Last modified by", "آخر من عدّله"))
    modified_at = models.DateTimeField(bi("Last modified at", "تاريخ آخر تعديل"), null=True, blank=True)
    source = models.CharField(bi("Source", "المصدر"), max_length=10, choices=Source.choices, default=Source.HUMAN)

    class Meta:
        abstract = True


class ServiceGuarded(models.Model):
    """Rows are saved only by hr.services (which set ``_service_write`` right before the one save they make),
    except plain edits of a row still in an editable status."""

    EDITABLE_STATUSES = ("draft",)
    objects = ServiceOnlyQuerySet.as_manager()

    class Meta:
        abstract = True

    def _stored_status(self):
        if not self.pk:
            return None
        return type(self)._base_manager.filter(pk=self.pk).values_list("status", flat=True).first()

    def save(self, *args, **kwargs):
        allowed, self._service_write = getattr(self, "_service_write", False), False
        if not allowed:
            stored = self._stored_status()
            if (stored or self.EDITABLE_STATUSES[0]) != self.status or (stored is not None and stored not in self.EDITABLE_STATUSES):
                raise error(*SERVICE_ONLY, code="service_only")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if not getattr(self, "_service_delete", False):
            raise error(*SERVICE_ONLY, code="service_only")
        return super().delete(*args, **kwargs)


# --- settings and sequences ----------------------------------------------------------------------

class HrSettings(models.Model):
    """One per entity (3.1). Created by ``hr_setup_entity`` (R4)."""

    class OvertimeRoute(models.TextChoices):
        MANAGING_PARTNER = "managing_partner", bi("Managing Partner", "الشريك المدير")
        DEPARTMENT_HEAD = "department_head", bi("Department head", "رئيس القسم")

    entity = models.OneToOneField(Entity, on_delete=models.PROTECT, related_name="hr_settings",
                                  verbose_name=bi("Entity", "الكيان"))
    jurisdiction = models.CharField(bi("Jurisdiction", "الولاية القانونية"), max_length=2,
                                    validators=[validate_country_code])
    has_employees = models.BooleanField(bi("Has employees", "لديه موظفون"), default=True)
    overtime_approval_route = models.CharField(bi("Overtime approval route", "مسار اعتماد العمل الإضافي"),
                                               max_length=20, choices=OvertimeRoute.choices)
    employee_code_prefix = models.CharField(bi("Employee code prefix", "بادئة رمز الموظف"), max_length=8)
    alert_days = models.JSONField(bi("Alert days before expiry", "أيام التنبيه قبل الانتهاء"), default=list)
    work_week_days = models.PositiveSmallIntegerField(bi("Work days per week", "أيام العمل في الأسبوع"), default=5)
    standard_daily_hours = models.DecimalField(bi("Standard daily hours", "ساعات العمل اليومية"), max_digits=4,
                                               decimal_places=2, default=Decimal("8.00"))
    mudad_establishment_no = models.CharField(bi("Mudad establishment no.", "رقم المنشأة في مدد"), max_length=30, blank=True)
    gosi_establishment_no = models.CharField(bi("GOSI establishment no.", "رقم المنشأة في التأمينات"), max_length=30, blank=True)
    qiwa_establishment_no = models.CharField(bi("Qiwa establishment no.", "رقم المنشأة في قوى"), max_length=30, blank=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="+", verbose_name=bi("Updated by", "حدّثه"))
    updated_at = models.DateTimeField(bi("Updated at", "تاريخ التحديث"), auto_now=True)

    class Meta:
        verbose_name = bi("HR settings", "إعدادات الموارد البشرية")
        verbose_name_plural = bi("HR settings", "إعدادات الموارد البشرية")

    def __str__(self):
        return self.entity.code


class HrSequence(models.Model):
    """Gap-free numbers per (entity, kind, year), locked with select_for_update (employee codes, assignments)."""

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    kind = models.CharField(bi("Kind", "النوع"), max_length=4)
    year = models.PositiveIntegerField(bi("Year", "السنة"), default=0)
    last_number = models.PositiveIntegerField(bi("Last number", "آخر رقم"), default=0)

    class Meta:
        verbose_name = bi("HR sequence", "تسلسل الموارد البشرية")
        verbose_name_plural = bi("HR sequences", "تسلسلات الموارد البشرية")
        constraints = [models.UniqueConstraint(fields=["entity", "kind", "year"], name="hr_one_sequence")]


# --- statutory parameters (3.9) ------------------------------------------------------------------

class StatutoryParameter(models.Model):
    """Dated regulatory figure per jurisdiction (R6, R7). No entity FK: checked on the group parent."""

    class Status(models.TextChoices):
        UNVERIFIED = "unverified", bi("Unverified", "غير متحقق منه")
        VERIFIED = "verified", bi("Verified", "متحقق منه")
        SUPERSEDED = "superseded", bi("Superseded", "مستبدل")

    jurisdiction = models.CharField(bi("Jurisdiction", "الولاية القانونية"), max_length=2,
                                    validators=[validate_country_code])
    code = models.CharField(bi("Code", "الرمز"), max_length=60, validators=[RegexValidator(
        r"^[a-z0-9_]+(\.[a-z0-9_]+)*\Z", "Lower-case words separated by dots, e.g. eosb.award_table. / "
        "كلمات بأحرف صغيرة تفصلها نقاط، مثل eosb.award_table.")])
    value = models.JSONField(bi("Value", "القيمة"), help_text=bi("Decimal strings only, never floats",
                                                                  "أرقام عشرية نصية فقط"))
    valid_from = models.DateField(bi("Valid from", "ساري من"))
    valid_to = models.DateField(bi("Valid to", "ساري حتى"), null=True, blank=True)
    source_url = models.URLField(bi("Source URL", "رابط المصدر"), max_length=500)
    source_reference = models.CharField(bi("Source reference", "مرجع المصدر"), max_length=300)
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices,
                              default=Status.UNVERIFIED)
    entered_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="+", verbose_name=bi("Entered by", "أدخله"),
                                   help_text=bi("Empty for rows seeded by migration", "فارغ للصفوف المزروعة"))
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=bi("Verified by", "تحقق منه"))
    verified_at = models.DateTimeField(bi("Verified at", "تاريخ التحقق"), null=True, blank=True)
    used_at = models.DateTimeField(bi("First used by an approval", "أول استخدام في اعتماد"), null=True, blank=True)
    created_at = models.DateTimeField(bi("Created at", "تاريخ الإنشاء"), auto_now_add=True)

    objects = ServiceOnlyQuerySet.as_manager()

    # Once used by an approved record only these may change (R7): verification and supersession.
    USED_MUTABLE = ("status", "verified_by_id", "verified_at", "used_at")

    class Meta:
        verbose_name = bi("statutory parameter", "معيار نظامي")
        verbose_name_plural = bi("statutory parameters", "المعايير النظامية")
        ordering = ["jurisdiction", "code", "valid_from"]
        constraints = [models.UniqueConstraint(fields=["jurisdiction", "code", "valid_from"], name="hr_param_once")]

    def _floats(self, value):
        if isinstance(value, float):
            return True
        if isinstance(value, dict):
            return any(self._floats(v) for v in value.values())
        if isinstance(value, list):
            return any(self._floats(v) for v in value)
        return False

    def save(self, *args, **kwargs):
        if self._floats(self.value):
            raise error("Statutory parameter values use Decimal strings, never floats.",
                        "تستخدم قيم المعايير النظامية أرقامًا عشرية نصية، لا أعدادًا عشرية ثنائية.", code="float_rejected")
        if self.pk:
            stored = type(self)._base_manager.filter(pk=self.pk).first()
            if stored and stored.used_at is not None:
                changed = [f.attname for f in self._meta.concrete_fields
                           if getattr(stored, f.attname) != getattr(self, f.attname)]
                if set(changed) - set(self.USED_MUTABLE):
                    raise error(
                        f"Parameter {self.code} from {self.valid_from} was used by an approved record and is never "
                        "edited; add a new row with a later valid-from date or supersede it.",
                        f"استُخدم المعيار {self.code} الساري من {self.valid_from} في سجل معتمد ولا يُعدَّل أبدًا؛ أضف صفًا "
                        "جديدًا بتاريخ سريان لاحق أو استبدله.", code="parameter_used")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise error("Statutory parameters are never deleted.", "لا تُحذف المعايير النظامية.", code="parameter_used")

    def __str__(self):
        return f"{self.jurisdiction} {self.code} {self.valid_from} ({self.status})"


# --- organisation (3.11, 3.12) ---------------------------------------------------------------------

class Department(Tracked):
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    code = models.SlugField(bi("Code", "الرمز"), max_length=20)
    name_en = models.CharField(bi("Name (English)", "الاسم (بالإنجليزية)"), max_length=150)
    name_ar = models.CharField(bi("Name (Arabic)", "الاسم (بالعربية)"), max_length=150)
    head = models.ForeignKey("Employee", null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                             verbose_name=bi("Department head", "رئيس القسم"))
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="children",
                               verbose_name=bi("Parent department", "القسم الأعلى"))
    is_active = models.BooleanField(bi("Active", "نشط"), default=True)

    class Meta:
        verbose_name = bi("department", "قسم")
        verbose_name_plural = bi("departments", "الأقسام")
        ordering = ["entity", "code"]
        constraints = [models.UniqueConstraint(fields=["entity", "code"], name="hr_department_code")]

    def __str__(self):
        return f"{self.code} — {self.name_en} / {self.name_ar}"


class Grade(Tracked):
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    code = models.SlugField(bi("Code", "الرمز"), max_length=20)
    name_en = models.CharField(bi("Name (English)", "الاسم (بالإنجليزية)"), max_length=150)
    name_ar = models.CharField(bi("Name (Arabic)", "الاسم (بالعربية)"), max_length=150)
    # No default (R54): the service refuses a grade without an explicit True/False.
    overtime_eligible = models.BooleanField(bi("Overtime eligible", "مؤهل للعمل الإضافي"))
    sort_order = models.PositiveSmallIntegerField(bi("Sort order", "ترتيب العرض"), default=0)
    is_active = models.BooleanField(bi("Active", "نشط"), default=True)

    class Meta:
        verbose_name = bi("grade", "درجة وظيفية")
        verbose_name_plural = bi("grades", "الدرجات الوظيفية")
        ordering = ["entity", "sort_order", "code"]
        constraints = [models.UniqueConstraint(fields=["entity", "code"], name="hr_grade_code")]

    def __str__(self):
        return f"{self.code} — {self.name_en} / {self.name_ar}"


# --- employees (3.2) -------------------------------------------------------------------------------

class EmploymentBasis(models.TextChoices):
    KSA_EMPLOYEE = "ksa_employee", bi("Employee under Saudi labour law", "موظف خاضع لنظام العمل السعودي")
    LOCAL_ENTITY_EMPLOYEE = "local_entity_employee", bi("Employee of a local group company",
                                                        "موظف لدى شركة محلية في المجموعة")
    REMUNERATION = "remuneration", bi("Cross-border remuneration", "مكافأة شهرية عبر الحدود")


class Employee(Tracked):
    class IdType(models.TextChoices):
        NATIONAL_ID = "national_id", bi("National ID", "الهوية الوطنية")
        IQAMA = "iqama", bi("Iqama (residence permit)", "الإقامة")
        GCC_ID = "gcc_id", bi("GCC ID", "هوية خليجية")
        PASSPORT = "passport", bi("Passport", "جواز السفر")

    class Gender(models.TextChoices):
        MALE = "male", bi("Male", "ذكر")
        FEMALE = "female", bi("Female", "أنثى")

    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        ACTIVE = "active", bi("Active", "نشط")
        ON_NOTICE = "on_notice", bi("On notice", "في فترة الإشعار")
        TERMINATED = "terminated", bi("Terminated", "منتهية خدمته")

    # Personal data (R10): encrypted, masked without hr.view_personal_data.
    PERSONAL_FIELDS = ("full_name_en", "full_name_ar", "id_number", "date_of_birth", "gender", "phone", "email",
                       "address", "bank_name", "iban", "country_of_residence")
    # What a payroll officer may see unmasked (§7: bank/IBAN and ID for WPS).
    PAYMENT_FIELDS = ("id_number", "bank_name", "iban")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    employee_code = models.CharField(bi("Employee code", "رمز الموظف"), max_length=20, editable=False)
    full_name_en = EncryptedTextField(bi("Full name (English)", "الاسم الكامل (بالإنجليزية)"), plain_max_length=200)
    full_name_ar = EncryptedTextField(bi("Full name (Arabic)", "الاسم الكامل (بالعربية)"), plain_max_length=200)
    nationality = models.CharField(bi("Nationality", "الجنسية"), max_length=2, validators=[validate_country_code])
    id_type = models.CharField(bi("ID type", "نوع الهوية"), max_length=12, choices=IdType.choices)
    id_number = EncryptedTextField(bi("ID number", "رقم الهوية"), plain_max_length=20)
    id_number_hash = models.CharField(max_length=64, editable=False)
    date_of_birth = EncryptedDateField(bi("Date of birth", "تاريخ الميلاد"))
    gender = EncryptedTextField(bi("Gender", "الجنس"), plain_max_length=6, choices=Gender.choices)
    phone = EncryptedTextField(bi("Phone", "الهاتف"), plain_max_length=30, blank=True, default="")
    email = EncryptedTextField(bi("Email", "البريد الإلكتروني"), plain_max_length=254, blank=True, default="")
    address = EncryptedTextField(bi("Address", "العنوان"), plain_max_length=500, blank=True, default="")
    bank_name = EncryptedTextField(bi("Bank", "البنك"), plain_max_length=100, blank=True, default="")
    iban = EncryptedTextField(bi("IBAN", "رقم الآيبان"), plain_max_length=34, blank=True, default="")
    gosi_number = models.CharField(bi("GOSI number", "رقم التأمينات"), max_length=20, blank=True)
    gosi_registered_on = models.DateField(bi("GOSI registered on", "تاريخ التسجيل في التأمينات"), null=True, blank=True)
    hire_date = models.DateField(bi("Hire date", "تاريخ التعيين"))
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.DRAFT)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                related_name="hr_employee", verbose_name=bi("User account", "حساب المستخدم"))
    employment_basis = models.CharField(bi("Employment basis", "أساس التوظيف"), max_length=24,
                                        choices=EmploymentBasis.choices)
    country_of_residence = EncryptedTextField(bi("Country of residence", "بلد الإقامة"), plain_max_length=2,
                                              blank=True, default="")
    warnings = models.JSONField(bi("Warnings", "تنبيهات"), default=list, blank=True)

    objects = ServiceOnlyQuerySet.as_manager()

    class Meta:
        verbose_name = bi("employee", "موظف")
        verbose_name_plural = bi("employees", "الموظفون")
        ordering = ["entity", "employee_code"]
        constraints = [
            models.UniqueConstraint(fields=["entity", "employee_code"], name="hr_employee_code"),
            # R9: one non-terminated employee per ID number and entity (keyed hash: the value is encrypted).
            models.UniqueConstraint(fields=["entity", "id_number_hash"], condition=~Q(status="terminated"),
                                    name="hr_employee_id_once"),
        ]

    @property
    def is_saudi(self):
        return self.nationality == "SA"

    @property
    def wps_included(self):
        """Payroll R31, R51: only KSA employees are in the Saudi WPS salary file. Remuneration staff work outside
        KSA and are outside WPS (H17); local entity employees are paid by their local payroll (H9)."""
        return self.employment_basis == EmploymentBasis.KSA_EMPLOYEE

    def save(self, *args, **kwargs):
        allowed, self._service_write = getattr(self, "_service_write", False), False
        if not allowed:
            raise error(*SERVICE_ONLY, code="service_only")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        # R14: only a draft employee with no approved record, through hr.services.delete_draft_employee.
        if not getattr(self, "_service_delete", False):
            raise error("Employees are deleted only as drafts, by their creator, through hr.services.",
                        "لا يُحذف الموظف إلا وهو مسودة، ومن منشئه، من خلال خدمات الموارد البشرية.", code="employee_delete")
        return super().delete(*args, **kwargs)

    def __str__(self):
        """Never a name or ID number (R11): the pseudonymous code only."""
        return self.employee_code


# --- salary components and contracts (3.3, 3.4) --------------------------------------------------

class SalaryComponent(models.Model):
    class Kind(models.TextChoices):
        EARNING = "earning", bi("Earning", "استحقاق")
        DEDUCTION = "deduction", bi("Deduction", "استقطاع")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    code = models.SlugField(bi("Code", "الرمز"), max_length=20)
    name_en = models.CharField(bi("Name (English)", "الاسم (بالإنجليزية)"), max_length=100)
    name_ar = models.CharField(bi("Name (Arabic)", "الاسم (بالعربية)"), max_length=100)
    kind = models.CharField(bi("Kind", "النوع"), max_length=10, choices=Kind.choices, default=Kind.EARNING)
    counts_for_gosi = models.BooleanField(bi("Counts for GOSI", "يدخل في أجر التأمينات"))
    counts_for_eosb = models.BooleanField(bi("Counts for end of service", "يدخل في أجر نهاية الخدمة"))
    counts_for_overtime_base = models.BooleanField(bi("Counts for overtime base", "يدخل في أساس العمل الإضافي"))
    deducted_for_unpaid_leave = models.BooleanField(bi("Deducted for unpaid leave", "يُخصم عن الإجازة غير المدفوعة"))
    expense_account = models.ForeignKey("ledger.Account", on_delete=models.PROTECT, related_name="+",
                                        verbose_name=bi("Expense account", "حساب المصروف"))
    is_active = models.BooleanField(bi("Active", "نشط"), default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="+", verbose_name=bi("Created by", "أنشأه"),
                                   help_text=bi("Empty for rows seeded by hr_setup_entity", "فارغ للصفوف المزروعة"))
    source = models.CharField(bi("Source", "المصدر"), max_length=10, choices=Source.choices, default=Source.SYSTEM)

    class Meta:
        verbose_name = bi("salary component", "مكوّن الراتب")
        verbose_name_plural = bi("salary components", "مكوّنات الراتب")
        ordering = ["entity", "code"]
        constraints = [models.UniqueConstraint(fields=["entity", "code"], name="hr_component_code")]

    def __str__(self):
        return f"{self.code} — {self.name_en} / {self.name_ar}"


class EmploymentContract(Tracked, ServiceGuarded):
    class Kind(models.TextChoices):
        ORIGINAL = "original", bi("Original", "أصلي")
        AMENDMENT = "amendment", bi("Amendment", "تعديل")

    class ContractType(models.TextChoices):
        FIXED = "fixed", bi("Fixed term", "محدد المدة")
        UNLIMITED = "unlimited", bi("Open-ended", "غير محدد المدة")

    class CalendarBasis(models.TextChoices):
        # Labour Law Art. 10: Hijri unless the contract or work regulations state otherwise; the unified Qiwa
        # contract (cl. 14.6) states Gregorian [compliance 2026-10-17].
        HIJRI = "hijri", bi("Hijri", "هجري")
        GREGORIAN = "gregorian", bi("Gregorian", "ميلادي")

    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        SUBMITTED = "submitted", bi("Submitted", "مقدّم")
        APPROVED = "approved", bi("Approved", "معتمد")
        REJECTED = "rejected", bi("Rejected", "مرفوض")
        WITHDRAWN = "withdrawn", bi("Withdrawn", "مسحوب")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="contracts",
                                 verbose_name=bi("Employee", "الموظف"))
    version_no = models.PositiveIntegerField(bi("Version", "رقم النسخة"))
    kind = models.CharField(bi("Kind", "النوع"), max_length=10, choices=Kind.choices)
    effective_date = models.DateField(bi("Effective date", "تاريخ السريان"))
    contract_type = models.CharField(bi("Contract type", "نوع العقد"), max_length=10, choices=ContractType.choices)
    start_date = models.DateField(bi("Start date", "تاريخ البداية"))
    end_date = models.DateField(bi("End date", "تاريخ النهاية"), null=True, blank=True)
    calendar_basis = models.CharField(bi("Calendar basis", "التقويم المعتمد"), max_length=10,
                                      choices=CalendarBasis.choices, default=CalendarBasis.HIJRI)
    # R58: the contract clause or work-regulations article that states Gregorian (e.g. unified contract cl. 14.6).
    calendar_basis_ref = models.CharField(bi("Gregorian calendar reference", "مرجع اعتماد التقويم الميلادي"),
                                          max_length=200, blank=True)
    # Probation (Art. 53-54, Regs Art. 19 [compliance 2026-10-17]): consecutive periods fixed at signing, total
    # within the parameter maximum.
    probation_periods = models.JSONField(bi("Probation periods (days)", "فترات التجربة (بالأيام)"), default=list,
                                         blank=True)
    # R17 (Art. 54): a rehire's new probation (other job, or 6+ months since the last employment) is agreed in writing.
    probation_agreement_ref = models.CharField(bi("Written agreement for a new probation", "الاتفاق المكتوب على تجربة جديدة"),
                                               max_length=200, blank=True)
    job_title_en = models.CharField(bi("Job title (English)", "المسمى الوظيفي (بالإنجليزية)"), max_length=200)
    job_title_ar = models.CharField(bi("Job title (Arabic)", "المسمى الوظيفي (بالعربية)"), max_length=200)
    occupation_code = models.CharField(bi("Occupation code", "رمز المهنة"), max_length=20, blank=True)
    # R16(b) (Art. 55): consecutive renewals of a fixed contract (3 renewals or 4 years make a Saudi's open-ended).
    renewal_count = models.PositiveSmallIntegerField(bi("Consecutive renewals", "عدد التجديدات المتتالية"), default=0)
    # R19 (Art. 99): ministerial decision allowing 9 hours a day for the category of this job.
    extended_hours_ref = models.CharField(bi("Ministerial decision for 9-hour days", "القرار الوزاري لساعات العمل التسع"),
                                          max_length=200, blank=True)
    weekly_hours = models.DecimalField(bi("Weekly hours", "ساعات العمل الأسبوعية"), max_digits=5, decimal_places=2)
    daily_hours = models.DecimalField(bi("Daily hours", "ساعات العمل اليومية"), max_digits=4, decimal_places=2)
    annual_leave_days = models.PositiveSmallIntegerField(bi("Annual leave days", "أيام الإجازة السنوية"))
    notice_days_employer = models.PositiveSmallIntegerField(bi("Notice days (employer)", "مدة الإشعار (صاحب العمل)"))
    notice_days_employee = models.PositiveSmallIntegerField(bi("Notice days (employee)", "مدة الإشعار (الموظف)"))
    # R37 (Art. 75): monthly-paid workers' notice is 60 (employer) / 30 (worker); otherwise 30 either side.
    paid_monthly = models.BooleanField(bi("Paid monthly", "يتقاضى أجره شهريًا"), default=True)
    currency = models.ForeignKey(Currency, to_field="code", on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("Currency", "العملة"))
    department = models.ForeignKey(Department, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                                   verbose_name=bi("Department", "القسم"))
    grade = models.ForeignKey(Grade, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                              verbose_name=bi("Grade", "الدرجة"))
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.DRAFT)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+", verbose_name=bi("Submitted by", "قدّمه"))
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=bi("Approved by", "اعتمده"))
    approved_at = models.DateTimeField(bi("Approved at", "تاريخ الاعتماد"), null=True, blank=True)
    rejected_reason_en = models.TextField(bi("Rejection reason (English)", "سبب الرفض (بالإنجليزية)"), blank=True)
    rejected_reason_ar = models.TextField(bi("Rejection reason (Arabic)", "سبب الرفض (بالعربية)"), blank=True)
    qiwa_contract_ref = models.CharField(bi("Qiwa contract reference", "مرجع العقد في قوى"), max_length=40, blank=True)
    # R62 (Regs Art. 25): a written agreement that suspended unpaid days do not extend the fixed term.
    suspension_agreement_ref = models.CharField(
        bi("Written agreement: term not extended by suspension", "اتفاق مكتوب: عدم تمديد المدة بأيام الإيقاف"),
        max_length=200, blank=True)
    warnings = models.JSONField(bi("Warnings", "تنبيهات"), default=list, blank=True)
    parameter_ids = models.JSONField(bi("Parameters used", "المعايير المستخدمة"), default=list, blank=True)
    # Set by an approved termination (R38), cleared by its withdrawal (R41): the only change to an approved row.
    ended_on = models.DateField(bi("Ended on", "تاريخ الانتهاء الفعلي"), null=True, blank=True)

    class Meta:
        verbose_name = bi("employment contract", "عقد عمل")
        verbose_name_plural = bi("employment contracts", "عقود العمل")
        ordering = ["entity", "employee", "version_no"]
        constraints = [models.UniqueConstraint(fields=["entity", "employee", "version_no"], name="hr_contract_version")]

    @property
    def probation_days(self):
        return sum(int(p) for p in (self.probation_periods or []))

    def __str__(self):
        return f"{self.employee.employee_code} v{self.version_no}"


class ContractComponentQuerySet(models.QuerySet):
    def _locked(self):
        return self.exclude(contract__status="draft").exists()

    def update(self, **kwargs):
        if self._locked():
            raise error(*SERVICE_ONLY, code="service_only")
        return super().update(**kwargs)

    def delete(self):
        if self._locked():
            raise error(*SERVICE_ONLY, code="service_only")
        return super().delete()


class ContractComponent(models.Model):
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    contract = models.ForeignKey(EmploymentContract, on_delete=models.PROTECT, related_name="components",
                                 verbose_name=bi("Contract", "العقد"))
    component = models.ForeignKey(SalaryComponent, on_delete=models.PROTECT, related_name="+",
                                  verbose_name=bi("Component", "المكوّن"))
    # Up to 3 dp for a remuneration contract's currency; checked against that currency's decimals (R53).
    monthly_amount = models.DecimalField(bi("Monthly amount", "المبلغ الشهري"), max_digits=21, decimal_places=3)

    objects = ContractComponentQuerySet.as_manager()

    class Meta:
        verbose_name = bi("contract component", "مكوّن العقد")
        verbose_name_plural = bi("contract components", "مكوّنات العقد")
        constraints = [models.UniqueConstraint(fields=["contract", "component"], name="hr_contract_component_once")]

    def _contract_status(self):
        return EmploymentContract._base_manager.filter(pk=self.contract_id).values_list("status", flat=True).first()

    def save(self, *args, **kwargs):
        if self.entity_id != self.contract.entity_id or self.component.entity_id != self.entity_id:
            raise error("A contract component belongs to its contract's entity.",
                        "يتبع مكوّن العقد كيان العقد.", code="entity_mismatch")
        if isinstance(self.monthly_amount, float):
            raise error("Amounts are Decimal, never float.", "المبالغ أرقام عشرية لا أعداد عشرية ثنائية.",
                        code="float_rejected")
        if self._contract_status() != "draft":
            raise error(*SERVICE_ONLY, code="service_only")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self._contract_status() != "draft":
            raise error(*SERVICE_ONLY, code="service_only")
        return super().delete(*args, **kwargs)


# --- assignments and secondments (3.5) ----------------------------------------------------------

class Assignment(Tracked, ServiceGuarded):
    class Kind(models.TextChoices):
        SECONDMENT = "secondment", bi("Secondment (billable)", "إعارة (تُفوتر)")
        ASSIGNMENT = "assignment", bi("Assignment (not billable)", "تكليف (لا يُفوتر)")

    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        SUBMITTED = "submitted", bi("Submitted", "مقدّم")
        APPROVED = "approved", bi("Approved", "معتمد")
        ACTIVE = "active", bi("Active", "سارٍ")
        ENDED = "ended", bi("Ended", "منتهٍ")
        CANCELLED = "cancelled", bi("Cancelled", "ملغى")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    reference = models.CharField(bi("Reference", "المرجع"), max_length=40, blank=True)
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="assignments",
                                 verbose_name=bi("Employee", "الموظف"))
    kind = models.CharField(bi("Kind", "النوع"), max_length=12, choices=Kind.choices)
    receiving_entity = models.ForeignKey(Entity, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                                         verbose_name=bi("Receiving entity", "الكيان المستقبِل"))
    receiving_party_en = models.CharField(bi("Receiving party (English)", "الجهة المستقبِلة (بالإنجليزية)"),
                                          max_length=200, blank=True)
    receiving_party_ar = models.CharField(bi("Receiving party (Arabic)", "الجهة المستقبِلة (بالعربية)"),
                                          max_length=200, blank=True)
    role_title_en = models.CharField(bi("Role title (English)", "المسمى (بالإنجليزية)"), max_length=200)
    role_title_ar = models.CharField(bi("Role title (Arabic)", "المسمى (بالعربية)"), max_length=200)
    start_date = models.DateField(bi("Start date", "تاريخ البداية"))
    end_date = models.DateField(bi("End date", "تاريخ النهاية"), null=True, blank=True)
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.DRAFT)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+", verbose_name=bi("Submitted by", "قدّمه"))
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=bi("Approved by", "اعتمده"))
    approved_at = models.DateTimeField(bi("Approved at", "تاريخ الاعتماد"), null=True, blank=True)
    end_reason_en = models.TextField(bi("End reason (English)", "سبب الإنهاء (بالإنجليزية)"), blank=True)
    end_reason_ar = models.TextField(bi("End reason (Arabic)", "سبب الإنهاء (بالعربية)"), blank=True)
    ended_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                 related_name="+", verbose_name=bi("Ended by", "أنهاه"))
    ended_at = models.DateTimeField(bi("Ended at", "تاريخ تسجيل الإنهاء"), null=True, blank=True)

    class Meta:
        verbose_name = bi("assignment", "تكليف أو إعارة")
        verbose_name_plural = bi("assignments", "التكليفات والإعارات")
        ordering = ["entity", "start_date"]
        constraints = [models.UniqueConstraint(fields=["entity", "reference"], condition=~Q(reference=""),
                                               name="hr_assignment_reference")]

    def __str__(self):
        return self.reference or f"{self.employee.employee_code} ({self.get_status_display()})"


class AssignmentEvent(models.Model):
    """Outbox to billing (R24, R25). Append-only except its delivery fields."""

    class Kind(models.TextChoices):
        ENDED = "ended", bi("Ended", "إنهاء")
        END_CHANGED = "end_changed", bi("End date changed", "تغيير تاريخ الإنهاء")
        CANCELLED = "cancelled", bi("Cancelled", "إلغاء")

    class Delivery(models.TextChoices):
        PENDING = "pending", bi("Pending", "قيد الانتظار")
        DELIVERED = "delivered", bi("Delivered", "تم التسليم")
        FAILED = "failed", bi("Failed", "فشل")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    assignment = models.ForeignKey(Assignment, on_delete=models.PROTECT, related_name="events",
                                   verbose_name=bi("Assignment", "التكليف"))
    kind = models.CharField(bi("Kind", "النوع"), max_length=12, choices=Kind.choices)
    end_date = models.DateField(bi("End date", "تاريخ النهاية"))
    previous_end_date = models.DateField(bi("Previous end date", "تاريخ النهاية السابق"), null=True, blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
                              verbose_name=bi("Actor", "المنفّذ"))
    created_at = models.DateTimeField(bi("Created at", "تاريخ الإنشاء"), auto_now_add=True)
    delivery_status = models.CharField(bi("Delivery", "التسليم"), max_length=10, choices=Delivery.choices,
                                       default=Delivery.PENDING)
    attempts = models.PositiveSmallIntegerField(bi("Attempts", "المحاولات"), default=0)
    last_error_en = models.TextField(bi("Last error (English)", "آخر خطأ (بالإنجليزية)"), blank=True)
    last_error_ar = models.TextField(bi("Last error (Arabic)", "آخر خطأ (بالعربية)"), blank=True)
    billing_assignment_id = models.CharField(bi("Billing assignment", "إعارة الفوترة"), max_length=60, blank=True)
    delivered_at = models.DateTimeField(bi("Delivered at", "تاريخ التسليم"), null=True, blank=True)

    DELIVERY_FIELDS = ("delivery_status", "attempts", "last_error_en", "last_error_ar", "billing_assignment_id",
                       "delivered_at")

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        verbose_name = bi("assignment event", "حدث تكليف")
        verbose_name_plural = bi("assignment events", "أحداث التكليفات")
        ordering = ["id"]

    def save(self, *args, **kwargs):
        if self.pk:
            update_fields = kwargs.get("update_fields")
            if not update_fields or set(update_fields) - set(self.DELIVERY_FIELDS):
                raise error(*APPEND_ONLY, code="append_only")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise error(*APPEND_ONLY, code="append_only")


# --- leave (3.6, 3.7) ------------------------------------------------------------------------------

class LeaveType(models.Model):
    class PaidRule(models.TextChoices):
        FULL = "full", bi("Full pay", "بأجر كامل")
        TIERED = "tiered", bi("Tiered pay", "أجر متدرج")
        UNPAID = "unpaid", bi("Unpaid", "بدون أجر")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    code = models.SlugField(bi("Code", "الرمز"), max_length=30)
    name_en = models.CharField(bi("Name (English)", "الاسم (بالإنجليزية)"), max_length=100)
    name_ar = models.CharField(bi("Name (Arabic)", "الاسم (بالعربية)"), max_length=100)
    statutory = models.BooleanField(bi("Statutory", "نظامية"))
    parameter_code = models.CharField(bi("Parameter code", "رمز المعيار"), max_length=60, blank=True)
    paid_rule = models.CharField(bi("Pay", "الأجر"), max_length=10, choices=PaidRule.choices)
    deducts_balance = models.BooleanField(bi("Deducts balance", "تخصم من الرصيد"), default=False)
    requires_document = models.BooleanField(bi("Requires document", "تتطلب مستندًا"), default=False)
    gender = models.CharField(bi("Gender", "الجنس"), max_length=6, choices=Employee.Gender.choices, blank=True)
    once_per_service = models.BooleanField(bi("Once per service", "مرة واحدة خلال الخدمة"), default=False)

    class Meta:
        verbose_name = bi("leave type", "نوع إجازة")
        verbose_name_plural = bi("leave types", "أنواع الإجازات")
        ordering = ["entity", "code"]
        constraints = [models.UniqueConstraint(fields=["entity", "code"], name="hr_leave_type_code")]

    def __str__(self):
        return f"{self.code} — {self.name_en} / {self.name_ar}"


class LeaveRequest(Tracked, ServiceGuarded):
    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        SUBMITTED = "submitted", bi("Submitted", "مقدّم")
        APPROVED = "approved", bi("Approved", "معتمد")
        REJECTED = "rejected", bi("Rejected", "مرفوض")
        CANCELLED = "cancelled", bi("Cancelled", "ملغى")
        TAKEN = "taken", bi("Taken", "مستنفد")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="leave_requests",
                                 verbose_name=bi("Employee", "الموظف"))
    leave_type = models.ForeignKey(LeaveType, on_delete=models.PROTECT, related_name="+",
                                   verbose_name=bi("Leave type", "نوع الإجازة"))
    start_date = models.DateField(bi("Start date", "تاريخ البداية"))
    end_date = models.DateField(bi("End date", "تاريخ النهاية"))
    days = models.DecimalField(bi("Days", "الأيام"), max_digits=6, decimal_places=2, default=ZERO)
    event_date = models.DateField(bi("Event date (birth, death, marriage)", "تاريخ الواقعة (ولادة، وفاة، زواج)"),
                                  null=True, blank=True)
    unpaid = models.BooleanField(bi("Unpaid", "بدون أجر"), default=False)
    # R33 (Art. 151): the actual birth for maternity leave; days from ``unpaid_from`` complete the mandatory weeks
    # after a late birth and are unpaid.
    actual_event_date = models.DateField(bi("Actual date of the event (birth)", "التاريخ الفعلي للواقعة (الولادة)"),
                                         null=True, blank=True)
    unpaid_from = models.DateField(bi("Unpaid from", "بدون أجر اعتبارًا من"), null=True, blank=True)
    declarations = models.JSONField(bi("Declarations", "الإقرارات"), default=dict, blank=True)
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.DRAFT)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+", verbose_name=bi("Submitted by", "قدّمه"))
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=bi("Approved by", "اعتمده"))
    approved_at = models.DateTimeField(bi("Approved at", "تاريخ الاعتماد"), null=True, blank=True)
    document_ref = models.CharField(bi("Document reference", "مرجع المستند"), max_length=100, blank=True)
    reason_en = models.TextField(bi("Reason (English)", "السبب (بالإنجليزية)"), blank=True)
    reason_ar = models.TextField(bi("Reason (Arabic)", "السبب (بالعربية)"), blank=True)

    class Meta:
        verbose_name = bi("leave request", "طلب إجازة")
        verbose_name_plural = bi("leave requests", "طلبات الإجازات")
        ordering = ["entity", "start_date"]

    def __str__(self):
        return f"{self.employee.employee_code} {self.leave_type.code} {self.start_date}"


class LeaveLedger(models.Model):
    """Append-only (R29): balance = Σ days per (employee, leave type)."""

    class Kind(models.TextChoices):
        OPENING = "opening", bi("Opening balance", "رصيد افتتاحي")
        ACCRUAL = "accrual", bi("Accrual", "استحقاق")
        TAKEN = "taken", bi("Taken", "مستخدم")
        ADJUSTMENT = "adjustment", bi("Adjustment", "تسوية")
        ENCASHMENT = "encashment", bi("Encashment", "صرف نقدي")
        REVERSAL = "reversal", bi("Reversal", "عكس")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="leave_ledger",
                                 verbose_name=bi("Employee", "الموظف"))
    leave_type = models.ForeignKey(LeaveType, on_delete=models.PROTECT, related_name="+",
                                   verbose_name=bi("Leave type", "نوع الإجازة"))
    date = models.DateField(bi("Date", "التاريخ"))
    kind = models.CharField(bi("Kind", "النوع"), max_length=10, choices=Kind.choices)
    days = models.DecimalField(bi("Days", "الأيام"), max_digits=7, decimal_places=2)
    period = models.CharField(bi("Accrual month", "شهر الاستحقاق"), max_length=7, blank=True)
    source_type = models.CharField(bi("Source type", "نوع المصدر"), max_length=60, blank=True)
    source_id = models.PositiveBigIntegerField(bi("Source", "المصدر"), null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="+", verbose_name=bi("Created by", "أنشأه"),
                                   help_text=bi("Empty for accruals written by the hr_accrue_leave job",
                                                "فارغ للاستحقاقات التي تكتبها المهمة المجدولة"))
    created_at = models.DateTimeField(bi("Created at", "تاريخ الإنشاء"), auto_now_add=True)

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        verbose_name = bi("leave ledger row", "قيد رصيد إجازة")
        verbose_name_plural = bi("leave ledger", "سجل أرصدة الإجازات")
        ordering = ["employee", "date", "id"]
        constraints = [models.UniqueConstraint(fields=["employee", "leave_type", "period"], condition=Q(kind="accrual"),
                                               name="hr_accrual_once_per_month")]

    def save(self, *args, **kwargs):
        if self.pk:
            raise error(*APPEND_ONLY, code="append_only")
        if isinstance(self.days, float):
            raise error("Days are Decimal, never float.", "الأيام أرقام عشرية لا أعداد عشرية ثنائية.", code="float_rejected")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise error(*APPEND_ONLY, code="append_only")


class LeaveAdjustment(Tracked, ServiceGuarded):
    """A correction of a leave balance, approved before its ledger row is written (R29)."""

    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        APPROVED = "approved", bi("Approved", "معتمد")
        REJECTED = "rejected", bi("Rejected", "مرفوض")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Employee", "الموظف"))
    leave_type = models.ForeignKey(LeaveType, on_delete=models.PROTECT, related_name="+",
                                   verbose_name=bi("Leave type", "نوع الإجازة"))
    date = models.DateField(bi("Date", "التاريخ"))
    days = models.DecimalField(bi("Days", "الأيام"), max_digits=7, decimal_places=2)
    reason_en = models.TextField(bi("Reason (English)", "السبب (بالإنجليزية)"), blank=True)
    reason_ar = models.TextField(bi("Reason (Arabic)", "السبب (بالعربية)"))
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.DRAFT)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=bi("Approved by", "اعتمده"))
    approved_at = models.DateTimeField(bi("Approved at", "تاريخ الاعتماد"), null=True, blank=True)

    class Meta:
        verbose_name = bi("leave adjustment", "تسوية رصيد إجازة")
        verbose_name_plural = bi("leave adjustments", "تسويات أرصدة الإجازات")


class SickLeaveDay(models.Model):
    """Derived, append-only (R32): one row per approved sick day with its pay tier (payroll's input)."""

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="sick_days",
                                 verbose_name=bi("Employee", "الموظف"))
    date = models.DateField(bi("Date", "التاريخ"))
    leave_request = models.ForeignKey(LeaveRequest, on_delete=models.PROTECT, related_name="sick_days",
                                      verbose_name=bi("Leave request", "طلب الإجازة"))
    cycle_start = models.DateField(bi("Sick-leave year starts", "بداية سنة الإجازة المرضية"))
    tier_no = models.PositiveSmallIntegerField(bi("Tier", "الشريحة"))
    # R64 (Regs Art. 24): an official holiday inside sick leave is paid in full whatever the tier.
    official_holiday = models.BooleanField(bi("Official holiday", "عطلة رسمية"), default=False)
    pay_pct = models.DecimalField(bi("Pay %", "نسبة الأجر"), max_digits=5, decimal_places=2)

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        verbose_name = bi("sick leave day", "يوم إجازة مرضية")
        verbose_name_plural = bi("sick leave days", "أيام الإجازات المرضية")
        ordering = ["employee", "date"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise error(*APPEND_ONLY, code="append_only")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise error(*APPEND_ONLY, code="append_only")


# --- documents (3.8) -------------------------------------------------------------------------------

class EmployeeDocument(Tracked):
    class DocType(models.TextChoices):
        IQAMA = "iqama", bi("Iqama", "الإقامة")
        PASSPORT = "passport", bi("Passport", "جواز السفر")
        WORK_PERMIT = "work_permit", bi("Work permit", "رخصة العمل")
        VISA = "visa", bi("Visa", "التأشيرة")
        NATIONAL_ID = "national_id", bi("National ID", "الهوية الوطنية")
        DRIVING_LICENCE = "driving_licence", bi("Driving licence", "رخصة القيادة")
        MEDICAL_INSURANCE = "medical_insurance", bi("Medical insurance", "التأمين الطبي")
        OTHER = "other", bi("Other", "أخرى")

    EXPIRY_REQUIRED = ("iqama", "passport", "work_permit")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="documents",
                                 verbose_name=bi("Employee", "الموظف"))
    doc_type = models.CharField(bi("Document type", "نوع المستند"), max_length=20, choices=DocType.choices)
    number = EncryptedTextField(bi("Number", "الرقم"), plain_max_length=40)
    issuing_country = models.CharField(bi("Issuing country", "بلد الإصدار"), max_length=2, blank=True)
    issue_date = models.DateField(bi("Issue date", "تاريخ الإصدار"), null=True, blank=True)
    expiry_date = models.DateField(bi("Expiry date", "تاريخ الانتهاء"), null=True, blank=True)
    expiry_date_hijri = models.CharField(bi("Expiry date (Hijri, as printed)", "تاريخ الانتهاء (هجري كما هو مطبوع)"),
                                         max_length=10, blank=True)
    superseded_by = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                                      verbose_name=bi("Superseded by", "استُبدل بـ"))
    alerts_sent = models.JSONField(bi("Alerts sent", "التنبيهات المرسلة"), default=dict, blank=True)

    objects = ServiceOnlyQuerySet.as_manager()

    class Meta:
        verbose_name = bi("employee document", "مستند موظف")
        verbose_name_plural = bi("employee documents", "مستندات الموظفين")
        ordering = ["entity", "employee", "doc_type", "expiry_date"]

    def save(self, *args, **kwargs):
        allowed, self._service_write = getattr(self, "_service_write", False), False
        if not allowed:
            raise error(*SERVICE_ONLY, code="service_only")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise error("Employee documents are never deleted while the employee exists (R45).",
                    "لا تُحذف مستندات الموظف ما دام الموظف موجودًا.", code="document_delete")

    def __str__(self):
        return f"{self.employee.employee_code} {self.doc_type}"


class HrAlert(models.Model):
    """In-app alert (R25, R43). Text carries the employee code, never names or ID numbers (R11)."""

    class Kind(models.TextChoices):
        DOCUMENT_EXPIRY = "document_expiry", bi("Document expiry", "انتهاء مستند")
        BILLING_DELIVERY_FAILED = "billing_delivery_failed", bi("Billing delivery failed", "تعذر التسليم للفوترة")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="hr_alerts",
                                  verbose_name=bi("Recipient", "المستلم"))
    kind = models.CharField(bi("Kind", "النوع"), max_length=30, choices=Kind.choices)
    text_en = models.TextField(bi("Text (English)", "النص (بالإنجليزية)"))
    text_ar = models.TextField(bi("Text (Arabic)", "النص (بالعربية)"))
    language = models.CharField(bi("Language", "اللغة"), max_length=2, default="en")
    created_at = models.DateTimeField(bi("Created at", "تاريخ الإنشاء"), auto_now_add=True)
    read_at = models.DateTimeField(bi("Read at", "تاريخ القراءة"), null=True, blank=True)

    class Meta:
        verbose_name = bi("HR alert", "تنبيه موارد بشرية")
        verbose_name_plural = bi("HR alerts", "تنبيهات الموارد البشرية")
        ordering = ["-created_at"]

    @property
    def text(self):
        return self.text_ar if self.language == "ar" else self.text_en


# --- termination and end of service (3.10) -------------------------------------------------------

class Termination(Tracked, ServiceGuarded):
    class Kind(models.TextChoices):
        # Art. 79 bis: the worker's written wish to end a fixed-term contract [compliance 2026-10-17].
        RESIGNATION = "resignation", bi("Resignation (fixed-term, Art. 79 bis)", "استقالة (عقد محدد المدة، المادة 79 مكرر)")
        EMPLOYEE_NOTICE_ART75 = "employee_notice_art75", bi("Employee ends open-ended contract (Art. 75)",
                                                            "إنهاء الموظف لعقد غير محدد المدة (المادة 75)")
        EMPLOYER_TERMINATION = "employer_termination", bi("Employer termination with notice (Art. 75, 84)",
                                                          "إنهاء صاحب العمل مع الإشعار (المادتان 75 و84)")
        END_OF_FIXED_TERM = "end_of_fixed_term", bi("End of fixed term (Art. 74(2), 84)", "انتهاء المدة (المادتان 74(2) و84)")
        MUTUAL_AGREEMENT = "mutual_agreement", bi("Mutual agreement (Art. 74(1))", "اتفاق الطرفين (المادة 74(1))")
        ART80_DISMISSAL = "art80_dismissal", bi("Dismissal under Art. 80", "فصل وفق المادة 80")
        ART81_EMPLOYEE_LEAVES = "art81_employee_leaves", bi("Employee leaves keeping rights (Art. 81)",
                                                            "ترك العمل مع الاحتفاظ بالحقوق (المادة 81)")
        FORCE_MAJEURE = "force_majeure", bi("Force majeure (Art. 87)", "قوة قاهرة (المادة 87)")
        FEMALE_MARRIAGE_CHILDBIRTH = "female_marriage_childbirth", bi("Marriage or childbirth (Art. 87)",
                                                                      "الزواج أو الولادة (المادة 87)")
        DEATH = "death", bi("Death (Art. 79)", "الوفاة (المادة 79)")
        INCAPACITY = "incapacity", bi("Incapacity (Art. 79)", "العجز (المادة 79)")
        RETIREMENT = "retirement", bi("Retirement (Art. 74(4))", "التقاعد (المادة 74(4))")
        CLOSURE = "closure", bi("Closure of the establishment (Art. 74(6))", "إغلاق المنشأة (المادة 74(6))")
        ACTIVITY_ENDED = "activity_ended", bi("Activity ended (Art. 74(7))", "إنهاء النشاط (المادة 74(7))")
        BANKRUPTCY = "bankruptcy", bi("Bankruptcy (Art. 74(7 bis))", "الإفلاس (المادة 74(7) مكرر)")
        PROBATION_TERMINATION = "probation_termination", bi("Termination in probation (Art. 54)",
                                                            "إنهاء خلال فترة التجربة (المادة 54)")
        END_OF_REMUNERATION = "end_of_remuneration", bi("End of remuneration", "انتهاء المكافأة الشهرية")

    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        SUBMITTED = "submitted", bi("Submitted", "مقدّم")
        APPROVED = "approved", bi("Approved", "معتمد")
        WITHDRAWN = "withdrawn", bi("Withdrawn", "مسحوب")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="terminations",
                                 verbose_name=bi("Employee", "الموظف"))
    contract = models.ForeignKey(EmploymentContract, on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("Contract version", "نسخة العقد"))
    kind = models.CharField(bi("Kind", "النوع"), max_length=30, choices=Kind.choices)
    article_ref = models.CharField(bi("Article", "المادة"), max_length=20, blank=True)
    notice_given_on = models.DateField(bi("Notice given on", "تاريخ الإشعار"), null=True, blank=True)
    # Art. 79 bis resignation dates [compliance 2026-10-17]: the last working day is derived, never typed.
    submitted_on = models.DateField(bi("Resignation submitted on", "تاريخ تقديم الاستقالة"), null=True, blank=True)
    accepted_on = models.DateField(bi("Accepted on", "تاريخ القبول"), null=True, blank=True)
    postponed_until = models.DateField(bi("Acceptance postponed until", "تأجيل القبول حتى"), null=True, blank=True)
    postponement_reason_ar = models.TextField(bi("Postponement reason (Arabic)", "سبب التأجيل (بالعربية)"), blank=True)
    written_consent_ref = models.CharField(bi("Written consent reference", "مرجع الموافقة الكتابية"), max_length=100,
                                           blank=True)
    deemed_accepted = models.BooleanField(bi("Deemed accepted (no reply in 30 days)", "مقبولة حكمًا (دون رد خلال 30 يومًا)"),
                                          default=False)
    withdrawn_on = models.DateField(bi("Resignation withdrawn on", "تاريخ سحب الاستقالة"), null=True, blank=True)
    # R37 (Art. 80, 81, 87, 77) [compliance 2026-10-17].
    article_paragraph = models.CharField(bi("Article paragraph", "فقرة المادة"), max_length=10, blank=True)
    hearing_ref = models.CharField(bi("Hearing record (worker could object)", "محضر سماع أقوال العامل"), max_length=200,
                                   blank=True)
    absence_warning_ref = models.CharField(bi("Written absence warning reference", "مرجع الإنذار الكتابي بالغياب"),
                                           max_length=200, blank=True)
    marriage_contract_date = models.DateField(bi("Marriage contract date", "تاريخ عقد الزواج"), null=True, blank=True)
    delivery_date = models.DateField(bi("Delivery date", "تاريخ الوضع"), null=True, blank=True)
    art77_compensation = models.DecimalField(bi("Art. 77 compensation (manual)", "التعويض وفق المادة 77 (يدوي)"),
                                             max_digits=18, decimal_places=2, null=True, blank=True)
    compensation_ref = models.CharField(bi("Court judgment or contract clause for the compensation",
                                           "حكم المحكمة أو بند العقد للتعويض"), max_length=200, blank=True)
    agreed_fraction = models.DecimalField(bi("Agreed end-of-service fraction", "نسبة المكافأة المتفق عليها"),
                                          max_digits=5, decimal_places=4, null=True, blank=True)
    # R64 (Art. 88): the worker ended the contract: final settlement within 14 days instead of 7 (payroll R34).
    worker_initiated = models.BooleanField(bi("Ended by the worker", "أنهاه العامل"), default=False)
    # R59, R60 (Art. 82, 155): the approver's stored acknowledgement of the protection warnings.
    protection_articles = models.JSONField(bi("Protection warnings (articles)", "تنبيهات الحماية (المواد)"),
                                           default=list, blank=True)
    protection_acknowledged_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                                   on_delete=models.PROTECT, related_name="+",
                                                   verbose_name=bi("Warnings acknowledged by", "أقر بالتنبيهات"))
    protection_acknowledged_at = models.DateTimeField(bi("Warnings acknowledged at", "تاريخ الإقرار بالتنبيهات"),
                                                      null=True, blank=True)
    last_working_day = models.DateField(bi("Last working day", "آخر يوم عمل"))
    reason_en = models.TextField(bi("Reason (English)", "السبب (بالإنجليزية)"), blank=True)
    reason_ar = models.TextField(bi("Reason (Arabic)", "السبب (بالعربية)"))
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.DRAFT)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+", verbose_name=bi("Submitted by", "قدّمه"))
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=bi("Approved by", "اعتمده"))
    approved_at = models.DateTimeField(bi("Approved at", "تاريخ الاعتماد"), null=True, blank=True)
    withdrawn_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+", verbose_name=bi("Withdrawn by", "سحبه"))
    withdrawn_at = models.DateTimeField(bi("Withdrawn at", "تاريخ السحب"), null=True, blank=True)
    warnings = models.JSONField(bi("Warnings", "تنبيهات"), default=list, blank=True)
    notice_shortfall_days = models.PositiveSmallIntegerField(bi("Notice shortfall (days)", "نقص مدة الإشعار (أيام)"),
                                                             default=0)

    class Meta:
        verbose_name = bi("termination", "إنهاء خدمة")
        verbose_name_plural = bi("terminations", "إنهاءات الخدمة")
        ordering = ["entity", "last_working_day"]

    def __str__(self):
        return f"{self.employee.employee_code} {self.kind} {self.last_working_day}"


class EosbCalculation(models.Model):
    """Append-only snapshot of an end-of-service calculation (R39, R40)."""

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Employee", "الموظف"))
    termination = models.ForeignKey(Termination, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="eosb_calculations", verbose_name=bi("Termination", "إنهاء الخدمة"))
    kind = models.CharField(bi("Termination kind", "نوع الإنهاء"), max_length=30, choices=Termination.Kind.choices)
    as_of = models.DateField(bi("As of", "حتى تاريخ"))
    service_start = models.DateField(bi("Service start", "بداية الخدمة"))
    service_end = models.DateField(bi("Service end", "نهاية الخدمة"))
    calendar_basis = models.CharField(bi("Calendar basis", "التقويم المعتمد"), max_length=10,
                                      choices=EmploymentContract.CalendarBasis.choices)
    service_days = models.PositiveIntegerField(bi("Service days", "أيام الخدمة"))
    excluded_days = models.PositiveIntegerField(bi("Excluded days", "الأيام المستبعدة"))
    service_years = models.DecimalField(bi("Service years", "سنوات الخدمة"), max_digits=9, decimal_places=4)
    wage_basis = models.DecimalField(bi("Wage basis", "أساس الأجر"), max_digits=18, decimal_places=2)
    wage_components = models.JSONField(bi("Wage components", "مكوّنات الأجر"))
    full_award = models.DecimalField(bi("Full award", "المكافأة الكاملة"), max_digits=18, decimal_places=2)
    fraction = models.DecimalField(bi("Fraction", "النسبة"), max_digits=5, decimal_places=4)
    fraction_text = models.CharField(bi("Fraction (exact)", "النسبة (بدقة)"), max_length=20)
    award = models.DecimalField(bi("Award", "المكافأة"), max_digits=18, decimal_places=2)
    # Open legal points the figure depends on, e.g. OQ-33 for an Art. 75 worker notice (en/ar).
    legal_flags = models.JSONField(bi("Legal sign-off flags", "مسائل تنتظر الرأي القانوني"), default=list, blank=True)
    currency = models.ForeignKey(Currency, to_field="code", on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("Currency", "العملة"))
    parameter_ids = models.JSONField(bi("Parameters used", "المعايير المستخدمة"))
    contract_version = models.PositiveIntegerField(bi("Contract version", "نسخة العقد"))
    computed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
                                    verbose_name=bi("Computed by", "حسبه"))
    computed_at = models.DateTimeField(bi("Computed at", "تاريخ الحساب"), auto_now_add=True)
    supersedes = models.OneToOneField("self", null=True, blank=True, on_delete=models.PROTECT,
                                      related_name="superseded_by", verbose_name=bi("Supersedes", "يحل محل"))

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        verbose_name = bi("end-of-service calculation", "حساب مكافأة نهاية الخدمة")
        verbose_name_plural = bi("end-of-service calculations", "حسابات مكافأة نهاية الخدمة")
        ordering = ["id"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise error("End-of-service snapshots are immutable; recalculate to supersede.",
                        "لقطات مكافأة نهاية الخدمة لا تتغير؛ أعد الحساب لإنشاء لقطة بديلة.", code="append_only")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise error(*APPEND_ONLY, code="append_only")


class FinalSettlementInput(models.Model):
    """What HR hands to payroll for the final settlement (R35, R38; payroll R34 builds and posts it)."""

    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft (with payroll)", "مسودة (لدى الرواتب)")
        SUPERSEDED = "superseded", bi("Superseded", "مستبدل")
        DISCARDED = "discarded", bi("Discarded", "ملغى")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Employee", "الموظف"))
    termination = models.ForeignKey(Termination, on_delete=models.PROTECT, related_name="settlement_inputs",
                                    verbose_name=bi("Termination", "إنهاء الخدمة"))
    eosb_calculation = models.ForeignKey(EosbCalculation, null=True, blank=True, on_delete=models.PROTECT,
                                         related_name="+", verbose_name=bi("End-of-service calculation", "حساب المكافأة"))
    last_working_day = models.DateField(bi("Last working day", "آخر يوم عمل"))
    currency = models.ForeignKey(Currency, to_field="code", on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("Currency", "العملة"))
    eosb_award = models.DecimalField(bi("End-of-service award", "مكافأة نهاية الخدمة"), max_digits=18, decimal_places=2,
                                     default=ZERO)
    leave_balance_days = models.DecimalField(bi("Annual leave balance (days)", "رصيد الإجازة السنوية (أيام)"),
                                             max_digits=7, decimal_places=2, default=ZERO)
    daily_wage = models.DecimalField(bi("Daily wage", "الأجر اليومي"), max_digits=18, decimal_places=2, default=ZERO)
    leave_encashment = models.DecimalField(bi("Leave encashment", "بدل الإجازة"), max_digits=18, decimal_places=2,
                                           default=ZERO)
    notice_shortfall_days = models.PositiveSmallIntegerField(bi("Notice shortfall (days)", "نقص مدة الإشعار (أيام)"),
                                                             default=0)
    includes_statutory_lines = models.BooleanField(bi("End-of-service, leave and GOSI lines", "بنود المكافأة والإجازة والتأمينات"),
                                                   default=True)
    # R64 / Art. 88: who ended the contract and the statutory payment deadline (none for remuneration staff, H17).
    worker_initiated = models.BooleanField(bi("Ended by the worker", "أنهاه العامل"), default=False)
    due_by = models.DateField(bi("Settlement due by (Art. 88)", "موعد سداد المخالصة (المادة 88)"), null=True, blank=True)
    art77_compensation = models.DecimalField(bi("Art. 77 compensation", "التعويض وفق المادة 77"), max_digits=18,
                                             decimal_places=2, default=ZERO)
    # H18: employee loans are recovered from the final dues (EOSB included) without the monthly 10% / 50% caps
    # (Art. 92(1), 93 apply to monthly pay only). Bai' ajel balances only with the employee's signed undertaking
    # in the bai' ajel contract (Sharia sign-off stays required, payroll OQ-33). Figures come from payroll.
    outstanding_loans = models.DecimalField(bi("Outstanding employee loans", "رصيد قروض الموظف"), max_digits=18,
                                            decimal_places=2, default=ZERO)
    outstanding_bai_ajel = models.DecimalField(bi("Outstanding bai' ajel balance", "رصيد البيع الآجل"), max_digits=18,
                                               decimal_places=2, default=ZERO)
    bai_ajel_undertaking = models.BooleanField(bi("Bai' ajel undertaking signed", "تعهد البيع الآجل موقّع"), default=False)
    # H17: remuneration staff are outside WPS (payroll R51); local entity staff use local payroll.
    wps_included = models.BooleanField(bi("In the WPS salary file", "ضمن ملف حماية الأجور"), default=True)
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.DRAFT)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
                                   verbose_name=bi("Created by", "أنشأه"))
    created_at = models.DateTimeField(bi("Created at", "تاريخ الإنشاء"), auto_now_add=True)

    class Meta:
        verbose_name = bi("final settlement input", "مدخلات المخالصة النهائية")
        verbose_name_plural = bi("final settlement inputs", "مدخلات المخالصات النهائية")
        ordering = ["id"]
