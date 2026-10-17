"""Invoicing (docs/specs/invoicing.md): customer and vendor sub-ledgers with KSA VAT, slice 1.

Every business model has an ``entity`` FK (CLAUDE.md, D1). Business logic lives in
``invoicing.services``; the guards here only keep issued, posted and reversed
records from being changed outside those services (R13, R24, R36).
Money is ``Decimal`` with 2 decimals; rates are data with effective dates (3.2).
"""
import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models

from core.models import Currency, Entity, bi, validate_country_code, validate_vat_number

ZERO = Decimal("0")
MONEY = dict(max_digits=18, decimal_places=2, default=ZERO)
RATE = dict(max_digits=20, decimal_places=10, null=True, blank=True)


class InvoicingError(ValidationError):
    """A broken invoicing rule; the message carries English and Arabic ("English / العربية")."""

    def __init__(self, message, code=None, params=None):
        super().__init__(message, code=code, params=params)
        self.code = code


def error(en, ar, code=None):
    return InvoicingError(f"{en} / {ar}", code=code)


def _digits(n, en, ar):
    return RegexValidator(rf"^[0-9]{{{n}}}\Z", f"{en} must be exactly {n} digits. / يجب أن يتكون {ar} من {n} أرقام.",
                          code="invalid_digits")


SERVICE_ONLY = (
    "This record is issued, posted or reversed; it changes only through invoicing.services. / "
    "هذا السجل صادر أو مرحّل أو معكوس؛ لا يتغير إلا من خلال خدمات الفوترة."
)
STATUS_ONLY_BY_SERVICES = (
    "A document's status changes only through invoicing.services. / تتغير حالة المستند من خلال خدمات الفوترة فقط."
)


# --- reference data (3.2): global, not per entity ------------------------------------------------

class VatCategory(models.TextChoices):
    """ZATCA UNCL5305 codes."""

    STANDARD = "S", bi("Standard rated", "خاضع للنسبة الأساسية")
    ZERO_RATED = "Z", bi("Zero rated", "خاضع للنسبة الصفرية")
    EXEMPT = "E", bi("Exempt", "معفى")
    OUT_OF_SCOPE = "O", bi("Out of scope", "خارج نطاق الضريبة")


class VatRate(models.Model):
    """Statutory VAT rate per category with effective dates. Rates are data, never constants (3.2)."""

    category = models.CharField(bi("VAT category", "فئة الضريبة"), max_length=1, choices=VatCategory.choices)
    rate = models.DecimalField(bi("Rate %", "النسبة %"), max_digits=5, decimal_places=2,
                               validators=[MinValueValidator(ZERO)])
    valid_from = models.DateField(bi("Valid from", "ساري من"))
    valid_to = models.DateField(bi("Valid to", "ساري حتى"), null=True, blank=True)
    name_en = models.CharField(bi("Name (English)", "الاسم (بالإنجليزية)"), max_length=100)
    name_ar = models.CharField(bi("Name (Arabic)", "الاسم (بالعربية)"), max_length=100)

    class Meta:
        verbose_name = bi("VAT rate", "نسبة ضريبة القيمة المضافة")
        verbose_name_plural = bi("VAT rates", "نسب ضريبة القيمة المضافة")
        ordering = ["category", "valid_from"]
        constraints = [
            models.UniqueConstraint(fields=["category", "valid_from"], name="vat_rate_one_per_start"),
            models.CheckConstraint(condition=models.Q(rate__gte=0), name="vat_rate_not_negative"),
        ]

    def __str__(self):
        return f"{self.category} {self.rate}% ({self.valid_from}–{self.valid_to or ''})"


class VatExemptionReason(models.Model):
    """ZATCA VATEX reason code; required on lines with category Z, E or O (3.2, R10)."""

    code = models.CharField(bi("Code", "الرمز"), max_length=20, unique=True)
    category = models.CharField(bi("VAT category", "فئة الضريبة"), max_length=1, choices=VatCategory.choices)
    name_en = models.CharField(bi("Name (English)", "الاسم (بالإنجليزية)"), max_length=300)
    name_ar = models.CharField(bi("Name (Arabic)", "الاسم (بالعربية)"), max_length=300)
    is_active = models.BooleanField(bi("Active", "نشط"), default=True)

    class Meta:
        verbose_name = bi("VAT exemption reason", "سبب الإعفاء من الضريبة")
        verbose_name_plural = bi("VAT exemption reasons", "أسباب الإعفاء من الضريبة")
        ordering = ["category", "code"]

    def __str__(self):
        return f"{self.code} — {self.name_en}"


class NoteReason(models.Model):
    """The five statutory credit/debit note reasons (VAT IR Art. 40(1) / 54(3); DD KSA-10). Pick list (R18, R19)."""

    code = models.CharField(bi("Code", "الرمز"), max_length=20, unique=True)
    name_en = models.CharField(bi("Reason (English)", "السبب (بالإنجليزية)"), max_length=300)
    name_ar = models.CharField(bi("Reason (Arabic)", "السبب (بالعربية)"), max_length=300)

    class Meta:
        verbose_name = bi("note reason", "سبب الإشعار")
        verbose_name_plural = bi("note reasons", "أسباب الإشعارات")
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} — {self.name_en}"


class IncomeType(models.Model):
    """Income types (D16); prefill the income account and VAT category of sales lines and feed Art. 51 (OQ-9)."""

    class Art51(models.TextChoices):
        TAXABLE = "taxable", bi("Taxable supply", "توريد خاضع")
        EXEMPT = "exempt", bi("Exempt supply", "توريد معفى")

    code = models.SlugField(bi("Code", "الرمز"), max_length=40, unique=True)
    name_en = models.CharField(bi("Name (English)", "الاسم (بالإنجليزية)"), max_length=120)
    name_ar = models.CharField(bi("Name (Arabic)", "الاسم (بالعربية)"), max_length=120)
    default_vat_category = models.CharField(bi("Default VAT category", "فئة الضريبة الافتراضية"), max_length=1,
                                            choices=VatCategory.choices)
    default_exemption_reason = models.ForeignKey(
        VatExemptionReason, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
        verbose_name=bi("Default exemption reason", "سبب الإعفاء الافتراضي"),
    )
    art51_class = models.CharField(bi("Art. 51 class", "التصنيف وفق المادة 51"), max_length=10, choices=Art51.choices)
    default_account_code = models.CharField(bi("Default income account", "حساب الإيراد الافتراضي"), max_length=20,
                                            blank=True)
    default_intercompany_account_code = models.CharField(
        bi("Default intercompany income account", "حساب الإيراد الافتراضي بين شركات المجموعة"), max_length=20, blank=True,
    )
    is_real_estate_rent = models.BooleanField(
        bi("Rent on real estate", "إيجار عقار"), default=False,
        help_text=bi("Documents get the 15-year real-estate retention class (zatca R-38)",
                     "تأخذ المستندات فئة حفظ العقارات لمدة 15 سنة"),
    )

    class Meta:
        verbose_name = bi("income type", "نوع الإيراد")
        verbose_name_plural = bi("income types", "أنواع الإيرادات")
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} — {self.name_en}"


class PaymentMeans(models.TextChoices):
    """UNCL4461 payment means codes used on documents (BT-81)."""

    INSTRUMENT_NOT_DEFINED = "1", bi("Instrument not defined", "وسيلة غير محددة")
    CASH = "10", bi("In cash", "نقدًا")
    CREDIT_TRANSFER = "30", bi("Credit transfer", "تحويل دائن")
    BANK_ACCOUNT = "42", bi("Payment to bank account", "دفع إلى حساب بنكي")
    BANK_CARD = "48", bi("Bank card", "بطاقة بنكية")


