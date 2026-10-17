"""Minimal payment and supplier bill documents used to test the approval plug-in contract (spec §3.3).

Not part of the product: installed only in config.settings_test. The real payables/payments modules
will follow the same pattern.
"""
from decimal import Decimal

from django.conf import settings
from django.db import models

from access.errors import RuleError
from core.models import Entity


class _Document(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PENDING_APPROVAL = "pending_approval", "Pending approval"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        EXECUTED = "executed", "Executed"

    LOCKED_FIELDS = ("entity_id", "amount", "currency", "counterparty", "date")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    counterparty = models.CharField(max_length=200)
    amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    currency = models.CharField(max_length=3, default="SAR")
    date = models.DateField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    source = models.CharField(max_length=60, default="human")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.pk:
            stored = type(self).objects.get(pk=self.pk)
            if stored.status != self.Status.DRAFT:
                changed = [f for f in self.LOCKED_FIELDS if getattr(stored, f) != getattr(self, f)]
                if changed:  # R11
                    raise RuleError(
                        "document_locked",
                        "This document is in approval or approved and cannot be edited.",
                        "هذا المستند قيد الاعتماد أو معتمد ولا يمكن تعديله.",
                        detail=changed,
                    )
        super().save(*args, **kwargs)


class Payment(_Document):
    pass


class Bill(_Document):
    pass
