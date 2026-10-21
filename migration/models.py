"""Legacy data migration (docs/specs/migration.md §3): account maps and the link from legacy items to ledger drafts.

Records change only through migration.services. An approved map and its rows never change (R13): a change is a new
version. An ``ImportedEntry`` is unique per entity, source system, kind and legacy key, so a Falcon entry (or an
entity's opening balance) is never imported twice (R14, R15).
"""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from core.models import Entity, bi


class MigrationError(ValidationError):
    """A migration rule is broken. Messages are "English / العربية"."""


LOCKED = ("An approved or superseded account map never changes; draft a new version. / "
          "لا تتغير خريطة الحسابات المعتمدة أو المستبدلة؛ أعدّ نسخة جديدة.")


class AccountMapQuerySet(models.QuerySet):
    """Bulk writes skip ``save()``/``delete()``: an approved or superseded map is refused here too (R13)."""

    def update(self, **kwargs):
        if self.exclude(status=AccountMap.Status.DRAFT).exists():
            raise MigrationError(LOCKED)
        return super().update(**kwargs)

    def delete(self):
        if self.exclude(status=AccountMap.Status.DRAFT).exists():
            raise MigrationError(LOCKED)
        return super().delete()


class AccountMap(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        APPROVED = "approved", bi("Approved", "معتمدة")
        SUPERSEDED = "superseded", bi("Superseded", "مستبدلة")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="legacy_account_maps",
                               verbose_name=bi("Entity", "الكيان"))
    source_system = models.CharField(bi("Source system", "النظام المصدر"), max_length=20, default="falcon")
    version = models.PositiveIntegerField(bi("Version", "النسخة"))
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.DRAFT)
    note = models.CharField(bi("Note", "ملاحظة"), max_length=500, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
                                   verbose_name=bi("Created by", "أنشأها"))
    created_at = models.DateTimeField(bi("Created at", "تاريخ الإنشاء"), default=timezone.now)
    modified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=bi("Last modified by", "آخر من عدّلها"))
    modified_at = models.DateTimeField(bi("Last modified at", "تاريخ آخر تعديل"), null=True, blank=True)
    # R13 as amended (M7, AT-25): the preparer submits the draft, which opens its approval request in the one approval
    # engine (activity migration.account_map); a change to the draft afterwards clears the submission.
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+", verbose_name=bi("Submitted by", "قدّمها"))
    submitted_at = models.DateTimeField(bi("Submitted at", "تاريخ التقديم"), null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=bi("Approved by", "اعتمدها"))
    approved_at = models.DateTimeField(bi("Approved at", "تاريخ الاعتماد"), null=True, blank=True)

    objects = AccountMapQuerySet.as_manager()

    class Meta:
        verbose_name = bi("Legacy account map", "خريطة حسابات النظام السابق")
        ordering = ["entity", "source_system", "-version"]
        constraints = [
            models.UniqueConstraint(fields=["entity", "source_system", "version"], name="one_map_version"),
            models.UniqueConstraint(fields=["entity", "source_system"], condition=models.Q(status="draft"),
                                    name="one_draft_map"),
            models.UniqueConstraint(fields=["entity", "source_system"], condition=models.Q(status="approved"),
                                    name="one_approved_map"),
        ]

    def save(self, *args, **kwargs):
        stored = AccountMap.objects.filter(pk=self.pk).values_list("status", flat=True).first() if self.pk else None
        if stored in (self.Status.APPROVED, self.Status.SUPERSEDED) and not (
            stored == self.Status.APPROVED and self.status == self.Status.SUPERSEDED
            and kwargs.get("update_fields") == ["status"]
        ):
            raise MigrationError(LOCKED)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if AccountMap.objects.filter(pk=self.pk).exclude(status=self.Status.DRAFT).exists():
            raise MigrationError(LOCKED)
        return super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.entity.code} {self.source_system} v{self.version} ({self.status})"


class AccountMapRowQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if self.exclude(account_map__status=AccountMap.Status.DRAFT).exists():
            raise MigrationError(LOCKED)
        return super().update(**kwargs)

    def delete(self):
        if self.exclude(account_map__status=AccountMap.Status.DRAFT).exists():
            raise MigrationError(LOCKED)
        return super().delete()


class AccountMapRow(models.Model):
    account_map = models.ForeignKey(AccountMap, on_delete=models.CASCADE, related_name="rows",
                                    verbose_name=bi("Account map", "خريطة الحسابات"))
    legacy_code = models.CharField(bi("Legacy code", "كود الحساب في النظام السابق"), max_length=20)
    legacy_name = models.CharField(bi("Legacy name", "اسم الحساب في النظام السابق"), max_length=200, blank=True)
    account = models.ForeignKey("ledger.Account", on_delete=models.PROTECT, related_name="+",
                                verbose_name=bi("ERP account", "حساب النظام"))
    counterparty = models.ForeignKey(Entity, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                                     verbose_name=bi("Counterparty entity", "الكيان المقابل"))
    note = models.CharField(bi("Note", "ملاحظة"), max_length=300, blank=True)

    objects = AccountMapRowQuerySet.as_manager()

    class Meta:
        verbose_name = bi("Legacy account map row", "بند خريطة حسابات النظام السابق")
        ordering = ["account_map", "legacy_code"]
        constraints = [models.UniqueConstraint(fields=["account_map", "legacy_code"], name="one_row_per_legacy_code")]

    def _map_is_draft(self):
        return AccountMap.objects.filter(pk=self.account_map_id, status=AccountMap.Status.DRAFT).exists()

    def save(self, *args, **kwargs):
        if not self._map_is_draft():
            raise MigrationError(LOCKED)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if not self._map_is_draft():
            raise MigrationError(LOCKED)
        return super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.legacy_code} -> {self.account.code}"


class ImportedEntry(models.Model):
    class Kind(models.TextChoices):
        OPENING = "opening", bi("Opening balances", "أرصدة افتتاحية")
        JOURNAL = "journal", bi("Replayed journal entry", "قيد يومية معاد إدخاله")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="legacy_imports",
                               verbose_name=bi("Entity", "الكيان"))
    source_system = models.CharField(bi("Source system", "النظام المصدر"), max_length=20, default="falcon")
    kind = models.CharField(bi("Kind", "النوع"), max_length=10, choices=Kind.choices)
    legacy_key = models.CharField(bi("Legacy key", "المرجع في النظام السابق"), max_length=40,
                                  help_text=bi("Falcon entry number, or 'opening'", "رقم القيد في فالكون أو 'opening'"))
    journal_entry = models.OneToOneField("ledger.JournalEntry", on_delete=models.PROTECT, related_name="legacy_import",
                                         verbose_name=bi("Journal entry", "القيد"))
    account_map = models.ForeignKey(AccountMap, on_delete=models.PROTECT, related_name="imports",
                                    verbose_name=bi("Account map used", "خريطة الحسابات المستخدمة"))
    file_sha256 = models.CharField(bi("File SHA-256", "بصمة الملف"), max_length=64)
    lines_hash = models.CharField(bi("Lines hash", "بصمة البنود"), max_length=64)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
                                   verbose_name=bi("Imported by", "استوردها"))
    created_at = models.DateTimeField(bi("Imported at", "تاريخ الاستيراد"), default=timezone.now)

    class Meta:
        verbose_name = bi("Imported legacy entry", "قيد مستورد من النظام السابق")
        verbose_name_plural = bi("Imported legacy entries", "قيود مستوردة من النظام السابق")
        ordering = ["entity", "kind", "legacy_key"]
        constraints = [models.UniqueConstraint(fields=["entity", "source_system", "kind", "legacy_key"],
                                               name="import_once_per_legacy_key")]

    def __str__(self):
        return f"{self.entity.code} {self.source_system} {self.kind} {self.legacy_key}"