class BuyerIdScheme(models.TextChoices):
    """Buyer ID schemes in ZATCA priority order (BR-KSA-14; §3.1; TIN added by verification I2)."""

    TIN = "TIN", bi("Tax identification number", "الرقم المميز")
    CRN = "CRN", bi("Commercial registration number", "رقم السجل التجاري")
    MOM = "MOM", bi("MOMRA licence", "ترخيص وزارة الشؤون البلدية")
    MLS = "MLS", bi("MHRSD licence", "ترخيص وزارة الموارد البشرية")
    UNIFIED = "700", bi("Unified number (700)", "الرقم الموحد (700)")
    SAG = "SAG", bi("MISA licence", "ترخيص وزارة الاستثمار")
    NAT = "NAT", bi("National ID", "الهوية الوطنية")
    GCC = "GCC", bi("GCC ID", "هوية مواطني دول الخليج")
    IQA = "IQA", bi("Iqama number", "رقم الإقامة")
    PAS = "PAS", bi("Passport ID", "رقم جواز السفر")
    OTH = "OTH", bi("Other ID", "هوية أخرى")


BUYER_ID_PRIORITY = [c.value for c in BuyerIdScheme]


# --- common fields -------------------------------------------------------------------------------

class Tracked(models.Model):
    """Who created, changed and submitted a record (segregation, access R14 / R35) and its source (D8)."""

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
                                   verbose_name=bi("Created by", "أنشأه"))
    created_at = models.DateTimeField(bi("Created at", "تاريخ الإنشاء"), auto_now_add=True)
    updated_at = models.DateTimeField(bi("Updated at", "تاريخ التحديث"), auto_now=True)
    modified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=bi("Last modified by", "آخر من عدّله"))
    modified_at = models.DateTimeField(bi("Last modified at", "تاريخ آخر تعديل"), null=True, blank=True)
    source = models.CharField(
        bi("Source", "المصدر"), max_length=60, default="human",
        help_text=bi("human, import, system:<code> or agent:<name> (D8)", "شخص أو استيراد أو نظام أو وكيل ذكاء اصطناعي"),
    )

    class Meta:
        abstract = True


class Address(models.Model):
    """Party address (ZATCA buyer address, §3.1). Street, district and city are bilingual."""

    street_en = models.CharField(bi("Street (English)", "الشارع (بالإنجليزية)"), max_length=200, blank=True)
    street_ar = models.CharField(bi("Street (Arabic)", "الشارع (بالعربية)"), max_length=200, blank=True)
    building_no = models.CharField(bi("Building number", "رقم المبنى"), max_length=4, blank=True,
                                   validators=[_digits(4, "Building number", "رقم المبنى")])
    additional_no = models.CharField(bi("Secondary number", "الرقم الإضافي"), max_length=4, blank=True,
                                     validators=[_digits(4, "Secondary number", "الرقم الإضافي")])
    district_en = models.CharField(bi("District (English)", "الحي (بالإنجليزية)"), max_length=100, blank=True)
    district_ar = models.CharField(bi("District (Arabic)", "الحي (بالعربية)"), max_length=100, blank=True)
    city_en = models.CharField(bi("City (English)", "المدينة (بالإنجليزية)"), max_length=100, blank=True)
    city_ar = models.CharField(bi("City (Arabic)", "المدينة (بالعربية)"), max_length=100, blank=True)
    postal_code = models.CharField(bi("Postal code", "الرمز البريدي"), max_length=10, blank=True)
    country = models.CharField(bi("Country", "الدولة"), max_length=2, default="SA", validators=[validate_country_code])

    class Meta:
        abstract = True


# --- settings and sequences ------------------------------------------------------------------------

class InvoicingSettings(models.Model):
    """Per-entity invoicing switches (3.0)."""

    entity = models.OneToOneField(Entity, on_delete=models.PROTECT, related_name="invoicing_settings",
        verbose_name=bi("Entity", "الكيان"))
    segregation_required = models.BooleanField(
        bi("Creator may not approve", "لا يعتمد المنشئ"), default=True,
        help_text=bi("OQ-5. Approvals that post are always segregated by the ledger (access OQ-19)",
                     "الاعتمادات التي ترحّل قيودًا مفصولة دائمًا"),
    )
    auto_issue_from_approved_contract = models.BooleanField(
        bi("Issue from approved contracts without approval", "إصدار فواتير العقود المعتمدة دون اعتماد"), default=True,
    )
    deposit_vat_treatment = models.CharField(
        bi("Deposit VAT treatment", "المعاملة الضريبية للتأمينات"), max_length=40, null=True, blank=True,
        help_text=bi("Set only after tax-adviser sign-off (R66, OQ-16)", "يُحدَّد بعد موافقة المستشار الضريبي فقط"),
    )
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="+", verbose_name=bi("Updated by", "حدّثه"))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=bi("Updated at", "تاريخ التحديث"))

    class Meta:
        verbose_name = bi("invoicing settings", "إعدادات الفوترة")
        verbose_name_plural = bi("invoicing settings", "إعدادات الفوترة")

    def __str__(self):
        return f"{self.entity.code}"


class DocumentSequence(models.Model):
    """Gap-free number per (entity, document type, year); locked with select_for_update (D7, R14, OQ-4)."""

    class DocType(models.TextChoices):
        INV = "INV", bi("Tax invoice", "فاتورة ضريبية")
        CN = "CN", bi("Credit note", "إشعار دائن")
        DN = "DN", bi("Debit note", "إشعار مدين")
        PRE = "PRE", bi("Prepayment invoice", "فاتورة دفعة مقدمة")
        BILL = "BILL", bi("Vendor bill", "فاتورة مورد")
        VCN = "VCN", bi("Vendor credit note", "إشعار دائن من مورد")
        RCPT = "RCPT", bi("Customer receipt", "سند قبض")
        PAY = "PAY", bi("Vendor payment", "سند صرف")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    doc_type = models.CharField(bi("Document type", "نوع المستند"), max_length=4, choices=DocType.choices)
    year = models.PositiveIntegerField(verbose_name=bi("Year", "السنة"))
    last_number = models.PositiveIntegerField(default=0, verbose_name=bi("Last number", "آخر رقم"))

    class Meta:
        verbose_name = bi("document sequence", "تسلسل المستندات")
        verbose_name_plural = bi("document sequences", "تسلسلات المستندات")
        constraints = [models.UniqueConstraint(fields=["entity", "doc_type", "year"], name="one_sequence_per_type_year")]

    def __str__(self):
        return f"{self.entity.code}-{self.doc_type}-{self.year}: {self.last_number}"


# --- parties (3.1) -----------------------------------------------------------------------------------

