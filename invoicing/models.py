"""Invoicing (docs/specs/invoicing.md): customer and vendor sub-ledgers with KSA VAT, slice 1.

Every business model has an ``entity`` FK (CLAUDE.md, D1). Business logic lives in
``invoicing.services``; the guards here only keep issued, posted and reversed
records from being changed outside those services (R13, R24, R36).
Money is ``Decimal``: SAR amounts with 2 decimals, document-currency amounts with up to 3 (D27, R93); rates are
data with effective dates (3.2).
"""
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models

from banking import iban as iban_rules
from banking.crypto import EncryptedTextField
from core.models import Currency, Entity, bi, validate_country_code, validate_vat_number

ZERO = Decimal("0")
# SAR and functional amounts: 2 decimals (CLAUDE.md rule 7).
MONEY = dict(max_digits=18, decimal_places=2, default=ZERO)
# Amounts in the document's transaction currency (D27, R93): up to 3 decimals, never more than the currency's own
# minor units (checked by invoicing.services; ledger-fx R8).
DOC_MONEY_FIELD = dict(max_digits=21, decimal_places=3)
DOC_MONEY = dict(DOC_MONEY_FIELD, default=ZERO)
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


class WhtCategory(models.Model):
    """Withholding tax category for payments to non-residents (D14, R61 to R64; 3.2). Global statutory table.

    Reference values from the WHT guideline §3 p.28 (OQ-8), loaded by migration as **unverified** data: rates,
    categories and effective dates await tax-adviser sign-off (``is_verified`` False). Rates are data with
    effective dates, never constants; a rate change is a new row with the same ``code``.
    """

    code = models.SlugField(bi("Code", "الرمز"), max_length=40)
    name_en = models.CharField(bi("Name (English)", "الاسم (بالإنجليزية)"), max_length=200)
    name_ar = models.CharField(bi("Name (Arabic)", "الاسم (بالعربية)"), max_length=200)
    rate = models.DecimalField(bi("Rate %", "النسبة %"), max_digits=5, decimal_places=2,
                               validators=[MinValueValidator(ZERO)])
    valid_from = models.DateField(bi("Valid from", "ساري من"))
    valid_to = models.DateField(bi("Valid to", "ساري حتى"), null=True, blank=True)
    return_line_code = models.CharField(bi("WHT return line", "بند إقرار الاستقطاع"), max_length=20, blank=True)
    is_verified = models.BooleanField(
        bi("Verified by the tax adviser", "معتمد من المستشار الضريبي"), default=False,
        help_text=bi("Reference data until the tax adviser signs off (OQ-8)", "بيانات مرجعية حتى اعتماد المستشار الضريبي"),
    )
    sharia_prohibited = models.BooleanField(
        bi("Not allowed where Sharia is enforced", "غير مسموح عند تطبيق الضوابط الشرعية"), default=False,
        help_text=bi("Loan charges never arise in a Sharia-enforcing entity (OQ-8, R5, R7)",
                     "لا تنشأ أعباء القروض في كيان يطبق الضوابط الشرعية"),
    )

    class Meta:
        verbose_name = bi("withholding tax category", "فئة ضريبة الاستقطاع")
        verbose_name_plural = bi("withholding tax categories", "فئات ضريبة الاستقطاع")
        ordering = ["code", "valid_from"]
        constraints = [
            models.UniqueConstraint(fields=["code", "valid_from"], name="wht_category_one_per_start"),
            models.CheckConstraint(condition=models.Q(rate__gte=0) & models.Q(rate__lt=100), name="wht_rate_range"),
        ]

    def __str__(self):
        return f"{self.code} {self.rate}%"


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
        PUR = "PUR", bi("Purification transfer or payout", "تحويل أو صرف مبالغ التطهير")

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
    anonymised_at = models.DateTimeField(
        bi("Anonymised at", "تاريخ إخفاء الهوية"), null=True, blank=True, editable=False,
        help_text=bi("R52: personal data replaced by a fixed token after the retention period",
                     "استُبدلت البيانات الشخصية برمز ثابت بعد انتهاء مدة الحفظ"),
    )

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

    def display_name(self, *, personal=False):
        """Code and name. An individual's name is personal data (R51, PDPL): it is shown only when the caller
        has checked ``invoicing.view_personal_data`` in the customer's entity and passes ``personal=True``."""
        if self.customer_kind == self.Kind.INDIVIDUAL and not personal:
            return self.code
        return f"{self.code} — {self.name_ar or self.name_en}"

    def __str__(self):
        """Everywhere an object label is rendered (admin titles, breadcrumbs, history, choices): never an
        individual's name (verification review item 1)."""
        return self.display_name()


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
    Kind = Customer.Kind  # D24 / R91: the same kinds as customers

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="vendors",
        verbose_name=bi("Entity", "الكيان"))
    code = models.CharField(bi("Code", "الرمز"), max_length=20)
    name_en = models.CharField(bi("Name (English)", "الاسم (بالإنجليزية)"), max_length=200, blank=True)
    name_ar = models.CharField(bi("Name (Arabic)", "الاسم (بالعربية)"), max_length=200)
    vendor_kind = models.CharField(
        bi("Vendor kind", "نوع المورد"), max_length=12, choices=Customer.Kind.choices,
        help_text=bi("Individuals are personal data: names masked without the right (R91, R86)",
                     "الأفراد بيانات شخصية: تُخفى أسماؤهم دون الصلاحية"),
    )
    is_vat_registered = models.BooleanField(bi("VAT registered", "مسجل في ضريبة القيمة المضافة"), default=False)
    vat_number = models.CharField(bi("VAT number", "الرقم الضريبي"), max_length=15, blank=True,
                                  validators=[validate_vat_number])
    is_resident = models.BooleanField(bi("Resident in KSA", "مقيم في المملكة"), default=True)
    residence_country = models.CharField(bi("Country of residence", "دولة الإقامة"), max_length=2, blank=True)
    group_entity = models.ForeignKey(Entity, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                                     verbose_name=bi("Group entity", "كيان من المجموعة"))
    bank_iban = models.CharField(bi("IBAN", "رقم الآيبان"), max_length=34, blank=True)
    wht_category = models.ForeignKey(
        WhtCategory, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
        verbose_name=bi("Withholding tax category", "فئة ضريبة الاستقطاع"),
        help_text=bi("Non-resident vendors: default for bill lines; payments need it (R61)",
                     "للموردين غير المقيمين: الافتراضي لبنود الفواتير، وتحتاجه المدفوعات"),
    )
    wht_gross_up = models.BooleanField(
        bi("Entity bears the withholding tax (gross-up)", "تتحمل المنشأة ضريبة الاستقطاع"), default=False,
        help_text=bi("R63: the vendor receives the full amount; method pending tax sign-off",
                     "يستلم المورد المبلغ كاملًا؛ الطريقة بانتظار اعتماد المستشار الضريبي"),
    )
    currency = models.ForeignKey(Currency, to_field="code", on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("Default currency", "العملة الافتراضية"))
    is_active = models.BooleanField(bi("Active", "نشط"), default=True)
    anonymised_at = models.DateTimeField(
        bi("Anonymised at", "تاريخ إخفاء الهوية"), null=True, blank=True, editable=False,
        help_text=bi("R52: an individual vendor's name, address, VAT number and IBAN replaced after the retention of "
                     "its bills, payments and withholding tax records",
                     "استُبدل اسم المورد الفرد وعنوانه ورقمه الضريبي والآيبان بعد انتهاء مدة حفظ فواتيره ومدفوعاته "
                     "وسجلات الاستقطاع"),
    )

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

    def display_name(self, *, personal=False):
        """Code and name; an individual vendor's name only with ``personal=True`` after the caller checked
        ``invoicing.view_personal_data`` in the vendor's entity (R51, R91)."""
        if self.vendor_kind == self.Kind.INDIVIDUAL and not personal:
            return self.code
        return f"{self.code} — {self.name_ar or self.name_en}"

    def __str__(self):
        """Never an individual vendor's name (R86, R91): admin titles, choices and history show the code."""
        return self.display_name()


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
    version = models.PositiveIntegerField(
        bi("Version", "رقم النسخة"), default=0, editable=False,
        help_text=bi("Bumped by every edit, line change, submission and return to draft: an approval or posting "
                     "checked against an older version is refused (access R14, R42)",
                     "يزداد مع كل تعديل أو تغيير بند أو تقديم أو إعادة إلى مسودة: يُرفض الاعتماد المبني على نسخة أقدم"),
    )

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

    def bulk_create(self, objs, *args, **kwargs):
        """bulk_create skips save(): lines are added in bulk only to drafts of their own entity."""
        objs = list(objs)
        document_model = self.model._meta.get_field("document").related_model
        ids = {o.document_id for o in objs}
        docs = dict(document_model._base_manager.filter(pk__in=ids).values_list("pk", "entity_id"))
        statuses = set(document_model._base_manager.filter(pk__in=ids).values_list("status", flat=True))
        if statuses - {"draft"} or any(docs.get(o.document_id) != o.entity_id for o in objs):
            raise error(SERVICE_ONLY.split(" / ")[0], SERVICE_ONLY.split(" / ")[1], code="service_only")
        return super().bulk_create(objs, *args, **kwargs)

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
    unit_price = models.DecimalField(bi("Unit price", "سعر الوحدة"), **DOC_MONEY_FIELD)
    line_discount = models.DecimalField(bi("Line discount", "خصم البند"), **DOC_MONEY)
    net_amount = models.DecimalField(bi("Net amount", "المبلغ الصافي"), **DOC_MONEY)
    vat_category = models.CharField(bi("VAT category", "فئة الضريبة"), max_length=1, choices=VatCategory.choices)
    vat_rate = models.ForeignKey(VatRate, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("VAT rate", "نسبة الضريبة"))
    vat_exemption_reason = models.ForeignKey(VatExemptionReason, null=True, blank=True, on_delete=models.PROTECT,
                                             related_name="+", verbose_name=bi("Exemption reason", "سبب الإعفاء"))
    vat_amount = models.DecimalField(bi("Line VAT (display)", "ضريبة البند (للعرض)"), **DOC_MONEY)

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
        # The type, not the printed title (R47): an entity without a VAT number never issues a "Tax Invoice" (R94).
        INVOICE = "invoice", bi("Invoice", "فاتورة")
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
    approved_version = models.PositiveIntegerField(
        bi("Version approved", "النسخة المعتمدة"), null=True, blank=True, editable=False,
        help_text=bi("The version the approval was checked against; the posting on clearance needs the same (R87)",
                     "النسخة التي فُحص الاعتماد عليها؛ لا يُرحَّل المستند عند اعتماد الهيئة إلا بالنسخة نفسها"),
    )
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="documents",
        verbose_name=bi("Customer", "العميل"))
    # R60, R73 (billing.md R52 to R56): the billing period or correction a system-created document comes from.
    billing_item = models.ForeignKey(
        "billing.BillingPeriod", null=True, blank=True, on_delete=models.PROTECT, related_name="+",
        verbose_name=bi("Billing period", "فترة الفوترة"),
        help_text=bi("Set by create_from_billing; one non-rejected document per period (R60)",
                     "يُحدَّد عند الإنشاء من الفوترة؛ مستند واحد غير مرفوض لكل فترة"),
    )
    billing_adjustment = models.ForeignKey(
        "billing.BillingAdjustment", null=True, blank=True, on_delete=models.PROTECT, related_name="+",
        verbose_name=bi("Billing correction", "تصحيح الفوترة"),
        help_text=bi("Set by create_adjustment_from_billing; its lines are read-only for every user (R73)",
                     "يُحدَّد عند إنشاء الإشعار من تصحيح الفوترة؛ بنوده للقراءة فقط لكل المستخدمين"),
    )
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
    fx_exchange_rate = models.ForeignKey(
        "ledger.ExchangeRate", null=True, blank=True, on_delete=models.PROTECT, related_name="+",
        verbose_name=bi("Rate row used", "سجل السعر المستخدم"),
        help_text=bi("Pinned at approval: the document books at this rate (review item 2)",
                     "يُثبَّت عند الاعتماد: يُرحَّل المستند بهذا السعر"),
    )
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
    document_discount = models.DecimalField(bi("Document discount", "خصم المستند"), **DOC_MONEY)
    total_net = models.DecimalField(bi("Total net", "الإجمالي الصافي"), **DOC_MONEY)
    total_vat = models.DecimalField(bi("Total VAT", "إجمالي الضريبة"), **DOC_MONEY)
    total_gross = models.DecimalField(bi("Total gross", "الإجمالي شامل الضريبة"), **DOC_MONEY)
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
    anonymised_at = models.DateTimeField(
        bi("Buyer data anonymised at", "تاريخ إخفاء هوية المشتري"), null=True, blank=True, editable=False,
        help_text=bi("R52: the individual buyer's snapshot replaced by a fixed token after retention_until; amounts, "
                     "VAT, number and ZATCA data kept", "استُبدلت بيانات المشتري الفرد برمز ثابت بعد انتهاء مدة الحفظ"),
    )

    class Meta:
        verbose_name = bi("sales document", "مستند مبيعات")
        verbose_name_plural = bi("sales documents", "مستندات المبيعات")
        ordering = ["entity", "-issue_date", "number"]
        constraints = [
            models.UniqueConstraint(fields=["entity", "number"], condition=~models.Q(number=""),
                                    name="sales_number_per_entity"),
            # R60 / billing R52: at most one non-rejected document per billing period; a ZATCA rejection frees
            # the period for a new document under a new number (billing R12).
            models.UniqueConstraint(fields=["billing_item"],
                                    condition=models.Q(billing_item__isnull=False) & ~models.Q(status="rejected"),
                                    name="one_live_document_per_billing_period"),
            # R73: one non-terminal correction note per billing adjustment (deleted drafts are gone).
            models.UniqueConstraint(fields=["billing_adjustment"],
                                    condition=models.Q(billing_adjustment__isnull=False) & ~models.Q(status="rejected"),
                                    name="one_live_note_per_billing_adjustment"),
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
    def from_billing(self):
        """Created by billing (R60, R73): its lines and amounts are read-only for every user (billing R56)."""
        return self.billing_item_id is not None or self.billing_adjustment_id is not None

    @property
    def zatca_type_code(self):
        return self.ZATCA_TYPE_CODES[self.doc_type]

    @property
    def seller_vat_registered(self):
        """R94: the seller has a VAT number (the frozen snapshot once issued, the entity's while a draft)."""
        if self.status in (self.Status.ISSUED, self.Status.FINAL, self.Status.REJECTED):
            return bool((self.seller_vat_number or "").strip())
        return bool((self.entity.vat_number or "").strip())

    @property
    def title_en(self):
        """R47: printed title (rendering is not built in slice 1; the data is). R94: an entity without a VAT number
        is never titled "Tax Invoice"; its title waits for tax sign-off (OQ-59), so a neutral "Invoice" is shown."""
        if self.doc_type == self.DocType.INVOICE:
            if not self.seller_vat_registered:
                return "Invoice"
            return "Tax Invoice" if self.zatca_profile == self.Profile.STANDARD else "Simplified Tax Invoice"
        return "Credit Note" if self.doc_type == self.DocType.CREDIT_NOTE else "Debit Note"

    @property
    def title_ar(self):
        if self.doc_type == self.DocType.INVOICE:
            if not self.seller_vat_registered:
                return "فاتورة"
            return "فاتورة ضريبية" if self.zatca_profile == self.Profile.STANDARD else "فاتورة ضريبية مبسطة"
        return "إشعار دائن" if self.doc_type == self.DocType.CREDIT_NOTE else "إشعار مدين"

    def __str__(self):
        return f"{self.number or 'DRAFT'} {self.get_doc_type_display()} ({self.status})"


BILLING_LINES_EN = ("The lines of a document created from billing are read-only; correct the contract in billing "
                    "instead (R73, billing R56).")
BILLING_LINES_AR = "بنود المستند المنشأ من الفوترة للقراءة فقط؛ صحّح العقد في وحدة الفوترة بدلًا من ذلك."

# R73 / billing R56 (review finding 5): lines of a document linked to billing are written only inside
# ``billing_line_writes()`` (the billing hooks, the approval's re-pricing and the discard of a correction draft).
# The flag is per context in Python and transaction-local (``set_config(..., true)``) in PostgreSQL, where the
# line-lock trigger refuses every other insert, update or delete.
_BILLING_LINE_WRITES = ContextVar("invoicing_billing_line_writes", default=False)
BILLING_LINES_SETTING = "invoicing.billing_lines"


@contextmanager
def billing_line_writes():
    """Allow writes to billing-linked sales lines for the enclosed block of the current transaction only."""
    from django.db import connection

    if not connection.in_atomic_block:
        raise RuntimeError("billing_line_writes() is used inside a transaction (SET LOCAL).")
    token = _BILLING_LINE_WRITES.set(True)
    postgres = connection.vendor == "postgresql"
    if postgres:
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config(%s, 'on', true)", [BILLING_LINES_SETTING])
    try:
        yield
    finally:
        _BILLING_LINE_WRITES.reset(token)
        if postgres and not connection.needs_rollback:
            from django.db import DatabaseError

            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT set_config(%s, 'off', true)", [BILLING_LINES_SETTING])
            except DatabaseError:  # the transaction is already aborted: it rolls back, setting included
                pass


def _billing_lines_refused():
    raise error(BILLING_LINES_EN, BILLING_LINES_AR, code="billing_lines_read_only")


_BILLING_LINK = models.Q(billing_item__isnull=False) | models.Q(billing_adjustment__isnull=False)


class SalesLineQuerySet(GuardedLineQuerySet):
    def _touches_billing(self):
        return self.filter(models.Q(document__billing_item__isnull=False)
                           | models.Q(document__billing_adjustment__isnull=False)).exists()

    def update(self, **kwargs):
        if not _BILLING_LINE_WRITES.get() and self._touches_billing():
            _billing_lines_refused()
        return super().update(**kwargs)

    def delete(self):
        if not _BILLING_LINE_WRITES.get() and self._touches_billing():
            _billing_lines_refused()
        return super().delete()

    def bulk_create(self, objs, *args, **kwargs):
        objs = list(objs)
        if not _BILLING_LINE_WRITES.get() and SalesDocument._base_manager.filter(
                _BILLING_LINK, pk__in={o.document_id for o in objs}).exists():
            _billing_lines_refused()
        return super().bulk_create(objs, *args, **kwargs)



class SalesDocumentLine(LineAmounts, GuardedLine):
    objects = SalesLineQuerySet.as_manager()

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    document = models.ForeignKey(SalesDocument, on_delete=models.CASCADE, related_name="lines",
        verbose_name=bi("Document", "المستند"))
    income_type = models.ForeignKey(IncomeType, on_delete=models.PROTECT, related_name="+",
                                    verbose_name=bi("Income type", "نوع الإيراد"))
    income_account = models.ForeignKey("ledger.Account", on_delete=models.PROTECT, related_name="+",
                                       verbose_name=bi("Income account", "حساب الإيراد"))
    # BT-120 for VATEX-SA-OOS: "free text, to be provided by the taxpayer on case to case basis" (row 1c).
    exemption_text_en = models.CharField(bi("Exemption reason text (English)", "نص سبب الإعفاء (بالإنجليزية)"),
                                         max_length=300, blank=True)
    exemption_text_ar = models.CharField(bi("Exemption reason text (Arabic)", "نص سبب الإعفاء (بالعربية)"),
                                         max_length=300, blank=True)

    def _billing_locked(self):
        return SalesDocument._base_manager.filter(_BILLING_LINK, pk=self.document_id).exists()

    def save(self, *args, **kwargs):
        # R73 / billing R56: lines of a billing document are written only inside billing_line_writes() (the
        # billing hooks and the approval's re-pricing); nobody else adds, edits or deletes them.
        if not _BILLING_LINE_WRITES.get() and self._billing_locked():
            _billing_lines_refused()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if not _BILLING_LINE_WRITES.get() and self._billing_locked():
            _billing_lines_refused()
        return super().delete(*args, **kwargs)

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
    fx_exchange_rate = models.ForeignKey(
        "ledger.ExchangeRate", null=True, blank=True, on_delete=models.PROTECT, related_name="+",
        verbose_name=bi("Rate row used", "سجل السعر المستخدم"),
        help_text=bi("Pinned at approval: the document books at this rate (review item 2)",
                     "يُثبَّت عند الاعتماد: يُرحَّل المستند بهذا السعر"),
    )
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.DRAFT)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+", verbose_name=bi("Submitted by", "قدّمه"))
    submitted_at = models.DateTimeField(bi("Submitted at", "تاريخ التقديم"), null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=bi("Approved by", "اعتمده"))
    approved_at = models.DateTimeField(bi("Approved at", "تاريخ الاعتماد"), null=True, blank=True)
    original_document = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT,
                                          related_name="notes", verbose_name=bi("Original bill", "الفاتورة الأصلية"))
    vendor_total_vat = models.DecimalField(bi("VAT on the vendor document", "الضريبة في مستند المورد"), **DOC_MONEY)
    vendor_total_gross = models.DecimalField(bi("Total on the vendor document", "الإجمالي في مستند المورد"),
                                             **DOC_MONEY)
    reverse_charge = models.BooleanField(bi("Reverse charge", "احتساب عكسي"), default=False)
    total_net = models.DecimalField(bi("Total net", "الإجمالي الصافي"), **DOC_MONEY)
    total_vat = models.DecimalField(bi("VAT charged by the vendor", "الضريبة التي فرضها المورد"), **DOC_MONEY)
    total_gross = models.DecimalField(bi("Total payable", "إجمالي المستحق"), **DOC_MONEY)
    reverse_charge_vat = models.DecimalField(bi("Reverse-charge VAT", "ضريبة الاحتساب العكسي"), **DOC_MONEY)
    vat_breakdown = models.JSONField(default=list, blank=True, verbose_name=bi("VAT per category", "الضريبة حسب الفئة"))
    requires_purification = models.BooleanField(default=False,
        verbose_name=bi("Requires purification", "يتطلب التطهير"))
    is_intercompany = models.BooleanField(bi("Intercompany", "بين شركات المجموعة"), default=False)
    attachment = models.FileField(bi("Vendor document", "مستند المورد"), upload_to="invoicing/purchase/%Y/", blank=True)
    anonymised_at = models.DateTimeField(
        bi("Vendor data anonymised at", "تاريخ إخفاء هوية المورد"), null=True, blank=True, editable=False,
        help_text=bi("R52: an individual vendor's VAT number removed after retention; amounts, VAT and numbers kept",
                     "حُذف الرقم الضريبي للمورد الفرد بعد انتهاء مدة الحفظ؛ تبقى المبالغ والضريبة والأرقام"),
    )
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
    wht_category = models.ForeignKey(
        WhtCategory, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
        verbose_name=bi("Withholding tax category", "فئة ضريبة الاستقطاع"),
        help_text=bi("Required to pay a non-resident vendor (R61); defaults to the vendor's",
                     "مطلوبة للدفع لمورد غير مقيم؛ الافتراضي فئة المورد"),
    )

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
    amount = models.DecimalField(bi("Amount", "المبلغ"), **DOC_MONEY_FIELD)
    currency = models.ForeignKey(Currency, to_field="code", on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("Currency", "العملة"))
    fx_rate = models.DecimalField(bi("Rate to functional currency", "سعر الصرف إلى العملة الوظيفية"), **RATE)
    fx_rate_date = models.DateField(bi("Rate date", "تاريخ السعر"), null=True, blank=True)
    fx_rate_source = models.CharField(bi("Rate source", "مصدر السعر"), max_length=10, blank=True)
    fx_exchange_rate = models.ForeignKey(
        "ledger.ExchangeRate", null=True, blank=True, on_delete=models.PROTECT, related_name="+",
        verbose_name=bi("Rate row used", "سجل السعر المستخدم"),
        help_text=bi("Pinned at approval: the document books at this rate (review item 2)",
                     "يُثبَّت عند الاعتماد: يُرحَّل المستند بهذا السعر"),
    )
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
    bank_charge = models.DecimalField(bi("Bank charge", "رسوم بنكية"), **DOC_MONEY)
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
    wht_amount = models.DecimalField(bi("Withholding tax", "ضريبة الاستقطاع"), **DOC_MONEY)
    wht_gross_up = models.BooleanField(bi("Withholding tax borne by the entity", "الضريبة تتحملها المنشأة"),
                                       default=False)

    @property
    def settled_amount(self):
        """AP settled by the payment (3.5, R62, R63): cash plus withheld tax, unless the entity bears the tax."""
        return self.amount + (ZERO if self.wht_gross_up else self.wht_amount)

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


