"""Test-only document models. Never installed outside tests (config.settings_test)."""
from decimal import Decimal

from django.conf import settings
from django.db import models

from core.models import Entity, bi


class _Document(models.Model):
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    number = models.CharField(bi("Number", "الرقم"), max_length=20, blank=True)
    # A registered document type records its creator (ledger.documents.register requires it, R35).
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    modified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    modified_at = models.DateTimeField(null=True, blank=True)
    # What the document books; validate_lines (testdocs.apps) checks the entry against it.
    amount = models.DecimalField(bi("Amount", "المبلغ"), max_digits=18, decimal_places=2, default=Decimal("10.00"))

    class Meta:
        abstract = True


class Bill(_Document):
    class Meta:
        verbose_name = bi("test bill", "فاتورة مورد للاختبار")


class Receipt(_Document):
    class Meta:
        verbose_name = bi("test receipt", "سند قبض للاختبار")


class SalesDocument(_Document):
    class Meta:
        verbose_name = bi("test sales document", "مستند مبيعات للاختبار")


class Unregistered(_Document):
    class Meta:
        verbose_name = bi("unregistered test document", "مستند اختبار غير مسجل")


def validate_lines(document, lines):
    """Registered line hook: the entry's debits equal the document's amount (as a real module checks its totals)."""
    debit = sum((line.debit for line in lines), Decimal("0"))
    if debit != document.amount:
        return [f"Debits {debit} differ from the document amount {document.amount}. / "
                f"المدين {debit} يختلف عن مبلغ المستند {document.amount}."]
    return []