class Customer(Tracked, Address):
    class Kind(models.TextChoices):
        BUSINESS = "business", bi("Business", "منشأة")
        INDIVIDUAL = "individual", bi("Individual", "فرد")
        GOVERNMENT = "government", bi("Government", "جهة حكومية")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="customers",
        verbose_name=bi("Entity", "الكيان"))
    code = models.CharField(bi("Code", "الرمز"), max_length=20)
    name_en = models.CharField(bi("Name (English)", "الاسم (بالإنجليزية)"), max_length=200, blank=True)
    name_ar = models.CharField(bi("Name (Arabic)", "الاسم (بالعربية)"), max_length=200)
    customer_kind = models.CharField(bi("Customer kind", "نوع العميل"), max_length=12, choices=Kind.choices,
                                     default=Kind.BUSINESS)
    is_vat_registered = models.BooleanField(bi("VAT registered", "مسجل في ضريبة القيمة المضافة"), default=False)
    vat_number = models.CharField(bi("VAT number", "الرقم الضريبي"), max_length=15, blank=True,
                                  validators=[validate_vat_number])
    group_entity = models.ForeignKey(Entity, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                                     verbose_name=bi("Group entity", "كيان من المجموعة"))
    payment_terms_days = models.PositiveIntegerField(bi("Payment terms (days)", "مدة السداد (أيام)"), null=True,
                                                     blank=True)
    currency = models.ForeignKey(Currency, to_field="code", on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("Default currency", "العملة الافتراضية"))
    is_active = models.BooleanField(bi("Active", "نشط"), default=True)

    class Meta:
        verbose_name = bi("customer", "عميل")
        verbose_name_plural = bi("customers", "العملاء")
        ordering = ["entity", "code"]
        constraints = [models.UniqueConstraint(fields=["entity", "code"], name="customer_code_per_entity")]

    def clean(self):
        from . import services

        problems = services.party_problems(self)
        if problems:
            raise ValidationError(problems)

    def __str__(self):
        return f"{self.code} — {self.name_ar or self.name_en}"


class CustomerIdentifier(models.Model):
    """A buyer ID held by a customer; when several exist the first in the §3.1 priority order is used (R16)."""

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="identifiers",
        verbose_name=bi("Customer", "العميل"))
    scheme = models.CharField(bi("Scheme", "نوع الهوية"), max_length=3, choices=BuyerIdScheme.choices)
    value = models.CharField(bi("Identifier", "رقم الهوية"), max_length=30,
                             validators=[RegexValidator(r"^[A-Za-z0-9]+\Z", "Letters and digits only. / أحرف وأرقام فقط.")])

    class Meta:
        verbose_name = bi("customer identifier", "هوية العميل")
        verbose_name_plural = bi("customer identifiers", "هويات العملاء")
        constraints = [models.UniqueConstraint(fields=["customer", "scheme"], name="one_identifier_per_scheme")]

    def save(self, *args, **kwargs):
        if self.entity_id != self.customer.entity_id:
            raise error("An identifier belongs to its customer's entity.", "تتبع الهوية كيان العميل.")
        super().save(*args, **kwargs)


class Vendor(Tracked, Address):
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="vendors",
        verbose_name=bi("Entity", "الكيان"))
    code = models.CharField(bi("Code", "الرمز"), max_length=20)
    name_en = models.CharField(bi("Name (English)", "الاسم (بالإنجليزية)"), max_length=200, blank=True)
    name_ar = models.CharField(bi("Name (Arabic)", "الاسم (بالعربية)"), max_length=200)
    is_vat_registered = models.BooleanField(bi("VAT registered", "مسجل في ضريبة القيمة المضافة"), default=False)
    vat_number = models.CharField(bi("VAT number", "الرقم الضريبي"), max_length=15, blank=True,
                                  validators=[validate_vat_number])
    is_resident = models.BooleanField(bi("Resident in KSA", "مقيم في المملكة"), default=True)
    residence_country = models.CharField(bi("Country of residence", "دولة الإقامة"), max_length=2, blank=True)
    group_entity = models.ForeignKey(Entity, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                                     verbose_name=bi("Group entity", "كيان من المجموعة"))
    bank_iban = models.CharField(bi("IBAN", "رقم الآيبان"), max_length=34, blank=True)
    currency = models.ForeignKey(Currency, to_field="code", on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("Default currency", "العملة الافتراضية"))
    is_active = models.BooleanField(bi("Active", "نشط"), default=True)

    class Meta:
        verbose_name = bi("vendor", "مورد")
        verbose_name_plural = bi("vendors", "الموردون")
        ordering = ["entity", "code"]
        constraints = [models.UniqueConstraint(fields=["entity", "code"], name="vendor_code_per_entity")]

    def clean(self):
        from . import services

        problems = services.party_problems(self)
        if problems:
            raise ValidationError(problems)

    def __str__(self):
        return f"{self.code} — {self.name_ar or self.name_en}"


# --- guarded documents -------------------------------------------------------------------------------

class GuardedQuerySet(models.QuerySet):
    """Bulk writes skip save()/delete(); only drafts change in bulk (R13, R24)."""

    editable_statuses = ("draft",)

    def update(self, **kwargs):
        if any(name.startswith("zatca_") for name in kwargs):  # R45
            raise error("ZATCA fields are written only by the ZATCA hand-off.",
                        "حقول الهيئة تكتبها واجهة الربط مع الهيئة فقط.", code="zatca_fields")
        if "status" in kwargs or self.exclude(status__in=self.editable_statuses).exists():
            raise error(SERVICE_ONLY.split(" / ")[0], SERVICE_ONLY.split(" / ")[1], code="service_only")
        return super().update(**kwargs)

    def delete(self):
        if self.exclude(status="draft").exists():
            raise error("Only drafts can be deleted.", "يمكن حذف المسودات فقط.", code="service_only")
        return super().delete()


class Guarded(models.Model):
    """Save guard: only drafts are edited; status and later changes come from invoicing.services (R13).

    A service sets ``obj._service_write = True`` right before the one save it makes.
    """

    objects = GuardedQuerySet.as_manager()

    class Meta:
        abstract = True

    def _stored_status(self):
        if not self.pk:
            return None
        return type(self)._base_manager.filter(pk=self.pk).values_list("status", flat=True).first()

    def save(self, *args, **kwargs):
        allowed, self._service_write = getattr(self, "_service_write", False), False
        stored = self._stored_status()
        if not allowed:
            if (stored or "draft") != self.status or (stored is None and self.status != "draft"):
                raise error(STATUS_ONLY_BY_SERVICES.split(" / ")[0], STATUS_ONLY_BY_SERVICES.split(" / ")[1],
                            code="service_only")
            if stored not in (None, "draft"):
                raise error(SERVICE_ONLY.split(" / ")[0], SERVICE_ONLY.split(" / ")[1], code="service_only")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self._stored_status() != "draft":
            raise error("Only drafts can be deleted.", "يمكن حذف المسودات فقط.", code="service_only")
        return super().delete(*args, **kwargs)


class GuardedLineQuerySet(models.QuerySet):
    def _touches_locked(self):
        return self.exclude(document__status="draft").exists()

    def update(self, **kwargs):
        if self._touches_locked():
            raise error(SERVICE_ONLY.split(" / ")[0], SERVICE_ONLY.split(" / ")[1], code="service_only")
        return super().update(**kwargs)

    def delete(self):
        if self._touches_locked():
            raise error(SERVICE_ONLY.split(" / ")[0], SERVICE_ONLY.split(" / ")[1], code="service_only")
        return super().delete()


class GuardedLine(models.Model):
    """Lines change only while their document is a draft, except through invoicing.services."""

    objects = GuardedLineQuerySet.as_manager()

    class Meta:
        abstract = True

    def _document_status(self):
        return type(self.document)._base_manager.filter(pk=self.document_id).values_list("status", flat=True).first()

    def save(self, *args, **kwargs):
        allowed, self._service_write = getattr(self, "_service_write", False), False
        if self.entity_id != self.document.entity_id:
            raise error("A line belongs to its document's entity.", "يتبع البند كيان مستنده.", code="entity_mismatch")
        for name in ("quantity", "unit_price", "line_discount"):
            if isinstance(getattr(self, name), float):  # R56: floats are rejected, not converted
                raise error(f"{name} is a float; use a Decimal or a string.",
                            f"القيمة {name} عدد عشري ثنائي؛ استخدم Decimal أو نصًا.", code="float_rejected")
        if not allowed and self._document_status() != "draft":
            raise error(SERVICE_ONLY.split(" / ")[0], SERVICE_ONLY.split(" / ")[1], code="service_only")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self._document_status() != "draft":
            raise error(SERVICE_ONLY.split(" / ")[0], SERVICE_ONLY.split(" / ")[1], code="service_only")
        return super().delete(*args, **kwargs)


