"""Group structure: every record in the ERP belongs to an Entity.

Almiras is entity 1. Subsidiaries and minority holdings are added as rows,
not as new code.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models


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

    def __str__(self):
        return f"{self.code} — {self.name_en}"