TREATY_DOCUMENT_TYPES = {".pdf": (b"%PDF-",), ".png": (b"\x89PNG\r\n\x1a\n",), ".jpg": (b"\xff\xd8\xff",),
                         ".jpeg": (b"\xff\xd8\xff",)}
TREATY_DOCUMENT_MAX_BYTES = 10 * 1024 * 1024


def validate_treaty_document(upload):
    """Review finding 4: a treaty certificate scan is a PDF, PNG or JPEG of at most 10 MB, by name and content."""
    if not upload:
        return
    import os

    ext = os.path.splitext(getattr(upload, "name", "") or "")[1].lower()
    if ext not in TREATY_DOCUMENT_TYPES:
        raise error("The certificate is uploaded as a PDF, PNG or JPG file.",
                    "تُرفع الشهادة بصيغة PDF أو PNG أو JPG.", code="certificate_file_type")
    size = getattr(upload, "size", None)
    if size is None or size > TREATY_DOCUMENT_MAX_BYTES:
        raise error("The certificate file is at most 10 MB.", "حجم ملف الشهادة 10 ميغابايت كحد أقصى.",
                    code="certificate_file_size")
    position = upload.tell() if hasattr(upload, "tell") else None
    upload.seek(0)
    head = upload.read(8)
    if position is not None:
        upload.seek(position)
    if not any(head.startswith(magic) for magic in TREATY_DOCUMENT_TYPES[ext]):
        raise error("The certificate file content is not the PDF, PNG or JPG its name says.",
                    "محتوى ملف الشهادة لا يطابق صيغة PDF أو PNG أو JPG المذكورة في اسمه.",
                    code="certificate_file_type")