class LineAmounts(models.Model):
    """Quantity, price, discount and the computed net and VAT of a document line (R9)."""

    line_no = models.PositiveIntegerField(bi("Line", "البند"))
    description_en = models.CharField(bi("Description (English)", "الوصف (بالإنجليزية)"), max_length=300, blank=True)
    description_ar = models.CharField(bi("Description (Arabic)", "الوصف (بالعربية)"), max_length=300, blank=True)
    quantity = models.DecimalField(bi("Quantity", "الكمية"), max_digits=12, decimal_places=3)
    unit_code = models.CharField(bi("Unit", "الوحدة"), max_length=3, default="C62",
                                 validators=[RegexValidator(r"^[A-Z0-9]{2,3}\Z", "UN/ECE rec 20 code. / رمز وحدة UN/ECE.")])
    unit_price = models.DecimalField(bi("Unit price", "سعر الوحدة"), max_digits=18, decimal_places=2)
    line_discount = models.DecimalField(bi("Line discount", "خصم البند"), **MONEY)
    net_amount = models.DecimalField(bi("Net amount", "المبلغ الصافي"), **MONEY)
    vat_category = models.CharField(bi("VAT category", "فئة الضريبة"), max_length=1, choices=VatCategory.choices)
    vat_rate = models.ForeignKey(VatRate, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("VAT rate", "نسبة الضريبة"))
    vat_exemption_reason = models.ForeignKey(VatExemptionReason, null=True, blank=True, on_delete=models.PROTECT,
                                             related_name="+", verbose_name=bi("Exemption reason", "سبب الإعفاء"))
    vat_amount = models.DecimalField(bi("Line VAT (display)", "ضريبة البند (للعرض)"), **MONEY)

    class Meta:
        abstract = True


# --- sales documents (3.3) ------------------------------------------------------------------------

class Snapshot(models.Model):
    """Seller and buyer data copied at issue and frozen after (R15, R72)."""

    seller_legal_name_en = models.CharField(max_length=200, blank=True,
        verbose_name=bi("Seller legal name (English)", "البائع: الاسم القانوني (بالإنجليزية)"))
    seller_legal_name_ar = models.CharField(max_length=200, blank=True,
        verbose_name=bi("Seller legal name (Arabic)", "البائع: الاسم القانوني (بالعربية)"))
    seller_vat_number = models.CharField(max_length=15, blank=True,
        verbose_name=bi("Seller VAT number", "البائع: الرقم الضريبي"))
    seller_cr_number = models.CharField(max_length=20, blank=True,
        verbose_name=bi("Seller CR number", "البائع: رقم السجل التجاري"))
    seller_unified_number = models.CharField(max_length=10, blank=True,
        verbose_name=bi("Seller unified number", "البائع: الرقم الوطني الموحد"))
    seller_building_no = models.CharField(max_length=4, blank=True,
        verbose_name=bi("Seller building number", "البائع: رقم المبنى"))
    seller_street_en = models.CharField(max_length=200, blank=True,
        verbose_name=bi("Seller street (English)", "البائع: الشارع (بالإنجليزية)"))
    seller_street_ar = models.CharField(max_length=200, blank=True,
        verbose_name=bi("Seller street (Arabic)", "البائع: الشارع (بالعربية)"))
    seller_district_en = models.CharField(max_length=100, blank=True,
        verbose_name=bi("Seller district (English)", "البائع: الحي (بالإنجليزية)"))
    seller_district_ar = models.CharField(max_length=100, blank=True,
        verbose_name=bi("Seller district (Arabic)", "البائع: الحي (بالعربية)"))
    seller_additional_no = models.CharField(max_length=4, blank=True,
        verbose_name=bi("Seller secondary number", "البائع: الرقم الإضافي"))
    seller_postal_code = models.CharField(max_length=5, blank=True,
        verbose_name=bi("Seller postal code", "البائع: الرمز البريدي"))
    seller_city_en = models.CharField(max_length=100, blank=True,
        verbose_name=bi("Seller city (English)", "البائع: المدينة (بالإنجليزية)"))
    seller_city_ar = models.CharField(max_length=100, blank=True,
        verbose_name=bi("Seller city (Arabic)", "البائع: المدينة (بالعربية)"))
    seller_country = models.CharField(max_length=2, blank=True, verbose_name=bi("Seller country", "البائع: الدولة"))
    buyer_name_en = models.CharField(max_length=200, blank=True,
        verbose_name=bi("Buyer name (English)", "المشتري: الاسم (بالإنجليزية)"))
    buyer_name_ar = models.CharField(max_length=200, blank=True,
        verbose_name=bi("Buyer name (Arabic)", "المشتري: الاسم (بالعربية)"))
    buyer_vat_number = models.CharField(max_length=15, blank=True,
        verbose_name=bi("Buyer VAT number", "المشتري: الرقم الضريبي"))
    buyer_other_id_scheme = models.CharField(max_length=3, blank=True,
        verbose_name=bi("Buyer ID scheme", "المشتري: نوع الهوية"))
    buyer_other_id = models.CharField(max_length=30, blank=True, verbose_name=bi("Buyer ID", "المشتري: رقم الهوية"))
    buyer_building_no = models.CharField(max_length=4, blank=True,
        verbose_name=bi("Buyer building number", "المشتري: رقم المبنى"))
    buyer_street_en = models.CharField(max_length=200, blank=True,
        verbose_name=bi("Buyer street (English)", "المشتري: الشارع (بالإنجليزية)"))
    buyer_street_ar = models.CharField(max_length=200, blank=True,
        verbose_name=bi("Buyer street (Arabic)", "المشتري: الشارع (بالعربية)"))
    buyer_district_en = models.CharField(max_length=100, blank=True,
        verbose_name=bi("Buyer district (English)", "المشتري: الحي (بالإنجليزية)"))
    buyer_district_ar = models.CharField(max_length=100, blank=True,
        verbose_name=bi("Buyer district (Arabic)", "المشتري: الحي (بالعربية)"))
    buyer_additional_no = models.CharField(max_length=4, blank=True,
        verbose_name=bi("Buyer secondary number", "المشتري: الرقم الإضافي"))
    buyer_postal_code = models.CharField(max_length=10, blank=True,
        verbose_name=bi("Buyer postal code", "المشتري: الرمز البريدي"))
    buyer_city_en = models.CharField(max_length=100, blank=True,
        verbose_name=bi("Buyer city (English)", "المشتري: المدينة (بالإنجليزية)"))
    buyer_city_ar = models.CharField(max_length=100, blank=True,
        verbose_name=bi("Buyer city (Arabic)", "المشتري: المدينة (بالعربية)"))
    buyer_country = models.CharField(max_length=2, blank=True, verbose_name=bi("Buyer country", "المشتري: الدولة"))

    class Meta:
        abstract = True


