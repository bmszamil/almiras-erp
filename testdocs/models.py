"""Test-only document models. Never installed outside tests (config.settings_test)."""
from django.conf import settings
from django.db import models

from core.models import Entity, bi


class _Document(models.Model):
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    number = models.CharField(bi("Number", "الرقم"), max_length=20, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    modified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    modified_at = models.DateTimeField(null=True, blank=True)

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