class TreatyCertificateQuerySet(models.QuerySet):
    """Bulk writes skip save(): certificates change only through invoicing.services (review finding 4)."""

    def update(self, **kwargs):
        raise error("Treaty certificates are changed only through invoicing.services; a verified one never.",
                    "تُعدَّل شهادات الاتفاقيات من خلال خدمات الفوترة فقط، ولا تُعدَّل بعد التحقق منها.",
                    code="service_only")

    def delete(self):
        raise error("Treaty certificates are never deleted.", "لا تُحذف شهادات الاتفاقيات.", code="certificate_kept")


class TreatyCertificate(models.Model):
    """Treaty relief evidence for a non-resident vendor (R62; 3.2). Written only by invoicing.services, which
    records who entered and who last changed it; a second person (neither of them) verifies it, and from then on
    it is never edited (a new certificate replaces it). ``treaty_rate`` 0.00 = exempt; ``wht_category`` null = all
    categories."""

    objects = TreatyCertificateQuerySet.as_manager()

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="treaty_certificates",
                               verbose_name=bi("Vendor", "المورد"))
    country = models.CharField(bi("Treaty country", "دولة الاتفاقية"), max_length=2, validators=[validate_country_code])
    treaty_rate = models.DecimalField(bi("Treaty rate %", "نسبة الاتفاقية %"), max_digits=5, decimal_places=2,
                                      validators=[MinValueValidator(ZERO)])
    wht_category = models.ForeignKey(WhtCategory, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                                     verbose_name=bi("Category (blank = all)", "الفئة (فارغ = الكل)"))
    valid_from = models.DateField(bi("Valid from", "ساري من"))
    valid_to = models.DateField(bi("Valid to", "ساري حتى"))
    document = models.FileField(bi("Certificate", "الشهادة"), upload_to="invoicing/treaty/%Y/", blank=True,
                                validators=[validate_treaty_document],
                                help_text=bi("PDF, PNG or JPG, at most 10 MB", "PDF أو PNG أو JPG، بحد أقصى 10 ميغابايت"))
    received_on = models.DateField(bi("Received on", "تاريخ الاستلام"))
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
                                   verbose_name=bi("Entered by", "أدخله"))
    created_at = models.DateTimeField(bi("Entered at", "تاريخ الإدخال"), auto_now_add=True)
    modified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=bi("Last changed by", "آخر من عدّله"))
    modified_at = models.DateTimeField(bi("Last changed at", "تاريخ آخر تعديل"), null=True, blank=True)
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=bi("Verified by", "تحقق منه"))
    verified_at = models.DateTimeField(bi("Verified at", "تاريخ التحقق"), null=True, blank=True)

    class Meta:
        verbose_name = bi("treaty certificate", "شهادة اتفاقية ضريبية")
        verbose_name_plural = bi("treaty certificates", "شهادات الاتفاقيات الضريبية")
        constraints = [
            models.CheckConstraint(condition=models.Q(treaty_rate__gte=0) & models.Q(treaty_rate__lt=100),
                                   name="treaty_rate_range"),
            models.CheckConstraint(condition=models.Q(valid_to__gte=models.F("valid_from")), name="treaty_dates"),
        ]

    def save(self, *args, **kwargs):
        allowed, self._service_write = getattr(self, "_service_write", False), False
        if self.pk:
            stored = type(self)._base_manager.filter(pk=self.pk).values("verified_at").first()
            if stored and stored["verified_at"] is not None:
                raise error("A verified treaty certificate is never edited; enter a new one.",
                            "لا تُعدَّل شهادة الاتفاقية بعد التحقق منها؛ أدخل شهادة جديدة.", code="certificate_verified")
        if not allowed:  # review finding 4: an edit always records who made it (update_treaty_certificate)
            raise error("Treaty certificates are entered and changed only through invoicing.services.",
                        "تُدخل شهادات الاتفاقيات وتُعدَّل من خلال خدمات الفوترة فقط.", code="service_only")
        if self.entity_id != self.vendor.entity_id:
            raise error("A certificate belongs to its vendor's entity.", "تتبع الشهادة كيان المورد.",
                        code="entity_mismatch")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise error("Treaty certificates are never deleted.", "لا تُحذف شهادات الاتفاقيات.", code="certificate_kept")

    def __str__(self):
        return f"{self.vendor.code} {self.country} {self.treaty_rate}% ({self.valid_from}–{self.valid_to})"


class WhtQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise error("Withholding tax deductions are never changed.", "لا تُعدَّل استقطاعات الضريبة.", code="wht_immutable")

    def delete(self):
        raise error("Withholding tax deductions are never deleted.", "لا تُحذف استقطاعات الضريبة.", code="wht_immutable")


class WhtDeduction(models.Model):
    """One per payment x bill x category (R62 to R64; 3.5). Immutable: written when the payment posts and kept
    when it is reversed (the reversal shows in the return data of its month). Source of the WHT return data."""

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    payment = models.ForeignKey(VendorPayment, on_delete=models.PROTECT, related_name="wht_deductions",
                                verbose_name=bi("Payment", "سند الصرف"))
    bill = models.ForeignKey(PurchaseDocument, on_delete=models.PROTECT, related_name="+",
                             verbose_name=bi("Bill", "الفاتورة"))
    wht_category = models.ForeignKey(WhtCategory, on_delete=models.PROTECT, related_name="+",
                                     verbose_name=bi("Category", "الفئة"))
    base_amount = models.DecimalField(bi("Base", "الوعاء"), **DOC_MONEY_FIELD)
    base_amount_sar = models.DecimalField(bi("Base in SAR", "الوعاء بالريال"), max_digits=18, decimal_places=2)
    rate_applied = models.DecimalField(bi("Rate applied %", "النسبة المطبقة %"), max_digits=5, decimal_places=2)
    treaty_certificate = models.ForeignKey(TreatyCertificate, null=True, blank=True, on_delete=models.PROTECT,
                                           related_name="+", verbose_name=bi("Treaty certificate", "شهادة الاتفاقية"))
    wht_amount = models.DecimalField(bi("Withholding tax", "ضريبة الاستقطاع"), **DOC_MONEY_FIELD)
    wht_amount_sar = models.DecimalField(bi("Withholding tax in SAR", "ضريبة الاستقطاع بالريال"), max_digits=18,
                                         decimal_places=2)
    gross_up = models.BooleanField(bi("Borne by the entity", "تتحملها المنشأة"), default=False)
    return_month = models.DateField(bi("Return month", "شهر الإقرار"))
    created_at = models.DateTimeField(bi("Created at", "تاريخ الإنشاء"), auto_now_add=True)

    objects = WhtQuerySet.as_manager()

    class Meta:
        verbose_name = bi("withholding tax deduction", "استقطاع ضريبي")
        verbose_name_plural = bi("withholding tax deductions", "استقطاعات ضريبية")
        ordering = ["entity", "return_month", "pk"]
        constraints = [models.UniqueConstraint(fields=["payment", "bill", "wht_category"], name="one_wht_row_per_category")]

    def save(self, *args, **kwargs):
        if self.pk is not None and type(self)._base_manager.filter(pk=self.pk).exists():
            raise error("Withholding tax deductions are never changed.", "لا تُعدَّل استقطاعات الضريبة.",
                        code="wht_immutable")
        if not getattr(self, "_service_write", False):
            raise error("Withholding tax is computed by invoicing.services when the payment posts.",
                        "تحتسب خدمات الفوترة ضريبة الاستقطاع عند ترحيل الدفعة.", code="service_only")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise error("Withholding tax deductions are never deleted.", "لا تُحذف استقطاعات الضريبة.", code="wht_immutable")


