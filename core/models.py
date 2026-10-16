"""Group structure: every record in the ERP belongs to an Entity.

Almiras is entity 1. Subsidiaries and minority holdings are added as rows,
not as new code.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, RegexValidator
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


# Highest ISO 4217 exponent the ledger stores (txn_amount has 3 decimals; D5, R40).
MAX_MINOR_UNITS = 3


class Currency(models.Model):
    """ISO 4217 currency (ledger-fx.md 3.1). Global reference data: no entity FK, same for every entity."""

    code = models.CharField(
        bi("Code", "الرمز"), max_length=3, primary_key=True,
        validators=[RegexValidator(r"^[A-Z]{3}\Z", "ISO 4217 code: three capital letters. / رمز ISO 4217: ثلاثة أحرف لاتينية كبيرة.")],
    )
    numeric_code = models.CharField(
        bi("Numeric code", "الرمز الرقمي"), max_length=3,
        validators=[_digits(3, "Numeric code", "الرمز الرقمي")],
    )
    name_en = models.CharField(bi("Name (English)", "الاسم (بالإنجليزية)"), max_length=100)
    name_ar = models.CharField(bi("Name (Arabic)", "الاسم (بالعربية)"), max_length=100)
    minor_units = models.PositiveSmallIntegerField(
        bi("Minor units", "عدد الخانات العشرية"), validators=[MaxValueValidator(4)],
        help_text=bi("ISO 4217 exponent: SAR 2, JPY 0, KWD 3", "أُسّ العملة وفق ISO 4217: الريال 2، الين 0، الدينار الكويتي 3"),
    )
    symbol = models.CharField(bi("Symbol", "الرمز المختصر"), max_length=8, blank=True)
    is_active = models.BooleanField(bi("Active", "نشطة"), default=True)

    class Meta:
        verbose_name = bi("currency", "عملة")
        verbose_name_plural = bi("currencies", "العملات")
        ordering = ["code"]

    def _check_storable(self):
        # R40: storage holds at most 3 decimals, so such a currency cannot be active.
        if self.is_active and self.minor_units is not None and self.minor_units > MAX_MINOR_UNITS:
            raise ValidationError(
                f"Currency {self.code} has {self.minor_units} minor units; at most {MAX_MINOR_UNITS} can be stored, "
                "so it cannot be active. / "
                f"للعملة {self.code} عدد {self.minor_units} خانات عشرية؛ الحد الأقصى للتخزين {MAX_MINOR_UNITS}، "
                "فلا يمكن تفعيلها.",
                code="too_many_minor_units",
            )

    def clean(self):
        self._check_storable()

    def save(self, *args, **kwargs):
        self._check_storable()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.code


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

    # The entity's functional currency (ledger-fx.md 3.2; name kept, OQ-10). Column name unchanged.
    base_currency = models.ForeignKey(
        Currency, to_field="code", db_column="base_currency", on_delete=models.PROTECT, default="SAR",
        related_name="+", verbose_name=bi("Functional currency", "العملة الوظيفية"),
        help_text=bi("Must have 2 minor units; locked after the first posting", "يجب أن تكون بخانتين عشريتين؛ تُقفل بعد أول ترحيل"),
    )
    presentation_currency = models.ForeignKey(
        Currency, to_field="code", on_delete=models.PROTECT, null=True, blank=True, related_name="+",
        verbose_name=bi("Presentation currency", "عملة العرض"),
        help_text=bi("Group reporting entity only (default SAR, OQ-7)", "للكيان المُعِدّ لتقارير المجموعة فقط (الافتراضي الريال، OQ-7)"),
    )
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
        self._check_functional_currency()

    def _check_functional_currency(self):
        """R4: the functional currency has 2 minor units. R5: it is locked once the entity has a posted entry."""
        if self.base_currency_id is None:
            return
        currency = Currency.objects.filter(code=self.base_currency_id).first()
        if currency is None or currency.minor_units != 2 or not currency.is_active:
            raise ValidationError(
                {"base_currency": f"Functional currency {self.base_currency_id} must be an active currency with 2 minor units. / "
                 f"يجب أن تكون العملة الوظيفية {self.base_currency_id} عملة نشطة بخانتين عشريتين."},
                code="functional_currency_decimals",
            )
        if self.pk:
            stored = Entity.objects.filter(pk=self.pk).values_list("base_currency", flat=True).first()
            if stored is not None and stored != self.base_currency_id and self.has_posted_entries():
                raise ValidationError(
                    {"base_currency": f"{self.code} already has posted entries; its functional currency cannot change. / "
                     f"للكيان {self.code} قيود مرحّلة؛ لا يمكن تغيير عملته الوظيفية."},
                    code="functional_currency_locked",
                )

    def has_posted_entries(self):
        # Reverse relation from ledger.JournalEntry; core does not import ledger.
        return self.journal_entries.exclude(status="draft").exists()

    def save(self, *args, **kwargs):
        # Checked on every save, not only in full_clean, so no code path skips R4 / R5.
        self._check_functional_currency()
        super().save(*args, **kwargs)

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
