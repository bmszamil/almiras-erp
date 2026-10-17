"""Group structure: every record in the ERP belongs to an Entity.

Almiras is entity 1. Subsidiaries and minority holdings are added as rows,
not as new code.
"""
import re
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models


VAT_NUMBER_RE = re.compile(r"^3\d{13}3$")
ADDRESS_FIELDS = ("building_number", "street", "district", "city", "postal_code", "country_code")


def valid_vat_number(value):
    """KSA VAT registration number: 15 digits, starting and ending with 3."""
    return bool(VAT_NUMBER_RE.match(value or ""))


class Entity(models.Model):
    class Consolidation(models.TextChoices):
        PARENT = "parent", "Parent (reporting entity)"
        FULL = "full", "Full consolidation (subsidiary)"
        EQUITY = "equity", "Equity method (associate / minority)"
        NONE = "none", "Not consolidated"

    class Standard(models.TextChoices):
        IFRS_SME = "ifrs_sme", "IFRS for SMEs"
        IFRS_FULL = "ifrs_full", "IFRS (as endorsed in KSA)"

    code = models.CharField(max_length=8, unique=True, help_text="Short code used in document numbers, e.g. ALM")
    name_en = models.CharField(max_length=200)
    name_ar = models.CharField(max_length=200)
    cr_number = models.CharField("Commercial registration", max_length=20, blank=True)
    vat_number = models.CharField(max_length=15, blank=True)
    # Structured national address (ZATCA seller/buyer address). additional_number is optional.
    building_number = models.CharField(max_length=4, blank=True)
    street = models.CharField(max_length=200, blank=True)
    district = models.CharField(max_length=100, blank=True)
    city = models.CharField(max_length=100, blank=True)
    postal_code = models.CharField(max_length=5, blank=True)
    country_code = models.CharField(max_length=2, default="SA", help_text="ISO 3166-1 alpha-2")
    additional_number = models.CharField(max_length=4, blank=True)
    base_currency = models.CharField(max_length=3, default="SAR")
    reporting_standard = models.CharField(max_length=10, choices=Standard.choices, default=Standard.IFRS_SME)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="children")
    ownership_pct = models.DecimalField(max_digits=6, decimal_places=3, default=Decimal("100"))
    consolidation = models.CharField(max_length=10, choices=Consolidation.choices, default=Consolidation.FULL)
    sharia_enforced = models.BooleanField(default=True)
    books_in_erp = models.BooleanField(
        default=False,
        help_text="True when the entity keeps its own ledger in this ERP. Drives mirror bills for intercompany "
        "invoices (intercompany spec D8, R15).",
    )
    books_in_erp_from = models.DateField(
        null=True, blank=True, help_text="First invoice date for which a mirror bill is created. Required when books_in_erp."
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "entities"
        ordering = ["code"]

    def clean(self):
        if self.parent is None and self.consolidation != self.Consolidation.PARENT:
            raise ValidationError("An entity without a parent must be the parent (reporting) entity.")
        if not (Decimal("0") < self.ownership_pct <= Decimal("100")):
            raise ValidationError("Ownership must be above 0% and at most 100%.")
        if self.vat_number and not valid_vat_number(self.vat_number):
            raise ValidationError({"vat_number": "A VAT number has 15 digits and starts and ends with 3. / "
                                                 "الرقم الضريبي 15 خانة يبدأ وينتهي بالرقم 3."})
        if self.books_in_erp and self.books_in_erp_from is None:
            raise ValidationError({"books_in_erp_from": "Set the date from which the entity keeps its books here. / "
                                                        "حدد تاريخ بدء القيود في النظام."})

    def missing_address_fields(self):
        """ZATCA structured address: the required parts that are empty."""
        return [f for f in ADDRESS_FIELDS if not (getattr(self, f) or "").strip()]

    def address(self):
        return {f: getattr(self, f) for f in (*ADDRESS_FIELDS, "additional_number")}

    def keeps_books_on(self, date):
        """D8/R15: the entity keeps its ledger in this ERP on `date`."""
        return bool(self.books_in_erp and self.books_in_erp_from is not None and date >= self.books_in_erp_from)

    def __str__(self):
        return f"{self.code} — {self.name_en}"