class PurificationQuerySet(GuardedQuerySet):
    """D31b review fix 6: ``bulk_create`` skips save(): only drafts without number or entry are added in bulk."""

    def bulk_create(self, objs, *args, **kwargs):
        objs = list(objs)
        if any(o.status != "draft" or o.number or o.journal_entry_id or o.reversal_entry_id or o.posted_by_id
               for o in objs):
            raise error(STATUS_ONLY_BY_SERVICES.split(" / ")[0], STATUS_ONLY_BY_SERVICES.split(" / ")[1],
                        code="service_only")
        return super().bulk_create(objs, *args, **kwargs)


class PurificationTransfer(Tracked, Guarded):
    """Purification of non-compliant income (R54, E14, E15; CLAUDE.md rule 5).

    ``transfer`` (E14): Dr 4900 / Cr 2600 for flagged income still to purify (``ledger.purification_amount``).
    ``funding`` (D31b): Dr 1140 / Cr operating bank: the cash behind 2600 moves into the entity's designated
    purification account (``banking.BankAccount.is_purification_account``), up to what was transferred and not yet
    funded. ``payout`` (E15, D31b): Dr 2600 / Cr 1140 from the purification account to one approved, active
    :class:`CharityRecipient`; it produces a :class:`PurificationInstruction` for the bank. Recorded by a person and
    posted by a **second** person (the ledger refuses the recorder): transfers and fundings with
    ``invoicing.post_purification_transfer``, payouts with ``invoicing.approve_purification_payout`` (CFO). Never by
    an agent. Amount, timing and the recipient list's Sharia sign-off stay OQ-30. Amounts are in SAR (functional).
    """

    class Kind(models.TextChoices):
        TRANSFER = "transfer", bi("Transfer to charity payable (E14)", "تحويل إلى مستحقات التطهير")
        FUNDING = "funding", bi("Funding of the purification account (D31b)", "تمويل حساب التطهير")
        PAYOUT = "payout", bi("Payout to charity (E15)", "صرف للجهة الخيرية")

    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        POSTED = "posted", bi("Posted", "مرحّل")
        REVERSED = "reversed", bi("Reversed", "معكوس")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    kind = models.CharField(bi("Kind", "النوع"), max_length=8, choices=Kind.choices)
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.DRAFT)
    number = models.CharField(bi("Number", "الرقم"), max_length=40, blank=True)
    date = models.DateField(bi("Date", "التاريخ"))
    amount = models.DecimalField(bi("Amount", "المبلغ"), max_digits=18, decimal_places=2)
    bank_account = models.ForeignKey(
        "ledger.Account", null=True, blank=True, on_delete=models.PROTECT, related_name="+",
        verbose_name=bi("Bank GL account (funding: paid from; payout: 1140)",
                        "حساب الأستاذ البنكي (التمويل: المصدر؛ الصرف: 1140)"))
    purification_account = models.ForeignKey(
        "banking.BankAccount", null=True, blank=True, on_delete=models.PROTECT, related_name="+",
        verbose_name=bi("Purification account (D31b)", "حساب التطهير"))
    recipient = models.ForeignKey(
        "invoicing.CharityRecipient", null=True, blank=True, on_delete=models.PROTECT, related_name="payouts",
        verbose_name=bi("Charity recipient (payout)", "الجهة الخيرية المستفيدة (الصرف)"))
    recipient_en = models.CharField(bi("Recipient (English)", "الجهة المستفيدة (بالإنجليزية)"), max_length=200,
                                    blank=True)
    recipient_ar = models.CharField(bi("Recipient (Arabic)", "الجهة المستفيدة (بالعربية)"), max_length=200, blank=True)
    reference_en = models.CharField(bi("Reference (English)", "المرجع (بالإنجليزية)"), max_length=200, blank=True)
    reference_ar = models.CharField(bi("Reference (Arabic)", "المرجع (بالعربية)"), max_length=200, blank=True)
    posted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                  related_name="+", verbose_name=bi("Posted by", "رحّله"))
    posted_at = models.DateTimeField(bi("Posted at", "تاريخ الترحيل"), null=True, blank=True)
    journal_entry = models.ForeignKey("ledger.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                      related_name="+", verbose_name=bi("Journal entry", "القيد اليومي"))
    reversal_entry = models.ForeignKey("ledger.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                       related_name="+", verbose_name=bi("Reversal entry", "قيد العكس"))
    reversed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=bi("Reversed by", "عكسه"))
    reversed_at = models.DateTimeField(bi("Reversed at", "تاريخ العكس"), null=True, blank=True)
    reversal_reason = models.CharField(bi("Reversal reason", "سبب العكس"), max_length=300, blank=True)

    objects = PurificationQuerySet.as_manager()

    class Meta:
        base_manager_name = "objects"  # review fix 6: _base_manager writes go through the guarded queryset
        verbose_name = bi("purification transfer", "تحويل مبالغ التطهير")
        verbose_name_plural = bi("purification transfers", "تحويلات مبالغ التطهير")
        ordering = ["entity", "-date", "number"]
        constraints = [
            models.UniqueConstraint(fields=["entity", "number"], condition=~models.Q(number=""),
                                    name="purification_number_per_entity"),
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="purification_amount_positive"),
        ]

    def __str__(self):
        return f"{self.number or 'DRAFT'} {self.get_kind_display()} {self.amount} ({self.status})"