class SalesDocument(Tracked, Snapshot, Guarded):
    class DocType(models.TextChoices):
        INVOICE = "invoice", bi("Tax invoice", "فاتورة ضريبية")
        CREDIT_NOTE = "credit_note", bi("Credit note", "إشعار دائن")
        DEBIT_NOTE = "debit_note", bi("Debit note", "إشعار مدين")

    ZATCA_TYPE_CODES = {"invoice": "388", "credit_note": "381", "debit_note": "383"}

    class Profile(models.TextChoices):
        STANDARD = "standard", bi("Standard (B2B/B2G, clearance)", "قياسية (اعتماد مسبق)")
        SIMPLIFIED = "simplified", bi("Simplified (B2C, reporting)", "مبسطة (إبلاغ)")

    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        SUBMITTED = "submitted", bi("Submitted", "مقدَّم")
        ISSUED = "issued", bi("Issued, awaiting ZATCA", "صادر، بانتظار الهيئة")
        FINAL = "final", bi("Final", "نهائي")
        REJECTED = "rejected", bi("Rejected by ZATCA", "مرفوض من الهيئة")

    class ZatcaStatus(models.TextChoices):
        NOT_SUBMITTED = "not_submitted", bi("Not submitted", "لم يُرسل")
        PENDING = "pending", bi("Pending", "قيد الانتظار")
        CLEARED = "cleared", bi("Cleared", "معتمد")
        REPORTED = "reported", bi("Reported", "مُبلَّغ")
        CLEARED_WITH_WARNINGS = "cleared_with_warnings", bi("Cleared with warnings", "معتمد مع تحذيرات")
        REPORTED_WITH_WARNINGS = "reported_with_warnings", bi("Reported with warnings", "مُبلَّغ مع تحذيرات")
        REJECTED = "rejected", bi("Rejected", "مرفوض")

    class Retention(models.TextChoices):
        STANDARD = "standard", bi("Standard (6 years)", "عادي (6 سنوات)")
        REAL_ESTATE = "capital_asset_real_estate", bi("Real estate capital asset (15 years)", "أصل عقاري (15 سنة)")
        MOVABLE = "movable_capital_asset", bi("Movable capital asset", "أصل رأسمالي منقول")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="sales_documents",
        verbose_name=bi("Entity", "الكيان"))
    doc_type = models.CharField(bi("Document type", "نوع المستند"), max_length=12, choices=DocType.choices)
    zatca_profile = models.CharField(bi("ZATCA profile", "نوع الفاتورة"), max_length=10, choices=Profile.choices,
                                     default=Profile.STANDARD)
    flag_third_party = models.BooleanField(bi("Third party", "طرف ثالث"), default=False)
    flag_nominal = models.BooleanField(bi("Nominal supply", "توريد اسمي"), default=False)
    flag_export = models.BooleanField(bi("Export", "تصدير"), default=False)
    flag_summary = models.BooleanField(bi("Summary", "ملخصة"), default=False)
    flag_self_billed = models.BooleanField(bi("Self-billed", "فوترة ذاتية"), default=False)
    number = models.CharField(bi("Number", "الرقم"), max_length=40, blank=True)
    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, verbose_name=bi("UUID", "المعرّف الفريد"))
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.DRAFT)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+", verbose_name=bi("Submitted by", "قدّمه"))
    submitted_at = models.DateTimeField(bi("Submitted at", "تاريخ التقديم"), null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=bi("Approved by", "اعتمده"))
    approved_at = models.DateTimeField(bi("Approved at", "تاريخ الاعتماد"), null=True, blank=True)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="documents",
        verbose_name=bi("Customer", "العميل"))
    issue_date = models.DateField(bi("Issue date", "تاريخ الإصدار"))
    issue_time = models.TimeField(bi("Issue time", "وقت الإصدار"), null=True, blank=True)
    requested_issue_date = models.DateField(
        bi("Document date requested", "تاريخ المستند المطلوب"), null=True, blank=True,
        help_text=bi("The date on the draft; at approval the issue date becomes the approval day in Riyadh (BR-KSA-04)",
                     "التاريخ في المسودة؛ عند الاعتماد يصبح تاريخ الإصدار يوم الاعتماد بتوقيت الرياض"),
    )
    supply_date = models.DateField(bi("Supply date", "تاريخ التوريد"), null=True, blank=True)
    supply_end_date = models.DateField(bi("Supply end date", "تاريخ نهاية التوريد"), null=True, blank=True)
    due_date = models.DateField(bi("Due date", "تاريخ الاستحقاق"), null=True, blank=True)
    currency = models.ForeignKey(Currency, to_field="code", on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("Currency", "العملة"))
    fx_rate = models.DecimalField(bi("Rate to functional currency", "سعر الصرف إلى العملة الوظيفية"), **RATE)
    fx_rate_date = models.DateField(bi("Rate date", "تاريخ السعر"), null=True, blank=True)
    fx_rate_source = models.CharField(bi("Rate source", "مصدر السعر"), max_length=10, blank=True)
    sar_rate = models.DecimalField(bi("Rate to SAR (ZATCA)", "سعر الصرف إلى الريال (للهيئة)"), **RATE)
    payment_means_code = models.CharField(bi("Payment means", "وسيلة الدفع"), max_length=3, blank=True,
                                          choices=PaymentMeans.choices)
    original_document = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT,
                                          related_name="notes", verbose_name=bi("Original invoice", "الفاتورة الأصلية"))
    reason_code = models.ForeignKey(NoteReason, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                                    verbose_name=bi("Reason", "السبب"))
    reason_ar = models.CharField(bi("Reason (Arabic)", "السبب (بالعربية)"), max_length=300, blank=True)
    reason_en = models.CharField(bi("Reason (English)", "السبب (بالإنجليزية)"), max_length=300, blank=True)
    adjustment_event_date = models.DateField(bi("Adjustment event date", "تاريخ حدث التعديل"), null=True, blank=True)
    retention_class = models.CharField(bi("Retention class", "فئة الحفظ"), max_length=30, choices=Retention.choices,
                                       default=Retention.STANDARD)
    retention_until = models.DateField(bi("Keep until", "يُحفظ حتى"), null=True, blank=True)
    document_discount = models.DecimalField(bi("Document discount", "خصم المستند"), **MONEY)
    total_net = models.DecimalField(bi("Total net", "الإجمالي الصافي"), **MONEY)
    total_vat = models.DecimalField(bi("Total VAT", "إجمالي الضريبة"), **MONEY)
    total_gross = models.DecimalField(bi("Total gross", "الإجمالي شامل الضريبة"), **MONEY)
    total_vat_sar = models.DecimalField(bi("Total VAT in SAR", "إجمالي الضريبة بالريال"), **MONEY)
    total_gross_sar = models.DecimalField(bi("Total gross in SAR", "الإجمالي بالريال"), **MONEY)
    vat_breakdown = models.JSONField(bi("VAT per category", "الضريبة حسب الفئة"), default=list, blank=True)
    requires_purification = models.BooleanField(bi("Requires purification", "يتطلب التطهير"), default=False)
    is_intercompany = models.BooleanField(bi("Intercompany", "بين شركات المجموعة"), default=False)
    journal_entry = models.ForeignKey("ledger.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                      related_name="+", verbose_name=bi("Journal entry", "القيد اليومي"))
    notes_en = models.TextField(bi("Notes (English)", "ملاحظات (بالإنجليزية)"), blank=True)
    notes_ar = models.TextField(bi("Notes (Arabic)", "ملاحظات (بالعربية)"), blank=True)
    # ZATCA fields: written only through the ZATCA hand-off (invoicing.zatca_port and the result callback, R45).
    zatca_status = models.CharField(bi("ZATCA status", "حالة الهيئة"), max_length=24, choices=ZatcaStatus.choices,
                                    default=ZatcaStatus.NOT_SUBMITTED)
    zatca_icv = models.PositiveIntegerField(null=True, blank=True,
        verbose_name=bi("Invoice counter (ICV)", "عداد الفاتورة"))
    zatca_previous_hash = models.CharField(max_length=128, blank=True,
        verbose_name=bi("Previous invoice hash", "تجزئة الفاتورة السابقة"))
    zatca_hash = models.CharField(max_length=128, blank=True, verbose_name=bi("Invoice hash", "تجزئة الفاتورة"))
    zatca_qr = models.TextField(blank=True, verbose_name=bi("QR code", "رمز الاستجابة السريعة"))
    zatca_messages = models.JSONField(default=list, blank=True, verbose_name=bi("ZATCA messages", "رسائل الهيئة"))

    class Meta:
        verbose_name = bi("sales document", "مستند مبيعات")
        verbose_name_plural = bi("sales documents", "مستندات المبيعات")
        ordering = ["entity", "-issue_date", "number"]
        constraints = [
            models.UniqueConstraint(fields=["entity", "number"], condition=~models.Q(number=""),
                                    name="sales_number_per_entity"),
        ]

    ZATCA_FIELDS = ("zatca_status", "zatca_icv", "zatca_previous_hash", "zatca_hash", "zatca_qr", "zatca_messages")

    def save(self, *args, **kwargs):
        # R45: zatca_* change only in saves made by the ZATCA hand-off (port and result callback).
        if not getattr(self, "_service_write", False):
            stored = type(self)._base_manager.filter(pk=self.pk).values(*self.ZATCA_FIELDS).first() if self.pk else None
            current = {name: getattr(self, name) for name in self.ZATCA_FIELDS}
            expected = stored or {name: self._meta.get_field(name).get_default() for name in self.ZATCA_FIELDS}
            if current != expected:
                raise error("ZATCA fields are written only by the ZATCA hand-off.",
                            "حقول الهيئة تكتبها واجهة الربط مع الهيئة فقط.", code="zatca_fields")
        super().save(*args, **kwargs)

    @property
    def zatca_type_code(self):
        return self.ZATCA_TYPE_CODES[self.doc_type]

    @property
    def title_en(self):
        """R47: printed title (rendering is not built in slice 1; the data is)."""
        if self.doc_type == self.DocType.INVOICE:
            return "Tax Invoice" if self.zatca_profile == self.Profile.STANDARD else "Simplified Tax Invoice"
        return "Credit Note" if self.doc_type == self.DocType.CREDIT_NOTE else "Debit Note"

    @property
    def title_ar(self):
        if self.doc_type == self.DocType.INVOICE:
            return "فاتورة ضريبية" if self.zatca_profile == self.Profile.STANDARD else "فاتورة ضريبية مبسطة"
        return "إشعار دائن" if self.doc_type == self.DocType.CREDIT_NOTE else "إشعار مدين"

    def __str__(self):
        return f"{self.number or 'DRAFT'} {self.get_doc_type_display()} ({self.status})"


class SalesDocumentLine(LineAmounts, GuardedLine):
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    document = models.ForeignKey(SalesDocument, on_delete=models.CASCADE, related_name="lines",
        verbose_name=bi("Document", "المستند"))
    income_type = models.ForeignKey(IncomeType, on_delete=models.PROTECT, related_name="+",
                                    verbose_name=bi("Income type", "نوع الإيراد"))
    income_account = models.ForeignKey("ledger.Account", on_delete=models.PROTECT, related_name="+",
                                       verbose_name=bi("Income account", "حساب الإيراد"))

    class Meta:
        verbose_name = bi("sales document line", "بند مستند مبيعات")
        verbose_name_plural = bi("sales document lines", "بنود مستندات المبيعات")
        ordering = ["document", "line_no"]
        constraints = [
            models.UniqueConstraint(fields=["document", "line_no"], name="sales_line_no_unique"),
            models.CheckConstraint(condition=models.Q(quantity__gt=0), name="sales_line_quantity_positive"),
            models.CheckConstraint(condition=models.Q(unit_price__gte=0), name="sales_line_price_not_negative"),
        ]


# --- purchase documents (3.4) ---------------------------------------------------------------------

class PurchaseDocument(Tracked, Guarded):
    class DocType(models.TextChoices):
        BILL = "bill", bi("Vendor bill", "فاتورة مورد")
        VENDOR_CREDIT_NOTE = "vendor_credit_note", bi("Vendor credit note", "إشعار دائن من مورد")

    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        SUBMITTED = "submitted", bi("Submitted", "مقدَّم")
        POSTED = "posted", bi("Posted", "مرحّل")
        REVERSED = "reversed", bi("Reversed", "معكوس")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="purchase_documents",
        verbose_name=bi("Entity", "الكيان"))
    doc_type = models.CharField(bi("Document type", "نوع المستند"), max_length=20, choices=DocType.choices,
                                default=DocType.BILL)
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="documents",
        verbose_name=bi("Vendor", "المورد"))
    number = models.CharField(bi("Number", "الرقم"), max_length=40, blank=True)
    vendor_document_number = models.CharField(bi("Vendor document number", "رقم مستند المورد"), max_length=60)
    vendor_document_date = models.DateField(bi("Vendor document date", "تاريخ مستند المورد"))
    vendor_vat_number = models.CharField(bi("Vendor VAT number", "الرقم الضريبي للمورد"), max_length=15, blank=True,
                                         validators=[validate_vat_number])
    accounting_date = models.DateField(bi("Accounting date", "تاريخ القيد"))
    due_date = models.DateField(bi("Due date", "تاريخ الاستحقاق"), null=True, blank=True)
    currency = models.ForeignKey(Currency, to_field="code", on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("Currency", "العملة"))
    fx_rate = models.DecimalField(bi("Rate to functional currency", "سعر الصرف إلى العملة الوظيفية"), **RATE)
    fx_rate_date = models.DateField(bi("Rate date", "تاريخ السعر"), null=True, blank=True)
    fx_rate_source = models.CharField(bi("Rate source", "مصدر السعر"), max_length=10, blank=True)
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.DRAFT)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+", verbose_name=bi("Submitted by", "قدّمه"))
    submitted_at = models.DateTimeField(bi("Submitted at", "تاريخ التقديم"), null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=bi("Approved by", "اعتمده"))
    approved_at = models.DateTimeField(bi("Approved at", "تاريخ الاعتماد"), null=True, blank=True)
    original_document = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT,
                                          related_name="notes", verbose_name=bi("Original bill", "الفاتورة الأصلية"))
    vendor_total_vat = models.DecimalField(bi("VAT on the vendor document", "الضريبة في مستند المورد"), **MONEY)
    vendor_total_gross = models.DecimalField(bi("Total on the vendor document", "الإجمالي في مستند المورد"), **MONEY)
    reverse_charge = models.BooleanField(bi("Reverse charge", "احتساب عكسي"), default=False)
    total_net = models.DecimalField(bi("Total net", "الإجمالي الصافي"), **MONEY)
    total_vat = models.DecimalField(bi("VAT charged by the vendor", "الضريبة التي فرضها المورد"), **MONEY)
    total_gross = models.DecimalField(bi("Total payable", "إجمالي المستحق"), **MONEY)
    reverse_charge_vat = models.DecimalField(bi("Reverse-charge VAT", "ضريبة الاحتساب العكسي"), **MONEY)
    vat_breakdown = models.JSONField(default=list, blank=True, verbose_name=bi("VAT per category", "الضريبة حسب الفئة"))
    requires_purification = models.BooleanField(default=False,
        verbose_name=bi("Requires purification", "يتطلب التطهير"))
    is_intercompany = models.BooleanField(bi("Intercompany", "بين شركات المجموعة"), default=False)
    attachment = models.FileField(bi("Vendor document", "مستند المورد"), upload_to="invoicing/purchase/%Y/", blank=True)
    journal_entry = models.ForeignKey("ledger.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                      related_name="+", verbose_name=bi("Journal entry", "القيد اليومي"))
    reversal_entry = models.ForeignKey("ledger.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                       related_name="+", verbose_name=bi("Reversal entry", "قيد العكس"))

    class Meta:
        verbose_name = bi("purchase document", "مستند مشتريات")
        verbose_name_plural = bi("purchase documents", "مستندات المشتريات")
        ordering = ["entity", "-accounting_date", "number"]
        constraints = [
            models.UniqueConstraint(fields=["entity", "number"], condition=~models.Q(number=""),
                                    name="purchase_number_per_entity"),
            # R23 / OQ-10: one live document per vendor document number (duplicate-bill guard).
            models.UniqueConstraint(fields=["entity", "vendor", "vendor_document_number"],
                                    condition=~models.Q(status="reversed"), name="no_duplicate_vendor_document"),
        ]

    def __str__(self):
        return f"{self.number or 'DRAFT'} {self.vendor_document_number} ({self.status})"


class PurchaseDocumentLine(LineAmounts, GuardedLine):
    class Attribution(models.TextChoices):
        TAXABLE = "taxable", bi("Used for taxable supplies", "مستخدم لتوريدات خاضعة")
        EXEMPT = "exempt", bi("Used for exempt supplies", "مستخدم لتوريدات معفاة")
        MIXED = "mixed", bi("Mixed use", "استخدام مختلط")

    class SupplyKind(models.TextChoices):
        GOODS = "goods", bi("Goods", "سلع")
        SERVICES = "services", bi("Services", "خدمات")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    document = models.ForeignKey(PurchaseDocument, on_delete=models.CASCADE, related_name="lines",
        verbose_name=bi("Document", "المستند"))
    vat_attribution = models.CharField(bi("VAT attribution", "إسناد الضريبة"), max_length=8,
                                       choices=Attribution.choices, default=Attribution.TAXABLE)
    supply_kind = models.CharField(bi("Supply kind", "نوع التوريد"), max_length=8, choices=SupplyKind.choices,
                                   blank=True)
    vat_recoverable = models.BooleanField(bi("VAT recoverable", "الضريبة قابلة للاسترداد"), default=False)
    account = models.ForeignKey("ledger.Account", on_delete=models.PROTECT, related_name="+",
                                verbose_name=bi("Expense or asset account", "حساب المصروف أو الأصل"))

    class Meta:
        verbose_name = bi("purchase document line", "بند مستند مشتريات")
        verbose_name_plural = bi("purchase document lines", "بنود مستندات المشتريات")
        ordering = ["document", "line_no"]
        constraints = [
            models.UniqueConstraint(fields=["document", "line_no"], name="purchase_line_no_unique"),
            models.CheckConstraint(condition=models.Q(quantity__gt=0), name="purchase_line_quantity_positive"),
            models.CheckConstraint(condition=models.Q(unit_price__gte=0), name="purchase_line_price_not_negative"),
        ]


# --- money movements (3.5) --------------------------------------------------------------------------

class MoneyMovement(Tracked, Guarded):
    number = models.CharField(bi("Number", "الرقم"), max_length=40, blank=True)
    date = models.DateField(bi("Date", "التاريخ"))
    amount = models.DecimalField(bi("Amount", "المبلغ"), max_digits=18, decimal_places=2)
    currency = models.ForeignKey(Currency, to_field="code", on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("Currency", "العملة"))
    fx_rate = models.DecimalField(bi("Rate to functional currency", "سعر الصرف إلى العملة الوظيفية"), **RATE)
    fx_rate_date = models.DateField(bi("Rate date", "تاريخ السعر"), null=True, blank=True)
    fx_rate_source = models.CharField(bi("Rate source", "مصدر السعر"), max_length=10, blank=True)
    bank_account = models.ForeignKey("ledger.Account", on_delete=models.PROTECT, related_name="+",
                                     verbose_name=bi("Bank account", "الحساب البنكي"))
    reference_en = models.CharField(bi("Reference (English)", "المرجع (بالإنجليزية)"), max_length=200, blank=True)
    reference_ar = models.CharField(bi("Reference (Arabic)", "المرجع (بالعربية)"), max_length=200, blank=True)
    bank_reference = models.CharField(bi("Bank reference", "مرجع البنك"), max_length=100, blank=True)
    posted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                  related_name="+", verbose_name=bi("Posted by", "رحّله"))
    posted_at = models.DateTimeField(bi("Posted at", "تاريخ الترحيل"), null=True, blank=True)
    journal_entry = models.ForeignKey("ledger.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                      related_name="+", verbose_name=bi("Journal entry", "القيد اليومي"))
    reversal_entry = models.ForeignKey("ledger.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                       related_name="+", verbose_name=bi("Reversal entry", "قيد العكس"))

    class Meta:
        abstract = True


class CustomerReceipt(MoneyMovement):
    class Kind(models.TextChoices):
        RECEIPT = "receipt", bi("Receipt", "قبض")
        REFUND = "refund", bi("Refund to customer", "رد مبلغ للعميل")

    class Purpose(models.TextChoices):
        SETTLEMENT = "settlement", bi("Settlement", "سداد")
        ADVANCE = "advance", bi("Advance", "دفعة مقدمة")
        DEPOSIT = "deposit", bi("Security deposit", "تأمين")

    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        POSTED = "posted", bi("Posted", "مرحّل")
        REVERSED = "reversed", bi("Reversed", "معكوس")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="customer_receipts",
        verbose_name=bi("Entity", "الكيان"))
    kind = models.CharField(bi("Kind", "النوع"), max_length=8, choices=Kind.choices, default=Kind.RECEIPT)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="receipts",
        verbose_name=bi("Customer", "العميل"))
    purpose = models.CharField(bi("Purpose", "الغرض"), max_length=10, choices=Purpose.choices,
                               default=Purpose.SETTLEMENT)
    bank_charge = models.DecimalField(bi("Bank charge", "رسوم بنكية"), **MONEY)
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.DRAFT)

    class Meta:
        verbose_name = bi("customer receipt", "سند قبض")
        verbose_name_plural = bi("customer receipts", "سندات القبض")
        ordering = ["entity", "-date", "number"]
        constraints = [
            models.UniqueConstraint(fields=["entity", "number"], condition=~models.Q(number=""),
                                    name="receipt_number_per_entity"),
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="receipt_amount_positive"),
        ]

    def __str__(self):
        return f"{self.number or 'DRAFT'} {self.amount} {self.currency_id} ({self.status})"


