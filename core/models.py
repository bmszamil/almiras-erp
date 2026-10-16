"""Group structure: every record in the ERP belongs to an Entity.

Almiras is entity 1. Subsidiaries and minority holdings are added as rows,
not as new code.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models

from .iso3166 import ISO_3166_ALPHA2


def validate_vat_number(value):
    """KSA VAT registration number: 15 digits, starting and ending with 3.

    PENDING VERIFICATION: this format rule is taken from invoicing.md §3.0 and
    must still be checked against the official ZATCA BR-KSA business rules.
    """
    if value in (None, ""):
        return
    if not (len(value) == 15 and value.isascii() and value.isdigit() and value[0] == "3" and value[-1] == "3"):
        raise ValidationError(
            "VAT number must be 15 digits and start and end with 3. / "
            "يجب أن يتكون الرقم الضريبي من 15 رقمًا ويبدأ وينتهي بالرقم 3.",
            code="invalid_vat_number",
        )


def _digits(n, label_en, label_ar):
    # \Z, not $: "$" also matches before a trailing newline ("1234\n").
    return RegexValidator(
        rf"^[0-9]{{{n}}}\Z",
        f"{label_en} must be exactly {n} digits. / يجب أن يتكون {label_ar} من {n} أرقام.",
        code="invalid_digits",
    )


def validate_unified_number(value):
    """Unified national number: digits only, first digit 7 (invoicing.md R72, D22)."""
    if value in (None, ""):
        return
    if not (value.isascii() and value.isdigit() and value[0] == "7"):
        raise ValidationError(
            "Unified national number must be digits only and start with 7. / "
            "يجب أن يتكون الرقم الوطني الموحد من أرقام فقط ويبدأ بالرقم 7.",
            code="invalid_unified_number",
        )


def validate_country_code(value):
    """ISO 3166-1 alpha-2 code, upper case (invoicing.md §3.0)."""
    if value not in ISO_3166_ALPHA2:
        raise ValidationError(
            f"{value!r} is not an ISO 3166-1 alpha-2 country code. / "
            f"{value!r} ليس رمز دولة صالحًا وفق ISO 3166-1.",
            code="invalid_country",
        )


def bi(en, ar):
    """Bilingual label "English / العربية", as used in the access module and admin (R28).

    gettext tooling is not installed, so labels carry both languages.
    """
    return f"{en} / {ar}"


def label_part(label, language):
    """The English (``"en"``) or Arabic (``"ar"``) half of a :func:`bi` label."""
    en, _, ar = str(label).partition(" / ")
    return ar if language == "ar" and ar else en


# Bilingual country names for the national address (ISO 3166-1 alpha-2 code stored).
COUNTRY_NAMES = {
    "SA": ("Saudi Arabia", "المملكة العربية السعودية"),
}


class Entity(models.Model):
    class Consolidation(models.TextChoices):
        PARENT = "parent", bi("Parent (reporting entity)", "الشركة الأم (الكيان المُعِدّ للتقارير)")
        FULL = "full", bi("Full consolidation (subsidiary)", "توحيد كامل (شركة تابعة)")
        EQUITY = "equity", bi("Equity method (associate / minority)", "طريقة حقوق الملكية (شركة زميلة أو حصة أقلية)")
        NONE = "none", bi("Not consolidated", "غير موحّد")

    class Standard(models.TextChoices):
        IFRS_SME = "ifrs_sme", bi("IFRS for SMEs", "المعيار الدولي للمنشآت الصغيرة والمتوسطة")
        IFRS_FULL = "ifrs_full", bi("IFRS (as endorsed in KSA)", "المعايير الدولية (المعتمدة في المملكة)")

    class LegalForm(models.TextChoices):
        LLC = "llc", bi("Limited liability company", "شركة ذات مسؤولية محدودة")
        JSC = "jsc", bi("Joint stock company", "شركة مساهمة")
        SOLE = "sole", bi("Sole proprietorship", "مؤسسة فردية")
        BRANCH_FOREIGN = "branch_foreign", bi("Branch of a foreign company", "فرع شركة أجنبية")
        OTHER = "other", bi("Other", "أخرى")

    class VatReturnPeriod(models.TextChoices):
        MONTHLY = "monthly", bi("Monthly", "شهري")
        QUARTERLY = "quarterly", bi("Quarterly", "ربع سنوي")

    code = models.CharField(
        bi("Code", "الرمز"), max_length=8, unique=True,
        help_text=bi("Short code used in document numbers, e.g. ALM", "رمز مختصر يُستخدم في أرقام المستندات، مثل ALM"),
    )
    name_en = models.CharField(bi("Name (English)", "الاسم (بالإنجليزية)"), max_length=200)
    name_ar = models.CharField(bi("Name (Arabic)", "الاسم (بالعربية)"), max_length=200)
    cr_number = models.CharField(bi("Commercial registration", "السجل التجاري"), max_length=20, blank=True)
    vat_number = models.CharField(
        bi("VAT number", "الرقم الضريبي"), max_length=15, blank=True, validators=[validate_vat_number]
    )

    # Registration (invoicing.md §3.0, D10). Optional at DB level; required to
    # issue documents, see core.services.entity_ready_to_issue (R69).
    legal_name_en = models.CharField(bi("Legal name (English)", "الاسم القانوني (بالإنجليزية)"), max_length=200, blank=True)
    legal_name_ar = models.CharField(bi("Legal name (Arabic)", "الاسم القانوني (بالعربية)"), max_length=200, blank=True)
    legal_form = models.CharField(
        bi("Legal form", "الشكل القانوني"), max_length=20, choices=LegalForm.choices, blank=True
    )
    unified_number = models.CharField(
        bi("Unified national number", "الرقم الوطني الموحد"), max_length=10, blank=True,
        validators=[validate_unified_number],
    )
    vat_effective_date = models.DateField(bi("VAT effective date", "تاريخ سريان التسجيل الضريبي"), null=True, blank=True)
    vat_return_period = models.CharField(
        bi("VAT return period", "فترة الإقرار الضريبي"), max_length=10, choices=VatReturnPeriod.choices, blank=True
    )

    # National address (bilingual where the spec says so).
    building_no = models.CharField(
        bi("Building number", "رقم المبنى"), max_length=4, blank=True,
        validators=[_digits(4, "Building number", "رقم المبنى")],
    )
    street_en = models.CharField(bi("Street (English)", "الشارع (بالإنجليزية)"), max_length=200, blank=True)
    street_ar = models.CharField(bi("Street (Arabic)", "الشارع (بالعربية)"), max_length=200, blank=True)
    district_en = models.CharField(bi("District (English)", "الحي (بالإنجليزية)"), max_length=100, blank=True)
    district_ar = models.CharField(bi("District (Arabic)", "الحي (بالعربية)"), max_length=100, blank=True)
    additional_no = models.CharField(
        bi("Secondary number", "الرقم الإضافي"), max_length=4, blank=True,
        validators=[_digits(4, "Secondary number", "الرقم الإضافي")],
    )
    postal_code = models.CharField(
        bi("Postal code", "الرمز البريدي"), max_length=5, blank=True,
        validators=[_digits(5, "Postal code", "الرمز البريدي")],
    )
    city_en = models.CharField(bi("City (English)", "المدينة (بالإنجليزية)"), max_length=100, blank=True)
    city_ar = models.CharField(bi("City (Arabic)", "المدينة (بالعربية)"), max_length=100, blank=True)
    country = models.CharField(
        bi("Country", "الدولة"), max_length=2, default="SA", validators=[validate_country_code],
        help_text=bi("ISO 3166-1 alpha-2 code", "رمز الدولة وفق ISO 3166-1"),
    )
    short_address = models.CharField(bi("Short address", "العنوان المختصر"), max_length=8, blank=True)

    base_currency = models.CharField(bi("Base currency", "العملة الأساسية"), max_length=3, default="SAR")
    reporting_standard = models.CharField(
        bi("Reporting standard", "معيار إعداد التقارير"), max_length=10, choices=Standard.choices,
        default=Standard.IFRS_SME,
    )
    parent = models.ForeignKey(
        "self", verbose_name=bi("Parent entity", "الكيان الأم"), null=True, blank=True,
        on_delete=models.PROTECT, related_name="children",
    )
    ownership_pct = models.DecimalField(
        bi("Ownership %", "نسبة الملكية %"), max_digits=6, decimal_places=3, default=Decimal("100")
    )
    consolidation = models.CharField(
        bi("Consolidation", "التوحيد"), max_length=10, choices=Consolidation.choices, default=Consolidation.FULL
    )
    sharia_enforced = models.BooleanField(bi("Sharia enforced", "تطبيق الضوابط الشرعية"), default=True)
    is_active = models.BooleanField(bi("Active", "نشط"), default=True)

    class Meta:
        verbose_name = bi("entity", "كيان")
        verbose_name_plural = bi("entities", "الكيانات")
        ordering = ["code"]

    def clean(self):
        # The VAT number format is checked by its field validator in full_clean
        # (clean_fields), so it is not repeated here: one error, not two.
        if self.parent is None and self.consolidation != self.Consolidation.PARENT:
            raise ValidationError(
                "An entity without a parent must be the parent (reporting) entity. / "
                "يجب أن يكون الكيان الذي ليس له كيان أم هو الشركة الأم (الكيان المُعِدّ للتقارير)."
            )
        if not (Decimal("0") < self.ownership_pct <= Decimal("100")):
            raise ValidationError("Ownership must be above 0% and at most 100%. / يجب أن تكون نسبة الملكية أكبر من 0% ولا تتجاوز 100%.")

    @property
    def legal_form_en(self):
        return label_part(self.get_legal_form_display(), "en") if self.legal_form else ""

    @property
    def legal_form_ar(self):
        return label_part(self.get_legal_form_display(), "ar") if self.legal_form else ""

    @property
    def vat_return_period_en(self):
        return label_part(self.get_vat_return_period_display(), "en") if self.vat_return_period else ""

    @property
    def vat_return_period_ar(self):
        return label_part(self.get_vat_return_period_display(), "ar") if self.vat_return_period else ""

    @property
    def country_name_en(self):
        return COUNTRY_NAMES.get(self.country, (self.country, self.country))[0]

    @property
    def country_name_ar(self):
        return COUNTRY_NAMES.get(self.country, (self.country, self.country))[1]

    def __str__(self):
        return f"{self.code} — {self.name_en}"


# Access models (access.md §3) live in core/access/models.py and belong to this app.
from core.access import models as _access_models  # noqa: E402,F401