class _ServiceOnlyQuerySet(models.QuerySet):
    """Bulk writes skip save(): these rows change one by one through invoicing.services (D31b)."""

    def update(self, **kwargs):
        raise error(SERVICE_ONLY.split(" / ")[0], SERVICE_ONLY.split(" / ")[1], code="service_only")

    def delete(self):
        raise error(SERVICE_ONLY.split(" / ")[0], SERVICE_ONLY.split(" / ")[1], code="service_only")

    def bulk_update(self, objs, fields, *args, **kwargs):
        raise error(SERVICE_ONLY.split(" / ")[0], SERVICE_ONLY.split(" / ")[1], code="service_only")

    def bulk_create(self, objs, *args, **kwargs):  # skips save(): never pre-approved or pre-cancelled rows
        raise error(SERVICE_ONLY.split(" / ")[0], SERVICE_ONLY.split(" / ")[1], code="service_only")


def _stored_values(obj):
    if obj.pk is None:
        return None
    return type(obj)._base_manager.filter(pk=obj.pk).values(*[f.attname for f in obj._meta.concrete_fields]).first()


class CharityRecipient(models.Model):
    """An approved charity recipient of purification payouts (R54 amendment D31b), per entity.

    Added by the accountant (``invoicing.manage_charity_recipient``) and approved by a Managing Partner who did not
    add it (``invoicing.approve_charity_recipient``, A + S!). Only approved, active recipients are paid. The Saudi
    IBAN (``SA`` + 22 digits, valid mod-97) is stored encrypted with a keyed hash and its last four characters
    (banking R4); lists show it masked, unmasked views need ``banking.view_iban`` and are logged. Once approved, a
    recipient never changes; it can only be deactivated (then it is never used again). Sharia board sign-off of the
    list stays OQ-30. PostgreSQL trigger: migration 0022.
    """

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    name_en = models.CharField(bi("Name (English)", "الاسم (بالإنجليزية)"), max_length=200)
    name_ar = models.CharField(bi("Name (Arabic)", "الاسم (بالعربية)"), max_length=200)
    bank = models.ForeignKey("banking.Bank", on_delete=models.PROTECT, related_name="+",
                             verbose_name=bi("Bank", "البنك"))
    iban = EncryptedTextField(bi("IBAN", "الآيبان"), plain_max_length=34)
    iban_hash = models.CharField(bi("IBAN keyed hash", "البصمة المفتاحية للآيبان"), max_length=64, editable=False)
    iban_last4 = models.CharField(bi("IBAN last 4", "آخر 4 خانات من الآيبان"), max_length=4, editable=False)
    platform_reference = models.CharField(
        bi("Platform reference", "مرجع المنصة"), max_length=100, blank=True,
        help_text=bi("Optional, e.g. the Ihsan national platform reference", "اختياري، مثل مرجع منصة إحسان"))
    is_active = models.BooleanField(bi("Active", "نشط"), default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
                                   verbose_name=bi("Added by", "أضافه"))
    created_at = models.DateTimeField(bi("Added at", "تاريخ الإضافة"), auto_now_add=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=bi("Approved by", "اعتمده"))
    approved_at = models.DateTimeField(bi("Approved at", "تاريخ الاعتماد"), null=True, blank=True)
    deactivated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                       related_name="+", verbose_name=bi("Deactivated by", "أوقفه"))
    deactivated_at = models.DateTimeField(bi("Deactivated at", "تاريخ الإيقاف"), null=True, blank=True)
    deactivation_reason = models.CharField(bi("Deactivation reason", "سبب الإيقاف"), max_length=300, blank=True)

    objects = _ServiceOnlyQuerySet.as_manager()

    APPROVAL_FIELDS = frozenset({"approved_by_id", "approved_at"})
    DEACTIVATION_FIELDS = frozenset({"is_active", "deactivated_by_id", "deactivated_at", "deactivation_reason"})

    class Meta:
        base_manager_name = "objects"  # review fix 6: _base_manager.update/bulk writes are refused too
        verbose_name = bi("charity recipient", "جهة خيرية مستفيدة")
        verbose_name_plural = bi("charity recipients", "الجهات الخيرية المستفيدة")
        ordering = ["entity", "name_en"]
        constraints = [
            models.UniqueConstraint(fields=["entity", "iban_hash"], condition=models.Q(is_active=True),
                                    name="charity_recipient_iban_once_per_entity"),
            models.CheckConstraint(condition=models.Q(approved_by__isnull=True, approved_at__isnull=True)
                                   | models.Q(approved_by__isnull=False, approved_at__isnull=False),
                                   name="charity_recipient_approval_complete"),
            models.CheckConstraint(condition=~models.Q(approved_by=models.F("created_by")),
                                   name="charity_recipient_approver_not_creator"),
        ]

    @property
    def is_approved(self):
        return self.approved_by_id is not None

    @property
    def masked_iban(self):
        return iban_rules.masked("SA", self.iban_last4)

    def __str__(self):  # masked IBAN only (banking R4)
        return f"{self.name_en} / {self.name_ar} {self.masked_iban}"

    def save(self, *args, **kwargs):
        allowed, self._service_write = getattr(self, "_service_write", False), False
        if not allowed:
            raise error("Charity recipients change only through invoicing.services.",
                        "تتغير الجهات الخيرية المستفيدة من خلال خدمات الفوترة فقط.", code="service_only")
        stored = _stored_values(self)
        if stored is None and (self.approved_by_id is not None or self.approved_at is not None or not self.is_active
                               or self.deactivated_by_id is not None or self.deactivated_at is not None):
            raise error("A charity recipient is added unapproved and active; a second person approves it (D31b).",
                        "تُضاف الجهة الخيرية المستفيدة غير معتمدة ونشطة؛ ويعتمدها شخص آخر.", code="recipient_immutable")
        if stored is not None:
            changed = {name for name, value in stored.items() if getattr(self, name) != value}
            approving = (stored["approved_by_id"] is None and changed <= self.APPROVAL_FIELDS
                         and self.approved_by_id is not None and stored["is_active"])
            deactivating = stored["is_active"] and not self.is_active and changed <= self.DEACTIVATION_FIELDS
            if changed and not (approving or deactivating):
                raise error("A charity recipient never changes; it is approved once or deactivated (D31b).",
                            "لا تتغير الجهة الخيرية المستفيدة؛ تُعتمد مرة واحدة أو تُوقف.", code="recipient_immutable")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise error("Charity recipients are never deleted; deactivate them.", "لا تُحذف الجهات الخيرية المستفيدة؛ أوقفها.",
                    code="recipient_immutable")