class VendorPayment(MoneyMovement):
    class Kind(models.TextChoices):
        PAYMENT = "payment", bi("Payment", "دفع")
        REFUND_RECEIVED = "refund_received", bi("Refund received", "استرداد من المورد")

    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        SUBMITTED = "submitted", bi("Submitted", "مقدَّم")
        POSTED = "posted", bi("Posted", "مرحّل")
        REVERSED = "reversed", bi("Reversed", "معكوس")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="vendor_payments",
        verbose_name=bi("Entity", "الكيان"))
    kind = models.CharField(bi("Kind", "النوع"), max_length=16, choices=Kind.choices, default=Kind.PAYMENT)
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="payments",
        verbose_name=bi("Vendor", "المورد"))
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.DRAFT)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+", verbose_name=bi("Submitted by", "قدّمه"))
    submitted_at = models.DateTimeField(bi("Submitted at", "تاريخ التقديم"), null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=bi("Approved by", "اعتمده"))
    approved_at = models.DateTimeField(bi("Approved at", "تاريخ الاعتماد"), null=True, blank=True)

    class Meta:
        verbose_name = bi("vendor payment", "سند صرف لمورد")
        verbose_name_plural = bi("vendor payments", "سندات الصرف للموردين")
        ordering = ["entity", "-date", "number"]
        constraints = [
            models.UniqueConstraint(fields=["entity", "number"], condition=~models.Q(number=""),
                                    name="payment_number_per_entity"),
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="payment_amount_positive"),
        ]

    def __str__(self):
        return f"{self.number or 'DRAFT'} {self.amount} {self.currency_id} ({self.status})"


