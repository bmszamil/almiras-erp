"""Group structure: every record in the ERP belongs to an Entity.

Almiras is entity 1. Subsidiaries and minority holdings are added as rows,
not as new code.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models


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
    return RegexValidator(
        rf"^[0-9]{{{n}}}$",
        f"{label_en} must be exactly {n} digits. / يجب أن يتكون {label_ar} من {n} أرقام.",
        code="invalid_digits",
    )


# Bilingual country names for the national address (ISO 3166-1 alpha-2 code stored).
COUNTRY_NAMES = {
    "SA": ("Saudi Arabia", "المملكة العربية السعودية"),
}


class Entity(models.Model):
    class Consolidation(models.TextChoices):
        PARENT = "parent", "Parent (reporting entity)"
        FULL = "full", "Full consolidation (subsidiary)"
        EQUITY = "equity", "Equity method (associate / minority)"
        NONE = "none", "Not consolidated"

    class Standard(models.TextChoices):
        IFRS_SME = "ifrs_sme", "IFRS for SMEs"
        IFRS_FULL = "ifrs_full", "IFRS (as endorsed in KSA)"

    class LegalForm(models.TextChoices):
        LLC = "llc", "Limited liability company"
        JSC = "jsc", "Joint stock company"
        SOLE = "sole", "Sole proprietorship"
        BRANCH_FOREIGN = "branch_foreign", "Branch of a foreign company"
        OTHER = "other", "Other"

    class VatReturnPeriod(models.TextChoices):
        MONTHLY = "monthly", "Monthly"
        QUARTERLY = "quarterly", "Quarterly"

    LEGAL_FORM_AR = {
        "llc": "شركة ذات مسؤولية محدودة",
        "jsc": "شركة مساهمة",
        "sole": "مؤسسة فردية",
        "branch_foreign": "فرع شركة أجنبية",
        "other": "أخرى",
    }
    VAT_RETURN_PERIOD_AR = {"monthly": "شهري", "quarterly": "ربع سنوي"}

    code = models.CharField(max_length=8, unique=True, help_text="Short code used in document numbers, e.g. ALM")
    name_en = models.CharField(max_length=200)
    name_ar = models.CharField(max_length=200)
    cr_number = models.CharField("Commercial registration", max_length=20, blank=True)
    vat_number = models.CharField(max_length=15, blank=True, validators=[validate_vat_number])

    # Registration (invoicing.md §3.0, D10). Optional at DB level; required to
    # issue documents, see core.services.entity_ready_to_issue (R69).
    legal_name_en = models.CharField(max_length=200, blank=True)
    legal_name_ar = models.CharField(max_length=200, blank=True)
    legal_form = models.CharField(max_length=20, choices=LegalForm.choices, blank=True)
    unified_number = models.CharField("Unified national number", max_length=10, blank=True)
    vat_effective_date = models.DateField(null=True, blank=True)
    vat_return_period = models.CharField(max_length=10, choices=VatReturnPeriod.choices, blank=True)

    # National address (bilingual where the spec says so).
    building_no = models.CharField(max_length=4, blank=True, validators=[_digits(4, "Building number", "رقم المبنى")])
    street_en = models.CharField(max_length=200, blank=True)
    street_ar = models.CharField(max_length=200, blank=True)
    district_en = models.CharField(max_length=100, blank=True)
    district_ar = models.CharField(max_length=100, blank=True)
    additional_no = models.CharField(
        "Secondary number", max_length=4, blank=True, validators=[_digits(4, "Secondary number", "الرقم الإضافي")]
    )
    postal_code = models.CharField(max_length=5, blank=True, validators=[_digits(5, "Postal code", "الرمز البريدي")])
    city_en = models.CharField(max_length=100, blank=True)
    city_ar = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=2, default="SA", help_text="ISO 3166-1 alpha-2 code")
    short_address = models.CharField(max_length=8, blank=True)

    base_currency = models.CharField(max_length=3, default="SAR")
    reporting_standard = models.CharField(max_length=10, choices=Standard.choices, default=Standard.IFRS_SME)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="children")
    ownership_pct = models.DecimalField(max_digits=6, decimal_places=3, default=Decimal("100"))
    consolidation = models.CharField(max_length=10, choices=Consolidation.choices, default=Consolidation.FULL)
    sharia_enforced = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "entities"
        ordering = ["code"]

    def clean(self):
        if self.parent is None and self.consolidation != self.Consolidation.PARENT:
            raise ValidationError("An entity without a parent must be the parent (reporting) entity.")
        if not (Decimal("0") < self.ownership_pct <= Decimal("100")):
            raise ValidationError("Ownership must be above 0% and at most 100%.")
        validate_vat_number(self.vat_number)

    @property
    def legal_form_ar(self):
        return self.LEGAL_FORM_AR.get(self.legal_form, "")

    @property
    def vat_return_period_ar(self):
        return self.VAT_RETURN_PERIOD_AR.get(self.vat_return_period, "")

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