class PurificationInstruction(models.Model):
    """The bank instruction a posted purification payout produces (R54 amendment D31b): pay ``amount`` SAR from the
    purification account to the recipient's IBAN with ``reference``. For the banking module and a future bank file
    (banking OQ-2); nothing here connects to a bank. ``pending`` until the payout is reversed (``cancelled``). The
    recipient's name, bank and IBAN are copied at posting; the IBAN is encrypted and shown masked (banking R4).
    PostgreSQL trigger: migration 0022.
    """

    class Status(models.TextChoices):
        PENDING = "pending", bi("Pending (to send to the bank)", "معلقة (للإرسال إلى البنك)")
        CANCELLED = "cancelled", bi("Cancelled (payout reversed)", "ملغاة (عُكس الصرف)")
        # Placeholders for the bank file (banking OQ-2, invoicing OQ-64(d)): nothing sets them yet. Once an
        # instruction is exported its payout is never reversed.
        EXPORTED = "exported", bi("Exported to a bank file", "مُصدَّرة في ملف بنكي")
        SENT = "sent", bi("Sent to the bank", "مُرسلة إلى البنك")

    SENT_STATUSES = frozenset({Status.EXPORTED, Status.SENT})

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    payout = models.OneToOneField(PurificationTransfer, on_delete=models.PROTECT, related_name="bank_instruction",
                                  verbose_name=bi("Payout", "الصرف"))
    from_account = models.ForeignKey("banking.BankAccount", on_delete=models.PROTECT, related_name="+",
                                     verbose_name=bi("From (purification account)", "من (حساب التطهير)"))
    from_iban_last4 = models.CharField(bi("From IBAN last 4", "آخر 4 خانات من آيبان حساب التطهير"), max_length=4,
                                       default="", editable=False)
    from_iban_hash = models.CharField(bi("From IBAN keyed hash", "البصمة المفتاحية لآيبان حساب التطهير"),
                                      max_length=64, default="", editable=False)
    recipient = models.ForeignKey(CharityRecipient, on_delete=models.PROTECT, related_name="instructions",
                                  verbose_name=bi("Recipient", "المستفيد"))
    beneficiary_name_en = models.CharField(bi("Beneficiary (English)", "المستفيد (بالإنجليزية)"), max_length=200)
    beneficiary_name_ar = models.CharField(bi("Beneficiary (Arabic)", "المستفيد (بالعربية)"), max_length=200)
    beneficiary_bank = models.ForeignKey("banking.Bank", on_delete=models.PROTECT, related_name="+",
                                         verbose_name=bi("Beneficiary bank", "بنك المستفيد"))
    beneficiary_iban = EncryptedTextField(bi("Beneficiary IBAN", "آيبان المستفيد"), plain_max_length=34)
    beneficiary_iban_last4 = models.CharField(bi("Beneficiary IBAN last 4", "آخر 4 خانات من آيبان المستفيد"),
                                              max_length=4)
    beneficiary_iban_hash = models.CharField(bi("Beneficiary IBAN keyed hash", "البصمة المفتاحية لآيبان المستفيد"),
                                             max_length=64, default="", editable=False)
    amount = models.DecimalField(bi("Amount", "المبلغ"), max_digits=18, decimal_places=2)
    currency = models.ForeignKey(Currency, to_field="code", on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("Currency", "العملة"))
    reference = models.CharField(bi("Reference", "المرجع"), max_length=140)
    execution_date = models.DateField(bi("Requested execution date", "تاريخ التنفيذ المطلوب"))
    status = models.CharField(bi("Status", "الحالة"), max_length=9, choices=Status.choices, default=Status.PENDING)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
                                   verbose_name=bi("Created by (payout poster)", "أنشأه (مرحّل الصرف)"))
    created_at = models.DateTimeField(bi("Created at", "تاريخ الإنشاء"), auto_now_add=True)
    cancelled_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+", verbose_name=bi("Cancelled by", "ألغاه"))
    cancelled_at = models.DateTimeField(bi("Cancelled at", "تاريخ الإلغاء"), null=True, blank=True)
    cancel_reason = models.CharField(bi("Cancellation reason", "سبب الإلغاء"), max_length=300, blank=True)

    objects = _ServiceOnlyQuerySet.as_manager()

    CANCEL_FIELDS = frozenset({"status", "cancelled_by_id", "cancelled_at", "cancel_reason"})

    class Meta:
        base_manager_name = "objects"  # review fix 6: _base_manager.update/bulk writes are refused too
        verbose_name = bi("purification bank instruction", "أمر بنكي لصرف التطهير")
        verbose_name_plural = bi("purification bank instructions", "أوامر بنكية لصرف التطهير")
        ordering = ["entity", "-execution_date", "pk"]
        constraints = [models.CheckConstraint(condition=models.Q(amount__gt=0), name="purification_instruction_positive")]

    @property
    def masked_iban(self):
        return iban_rules.masked("SA", self.beneficiary_iban_last4)

    def __str__(self):
        return f"{self.reference} {self.amount} {self.currency_id} -> {self.masked_iban} ({self.status})"

    def save(self, *args, **kwargs):
        allowed, self._service_write = getattr(self, "_service_write", False), False
        if not allowed:
            raise error("Bank instructions are written only when a payout posts or is reversed.",
                        "تُكتب الأوامر البنكية عند ترحيل الصرف أو عكسه فقط.", code="service_only")
        stored = _stored_values(self)
        if stored is None and (self.status != self.Status.PENDING or self.cancelled_by_id is not None
                               or self.cancelled_at is not None):
            raise error("A bank instruction is created pending when its payout posts (D31b).",
                        "يُنشأ الأمر البنكي معلقًا عند ترحيل الصرف.", code="instruction_immutable")
        if stored is not None:
            changed = {name for name, value in stored.items() if getattr(self, name) != value}
            cancelling = (stored["status"] == self.Status.PENDING and self.status == self.Status.CANCELLED
                          and changed <= self.CANCEL_FIELDS)
            if changed and not cancelling:
                raise error("A bank instruction never changes; it is only cancelled with its payout's reversal.",
                            "لا يتغير الأمر البنكي؛ يُلغى فقط بعكس الصرف.", code="instruction_immutable")
            if cancelling and not PurificationTransfer._base_manager.filter(
                    pk=self.payout_id, status=PurificationTransfer.Status.REVERSED).exists():
                raise error("A bank instruction is cancelled only once its payout is reversed (D31b).",
                            "يُلغى الأمر البنكي فقط بعد عكس الصرف.", code="instruction_immutable")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise error("Bank instructions are never deleted.", "لا تُحذف الأوامر البنكية.", code="instruction_immutable")