class Application(models.Model):
    """A planned allocation entered on a draft receipt or payment; becomes an allocation when it posts."""

    amount = models.DecimalField(bi("Amount", "المبلغ"), max_digits=18, decimal_places=2)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        source = self.source_document
        if self.entity_id != source.entity_id:
            raise error("An application belongs to its document's entity.", "يتبع التخصيص كيان مستنده.",
                        code="entity_mismatch")
        if type(source)._base_manager.filter(pk=source.pk).values_list("status", flat=True).first() != "draft":
            raise error(SERVICE_ONLY.split(" / ")[0], SERVICE_ONLY.split(" / ")[1], code="service_only")
        super().save(*args, **kwargs)


class ReceiptApplication(Application):
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    receipt = models.ForeignKey(CustomerReceipt, on_delete=models.CASCADE, related_name="applications",
        verbose_name=bi("Receipt", "سند القبض"))
    target = models.ForeignKey(SalesDocument, on_delete=models.PROTECT, related_name="+",
        verbose_name=bi("Allocated to", "مخصص إلى"))

    @property
    def source_document(self):
        return self.receipt

    class Meta:
        verbose_name = bi("receipt application", "تخصيص سند قبض")
        verbose_name_plural = bi("receipt applications", "تخصيصات سندات القبض")
        constraints = [models.CheckConstraint(condition=models.Q(amount__gt=0), name="receipt_application_positive")]


