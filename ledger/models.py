"""General ledger: accounts, periods, journal entries.

Rules enforced here and in ledger.services:
- Posted entries are never edited or deleted; corrections are reversals.
- Every posted entry balances (debits == credits) and has at least two lines.
- Postings into a closed period, a closed entity or another entity's account are rejected.
- Accounts flagged as Sharia-prohibited (e.g. conventional interest) cannot be posted
  to in an entity that enforces Sharia rules.
- Manual entries (requires_approval=True) go draft -> pending_approval -> approved -> posted
  through the approvals engine; while pending or approved they are locked like posted entries.
"""
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from core.models import Entity


class LedgerError(ValidationError):
    """Raised when a ledger rule is broken."""


class Account(models.Model):
    class Type(models.TextChoices):
        ASSET = "asset", "Asset"
        LIABILITY = "liability", "Liability"
        EQUITY = "equity", "Equity"
        INCOME = "income", "Income"
        EXPENSE = "expense", "Expense"

    DEBIT_NORMAL = {Type.ASSET, Type.EXPENSE}

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="accounts")
    code = models.CharField(max_length=20)
    name_en = models.CharField(max_length=200)
    name_ar = models.CharField(max_length=200)
    type = models.CharField(max_length=10, choices=Type.choices)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="children")
    is_postable = models.BooleanField(default=True, help_text="False for header/group accounts")
    is_intercompany = models.BooleanField(default=False)
    sharia_prohibited = models.BooleanField(default=False, help_text="Blocks postings where Sharia is enforced")
    requires_purification = models.BooleanField(
        default=False, help_text="Income to be purified (donated); posting allowed but flagged"
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = [("entity", "code")]
        ordering = ["entity", "code"]

    @property
    def is_debit_normal(self):
        return self.type in self.DEBIT_NORMAL

    def __str__(self):
        return f"{self.code} {self.name_en}"


class FiscalPeriod(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="periods")
    name = models.CharField(max_length=20, help_text="e.g. 2027-01")
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(max_length=6, choices=Status.choices, default=Status.OPEN)

    class Meta:
        unique_together = [("entity", "name")]
        ordering = ["entity", "start_date"]

    def clean(self):
        if self.end_date < self.start_date:
            raise ValidationError("Period end is before its start.")
        overlap = FiscalPeriod.objects.filter(
            entity=self.entity, start_date__lte=self.end_date, end_date__gte=self.start_date
        ).exclude(pk=self.pk)
        if overlap.exists():
            raise ValidationError("Period overlaps an existing period for this entity.")

    def __str__(self):
        return f"{self.entity.code} {self.name} ({self.status})"


class JournalEntry(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PENDING_APPROVAL = "pending_approval", "Pending approval"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        POSTED = "posted", "Posted"
        REVERSED = "reversed", "Reversed"

    # Fields post_entry writes; the only change allowed on an approved entry.
    POSTING_FIELDS = {"period", "number", "status", "posted_by", "posted_at"}

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="journal_entries")
    number = models.CharField(max_length=40, blank=True, help_text="Assigned on posting, never reused")
    date = models.DateField()
    period = models.ForeignKey(FiscalPeriod, null=True, blank=True, on_delete=models.PROTECT, related_name="entries")
    description = models.CharField(max_length=500)
    reference = models.CharField(max_length=100, blank=True, help_text="Source document code")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    source = models.CharField(
        max_length=60, default="human", help_text="human / import / agent:<name> (who or what created the entry)"
    )
    requires_approval = models.BooleanField(
        default=True,
        help_text="Manual entries need approval before posting. System entries made by module services "
        "through create_entry (and reversals) are exempt until OQ3 is answered.",
    )
    reversal_of = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversed_by"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name_plural = "journal entries"
        ordering = ["entity", "date", "number"]
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "number"], condition=~models.Q(number=""), name="unique_entry_number_per_entity"
            )
        ]

    def _stored_status(self):
        if not self.pk:
            return None
        return JournalEntry.objects.filter(pk=self.pk).values_list("status", flat=True).first()

    def save(self, *args, **kwargs):
        stored = self._stored_status()
        update_fields = kwargs.get("update_fields")
        # A posted entry may only change to "reversed"; a reversed entry never changes.
        if stored == self.Status.REVERSED:
            raise LedgerError("Reversed entries are locked.")
        if stored == self.Status.REJECTED:
            raise LedgerError("Rejected entries are locked. Create a new entry. / القيد المرفوض مقفل.")
        if stored == self.Status.POSTED and not (
            self.status == self.Status.REVERSED and update_fields == ["status"]
        ):
            raise LedgerError("Posted entries cannot be edited. Reverse the entry instead.")
        # Pending or approved entries are locked (R11); only the approval workflow moves their status.
        if stored == self.Status.PENDING_APPROVAL and not (
            update_fields == ["status"] and getattr(self, "_workflow", False) is True
            and self.status in {self.Status.DRAFT, self.Status.APPROVED, self.Status.REJECTED}
        ):
            raise LedgerError(
                "Entries pending approval cannot be edited. / لا يمكن تعديل قيد بانتظار الاعتماد."
            )
        if stored == self.Status.APPROVED and not (
            self.status == self.Status.POSTED and getattr(self, "_posting", False) is True
            and update_fields is not None and set(update_fields) <= self.POSTING_FIELDS
        ):
            raise LedgerError("Approved entries are locked until posted. / القيد المعتمد مقفل حتى يُرحّل.")
        if stored in {None, self.Status.DRAFT} and self.status != self.Status.DRAFT and not (
            (self.status == self.Status.POSTED and getattr(self, "_posting", False) is True)
            or (self.status == self.Status.PENDING_APPROVAL and getattr(self, "_workflow", False) is True)
        ):
            # Status jumps only through ledger.services.post_entry / the approval workflow.
            raise LedgerError("Status can only change through ledger.services. / لا تتغير الحالة إلا عبر خدمات الدفتر.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self._stored_status() != self.Status.DRAFT:
            raise LedgerError("Only draft entries can be deleted.")
        return super().delete(*args, **kwargs)

    @property
    def total_debit(self):
        return sum((line.debit for line in self.lines.all()), Decimal("0"))

    @property
    def total_credit(self):
        return sum((line.credit for line in self.lines.all()), Decimal("0"))

    def __str__(self):
        return f"{self.number or 'DRAFT'} {self.date} {self.description}"


class JournalLine(models.Model):
    entry = models.ForeignKey(JournalEntry, on_delete=models.CASCADE, related_name="lines")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="lines")
    description = models.CharField(max_length=300, blank=True)
    debit = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    credit = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    counterparty_entity = models.ForeignKey(
        Entity, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
        help_text="Required on intercompany accounts, used for elimination",
    )

    class Meta:
        ordering = ["entry", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(debit__gte=0) & models.Q(credit__gte=0), name="line_amounts_not_negative"
            ),
            models.CheckConstraint(
                condition=(models.Q(debit=0) & models.Q(credit__gt=0)) | (models.Q(credit=0) & models.Q(debit__gt=0)),
                name="line_one_side_only",
            ),
        ]

    def _entry_locked(self):
        return self.entry_id and JournalEntry.objects.filter(pk=self.entry_id).exclude(
            status=JournalEntry.Status.DRAFT
        ).exists()

    def save(self, *args, **kwargs):
        if self._entry_locked():
            raise LedgerError("Lines of a posted entry cannot be changed.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self._entry_locked():
            raise LedgerError("Lines of a posted entry cannot be deleted.")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.account.code} Dr {self.debit} Cr {self.credit}"


class EntrySequence(models.Model):
    """Gap-free entry numbering per entity and year."""

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT)
    year = models.PositiveIntegerField()
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = [("entity", "year")]