class ApplicationQuerySet(models.QuerySet):
    """Bulk writes skip save()/delete(): applications of posted receipts and payments never change (R36)."""

    def _touches_locked(self):
        source = self.model.SOURCE_FIELD
        return self.exclude(**{f"{source}__status": "draft"}).exists()

    def update(self, **kwargs):
        if self._touches_locked() or self.model.SOURCE_FIELD in kwargs or f"{self.model.SOURCE_FIELD}_id" in kwargs:
            raise error(SERVICE_ONLY.split(" / ")[0], SERVICE_ONLY.split(" / ")[1], code="service_only")
        return super().update(**kwargs)

    def delete(self):
        if self._touches_locked():
            raise error(SERVICE_ONLY.split(" / ")[0], SERVICE_ONLY.split(" / ")[1], code="service_only")
        return super().delete()


class Application(models.Model):
    """A planned allocation entered on a draft receipt or payment; becomes an allocation when it posts."""

    SOURCE_FIELD = None
    amount = models.DecimalField(bi("Amount", "المبلغ"), **DOC_MONEY_FIELD)

    objects = ApplicationQuerySet.as_manager()

    class Meta:
        abstract = True

    def _source_status(self):
        source = self.source_document
        return type(source)._base_manager.filter(pk=source.pk).values_list("status", flat=True).first()

    def delete(self, *args, **kwargs):
        if self._source_status() != "draft":
            raise error(SERVICE_ONLY.split(" / ")[0], SERVICE_ONLY.split(" / ")[1], code="service_only")
        return super().delete(*args, **kwargs)

    def save(self, *args, **kwargs):
        source = self.source_document
        if self.entity_id != source.entity_id:
            raise error("An application belongs to its document's entity.", "يتبع التخصيص كيان مستنده.",
                        code="entity_mismatch")
        if self._source_status() != "draft":
            raise error(SERVICE_ONLY.split(" / ")[0], SERVICE_ONLY.split(" / ")[1], code="service_only")
        if self.pk is not None:  # never moved to another receipt or payment
            stored = type(self)._base_manager.filter(pk=self.pk).values_list(f"{self.SOURCE_FIELD}_id", flat=True).first()
            if stored is not None and stored != source.pk:
                raise error(SERVICE_ONLY.split(" / ")[0], SERVICE_ONLY.split(" / ")[1], code="service_only")
        super().save(*args, **kwargs)


class ReceiptApplication(Application):
    SOURCE_FIELD = "receipt"
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
    SOURCE_FIELD = "payment"
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

    amount = models.DecimalField(bi("Amount", "المبلغ"), **DOC_MONEY_FIELD)
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