class PaymentApplication(Application):
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    payment = models.ForeignKey(VendorPayment, on_delete=models.CASCADE, related_name="applications",
        verbose_name=bi("Payment", "سند الصرف"))
    target = models.ForeignKey(PurchaseDocument, on_delete=models.PROTECT, related_name="+",
        verbose_name=bi("Allocated to", "مخصص إلى"))

    @property
    def source_document(self):
        return self.payment

    class Meta:
        verbose_name = bi("payment application", "تخصيص سند صرف")
        verbose_name_plural = bi("payment applications", "تخصيصات سندات الصرف")
        constraints = [models.CheckConstraint(condition=models.Q(amount__gt=0), name="payment_application_positive")]


# --- allocations (3.5; R34 to R37) --------------------------------------------------------------

class AllocationQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise error("Allocations are never changed; un-allocate with a new row.",
                    "لا تُعدَّل التخصيصات؛ ألغِ التخصيص بسطر جديد.", code="allocation_immutable")

    def delete(self):
        raise error("Allocations are never deleted; un-allocate with a new row.",
                    "لا تُحذف التخصيصات؛ ألغِ التخصيص بسطر جديد.", code="allocation_immutable")


class Allocation(models.Model):
    """Append-only (R36): an un-allocation is a new row with a negative amount and ``reverses`` set."""

    amount = models.DecimalField(bi("Amount", "المبلغ"), max_digits=18, decimal_places=2)
    date = models.DateField(bi("Date", "التاريخ"))
    via_settlement = models.BooleanField(
        bi("Settled in the ledger", "مسوّى في دفتر الأستاذ"), default=False,
        help_text=bi("Made at posting with a settlement line (foreign currency, ledger-fx R18)",
                     "أُنشئ عند الترحيل ببند تسوية (عملة أجنبية)"),
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
        verbose_name=bi("Created by", "أنشأه"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=bi("Created at", "تاريخ الإنشاء"))

    objects = AllocationQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.pk is not None and type(self)._base_manager.filter(pk=self.pk).exists():
            raise error("Allocations are never changed; un-allocate with a new row.",
                        "لا تُعدَّل التخصيصات؛ ألغِ التخصيص بسطر جديد.", code="allocation_immutable")
        if not getattr(self, "_service_write", False):
            raise error("Allocations are made through invoicing.services.", "تتم التخصيصات من خلال خدمات الفوترة.",
                        code="service_only")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise error("Allocations are never deleted; un-allocate with a new row.",
                    "لا تُحذف التخصيصات؛ ألغِ التخصيص بسطر جديد.", code="allocation_immutable")


class ReceivableAllocation(Allocation):
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    receipt = models.ForeignKey(CustomerReceipt, null=True, blank=True, on_delete=models.PROTECT,
                                related_name="allocations", verbose_name=bi("Receipt", "سند القبض"))
    credit_note = models.ForeignKey(SalesDocument, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="allocations_from", verbose_name=bi("Credit note", "الإشعار الدائن"))
    target = models.ForeignKey(SalesDocument, on_delete=models.PROTECT, related_name="allocations_to",
        verbose_name=bi("Allocated to", "مخصص إلى"))
    reverses = models.OneToOneField("self", null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="reversed_by", verbose_name=bi("Reverses", "يعكس"))

    class Meta:
        verbose_name = bi("receivable allocation", "تخصيص ذمم مدينة")
        verbose_name_plural = bi("receivable allocations", "تخصيصات الذمم المدينة")
        constraints = [
            models.CheckConstraint(condition=~models.Q(amount=0), name="receivable_allocation_not_zero"),
            models.CheckConstraint(
                condition=(models.Q(receipt__isnull=False) & models.Q(credit_note__isnull=True))
                | (models.Q(receipt__isnull=True) & models.Q(credit_note__isnull=False)),
                name="receivable_allocation_one_source",
            ),
        ]


class PayableAllocation(Allocation):
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    payment = models.ForeignKey(VendorPayment, null=True, blank=True, on_delete=models.PROTECT,
                                related_name="allocations", verbose_name=bi("Payment", "سند الصرف"))
    vendor_credit_note = models.ForeignKey(PurchaseDocument, null=True, blank=True, on_delete=models.PROTECT,
                                           related_name="allocations_from",
                                               verbose_name=bi("Vendor credit note", "إشعار المورد الدائن"))
    target = models.ForeignKey(PurchaseDocument, on_delete=models.PROTECT, related_name="allocations_to",
        verbose_name=bi("Allocated to", "مخصص إلى"))
    reverses = models.OneToOneField("self", null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="reversed_by", verbose_name=bi("Reverses", "يعكس"))

    class Meta:
        verbose_name = bi("payable allocation", "تخصيص ذمم دائنة")
        verbose_name_plural = bi("payable allocations", "تخصيصات الذمم الدائنة")
        constraints = [
            models.CheckConstraint(condition=~models.Q(amount=0), name="payable_allocation_not_zero"),
            models.CheckConstraint(
                condition=(models.Q(payment__isnull=False) & models.Q(vendor_credit_note__isnull=True))
                | (models.Q(payment__isnull=True) & models.Q(vendor_credit_note__isnull=False)),
                name="payable_allocation_one_source",
            ),
        ]
