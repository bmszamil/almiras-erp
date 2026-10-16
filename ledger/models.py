"""General ledger: accounts, periods, journal entries.

Rules enforced here and in ledger.services:
- Posted entries are never edited or deleted; corrections are reversals.
- Every posted entry balances (debits == credits) and has at least two lines.
- Postings into a closed period, a closed entity or another entity's account are rejected.
- Accounts flagged as Sharia-prohibited (e.g. conventional interest) cannot be posted
  to in an entity that enforces Sharia rules.
"""
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from core.models import Currency, Entity, bi


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
    is_monetary = models.BooleanField(
        bi("Monetary", "بند نقدي"), default=False,
        help_text=bi("Included in period-end FX revaluation", "يدخل في إعادة تقييم العملات في نهاية الفترة"),
    )
    currency = models.ForeignKey(
        Currency, to_field="code", null=True, blank=True, on_delete=models.PROTECT, related_name="+",
        verbose_name=bi("Account currency", "عملة الحساب"),
        help_text=bi("Empty: any currency. Set: every line must be in this currency", "فارغ: أي عملة. محدد: يجب أن تكون كل البنود بهذه العملة"),
    )

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
        POSTED = "posted", "Posted"
        REVERSED = "reversed", "Reversed"

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="journal_entries")
    number = models.CharField(max_length=40, blank=True, help_text="Assigned on posting, never reused")
    date = models.DateField()
    period = models.ForeignKey(FiscalPeriod, null=True, blank=True, on_delete=models.PROTECT, related_name="entries")
    description = models.CharField(max_length=500)
    reference = models.CharField(max_length=100, blank=True, help_text="Source document code")
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.DRAFT)
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
        # A posted entry may only change to "reversed"; a reversed entry never changes.
        if stored == self.Status.REVERSED:
            raise LedgerError("Reversed entries are locked.")
        if stored == self.Status.POSTED and not (
            self.status == self.Status.REVERSED and kwargs.get("update_fields") == ["status"]
        ):
            raise LedgerError("Posted entries cannot be edited. Reverse the entry instead.")
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
    class RateSource(models.TextChoices):
        FUNCTIONAL = "functional", bi("Functional currency (rate 1)", "العملة الوظيفية (سعر 1)")
        TABLE = "table", bi("Rate table", "جدول الأسعار")
        MANUAL = "manual", bi("Manual rate", "سعر يدوي")
        CARRYING = "carrying", bi("Carrying rate (settlement)", "السعر الدفتري (تسوية)")

    class LineKind(models.TextChoices):
        NORMAL = "normal", bi("Normal", "عادي")
        SETTLEMENT = "settlement", bi("Settlement", "تسوية")
        REALISED_FX = "realised_fx", bi("Realised FX gain/loss", "فروقات عملة محققة")
        REVALUATION = "revaluation", bi("Revaluation", "إعادة تقييم")
        ROUNDING = "rounding", bi("Currency rounding", "فروقات تقريب")

    entry = models.ForeignKey(JournalEntry, on_delete=models.CASCADE, related_name="lines")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="lines")
    description = models.CharField(max_length=300, blank=True)
    debit = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    credit = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    counterparty_entity = models.ForeignKey(
        Entity, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
        help_text="Required on intercompany accounts, used for elimination",
    )
    # Multi-currency (ledger-fx.md 3.5). debit / credit above are functional-currency amounts.
    currency = models.ForeignKey(
        Currency, to_field="code", on_delete=models.PROTECT, related_name="+",
        verbose_name=bi("Transaction currency", "عملة المعاملة"),
    )
    txn_amount = models.DecimalField(
        bi("Transaction amount", "مبلغ المعاملة"), max_digits=21, decimal_places=3,
        help_text=bi("In the transaction currency, same side as debit/credit", "بعملة المعاملة، في نفس جانب المدين/الدائن"),
    )
    fx_rate = models.DecimalField(
        bi("Exchange rate", "سعر الصرف"), max_digits=20, decimal_places=10, null=True, blank=True,
        help_text=bi("Functional units per 1 transaction unit", "وحدات العملة الوظيفية لكل وحدة من عملة المعاملة"),
    )
    rate_source = models.CharField(
        bi("Rate source", "مصدر السعر"), max_length=10, choices=RateSource.choices, default=RateSource.FUNCTIONAL
    )
    exchange_rate = models.ForeignKey(
        "ExchangeRate", null=True, blank=True, on_delete=models.PROTECT, related_name="lines",
        verbose_name=bi("Rate row", "سجل السعر"),
    )
    settles_line = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="settled_by",
        verbose_name=bi("Settles line", "يسوّي البند"),
    )
    manual_rate_reason = models.CharField(bi("Manual rate reason", "سبب السعر اليدوي"), max_length=300, blank=True)
    line_kind = models.CharField(bi("Line kind", "نوع البند"), max_length=12, choices=LineKind.choices, default=LineKind.NORMAL)

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
            models.CheckConstraint(condition=models.Q(txn_amount__gte=0), name="line_txn_amount_not_negative"),
            models.CheckConstraint(
                condition=models.Q(fx_rate__isnull=True) | models.Q(fx_rate__gt=0), name="line_fx_rate_positive"
            ),
            # Revaluation lines, and only they, carry no transaction amount and no rate (3.5).
            models.CheckConstraint(
                condition=(
                    models.Q(line_kind="revaluation", txn_amount=0, fx_rate__isnull=True)
                    | (~models.Q(line_kind="revaluation") & ~models.Q(txn_amount=0, fx_rate__isnull=True))
                ),
                name="line_revaluation_kind",
            ),
        ]

    def _entry_locked(self):
        return self.entry_id and JournalEntry.objects.filter(pk=self.entry_id).exclude(
            status=JournalEntry.Status.DRAFT
        ).exists()

    def save(self, *args, **kwargs):
        if self._entry_locked():
            raise LedgerError("Lines of a posted entry cannot be changed.")
        self._fill_functional_defaults()
        super().save(*args, **kwargs)

    def _fill_functional_defaults(self):
        """R1: a line saved without currency fields is in the entity's functional currency at rate 1.

        Only empty fields are filled; a value that was set is never changed, so
        an inconsistent line is rejected by validate_entry rather than repaired.
        """
        if self.currency_id is None:
            self.currency_id = JournalEntry.objects.filter(pk=self.entry_id).values_list(
                "entity__base_currency", flat=True
            ).first()
        if self.line_kind == self.LineKind.REVALUATION:
            return
        if self.txn_amount is None:
            self.txn_amount = (self.debit or Decimal("0")) + (self.credit or Decimal("0"))
        if self.fx_rate is None and self.rate_source == self.RateSource.FUNCTIONAL:
            self.fx_rate = Decimal("1")

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


class ExchangeRate(models.Model):
    """Units of ``to_currency`` per 1 unit of ``from_currency`` (ledger-fx.md 3.3).

    ``entity`` null = group-wide rate; set = that entity's override (OQ-4 default).
    A row used by a posted line is locked; corrections are new rows (R11).
    No ``forward`` rate type exists (R36).
    """

    class RateType(models.TextChoices):
        SPOT = "spot", bi("Spot", "فوري")
        AVERAGE = "average", bi("Average", "متوسط")

    class Source(models.TextChoices):
        MANUAL = "manual", bi("Manual entry", "إدخال يدوي")
        SAMA = "sama", bi("Saudi Central Bank (SAMA)", "البنك المركزي السعودي")
        ECB = "ecb", bi("European Central Bank", "البنك المركزي الأوروبي")
        CBE = "cbe", bi("Central Bank of Egypt", "البنك المركزي المصري")
        CBUAE = "cbuae", bi("Central Bank of the UAE", "مصرف الإمارات العربية المتحدة المركزي")
        PBOC = "pboc", bi("People's Bank of China", "بنك الشعب الصيني")
        DERIVED = "derived", bi("Derived cross rate", "سعر تقاطعي مشتق")
        FILE = "file", bi("File import", "استيراد ملف")

    entity = models.ForeignKey(
        Entity, null=True, blank=True, on_delete=models.PROTECT, related_name="exchange_rates",
        verbose_name=bi("Entity (empty = group-wide)", "الكيان (فارغ = للمجموعة)"),
    )
    date = models.DateField(bi("Rate date", "تاريخ السعر"))
    from_currency = models.ForeignKey(
        Currency, to_field="code", on_delete=models.PROTECT, related_name="+", verbose_name=bi("From", "من")
    )
    to_currency = models.ForeignKey(
        Currency, to_field="code", on_delete=models.PROTECT, related_name="+", verbose_name=bi("To", "إلى")
    )
    rate = models.DecimalField(bi("Rate", "السعر"), max_digits=20, decimal_places=10)
    rate_type = models.CharField(bi("Rate type", "نوع السعر"), max_length=8, choices=RateType.choices, default=RateType.SPOT)
    source = models.CharField(bi("Source", "المصدر"), max_length=8, choices=Source.choices, default=Source.MANUAL)
    source_reference = models.CharField(bi("Source reference", "مرجع المصدر"), max_length=200, blank=True)
    period_start = models.DateField(bi("Average from", "المتوسط من"), null=True, blank=True)
    period_end = models.DateField(bi("Average to", "المتوسط إلى"), null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    import_batch = models.CharField(bi("Import batch", "دفعة الاستيراد"), max_length=40, blank=True)

    class Meta:
        verbose_name = bi("exchange rate", "سعر صرف")
        verbose_name_plural = bi("exchange rates", "أسعار الصرف")
        ordering = ["-date", "from_currency", "to_currency"]
        constraints = [
            models.CheckConstraint(condition=~models.Q(from_currency=models.F("to_currency")), name="rate_currencies_differ"),
            models.CheckConstraint(condition=models.Q(rate__gt=0), name="rate_positive"),
            models.CheckConstraint(condition=models.Q(rate_type__in=["spot", "average"]), name="rate_type_known"),
            models.CheckConstraint(
                condition=~models.Q(rate_type="average")
                | (models.Q(period_start__isnull=False) & models.Q(period_end__isnull=False)),
                name="average_rate_has_window",
            ),
            # One row per key; a null entity counts as one value (partial indexes work on SQLite and PostgreSQL).
            models.UniqueConstraint(
                fields=["entity", "date", "from_currency", "to_currency", "rate_type", "source"],
                condition=models.Q(entity__isnull=False), name="unique_entity_rate",
            ),
            models.UniqueConstraint(
                fields=["date", "from_currency", "to_currency", "rate_type", "source"],
                condition=models.Q(entity__isnull=True), name="unique_global_rate",
            ),
        ]

    def clean(self):
        errors = []
        if self.from_currency_id and self.from_currency_id == self.to_currency_id:
            errors.append("From and to currency must differ. / يجب أن تختلف العملتان.")
        for code in (self.from_currency_id, self.to_currency_id):
            cur = Currency.objects.filter(code=code).first() if code else None
            if cur is not None and (not cur.is_active or cur.minor_units > 3):
                errors.append(f"Currency {code} cannot be used. / لا يمكن استخدام العملة {code}.")
        if self.rate is not None and self.rate <= 0:
            errors.append("The rate must be above zero. / يجب أن يكون السعر أكبر من صفر.")
        if self.rate_type == self.RateType.AVERAGE and not (self.period_start and self.period_end):
            errors.append("An average rate needs its period. / يحتاج السعر المتوسط إلى فترته.")
        if self.period_start and self.period_end and self.period_end < self.period_start:
            errors.append("The averaging period ends before it starts. / تنتهي فترة المتوسط قبل بدايتها.")
        if errors:
            raise ValidationError(errors)

    def is_locked(self):
        return self.pk is not None and JournalLine.objects.filter(exchange_rate_id=self.pk).exclude(
            entry__status=JournalEntry.Status.DRAFT
        ).exists()

    def save(self, *args, **kwargs):
        if self.is_locked():
            raise LedgerError(
                "This rate is used by posted entries and cannot be changed; add a new rate row. / "
                "هذا السعر مستخدم في قيود مرحّلة ولا يمكن تعديله؛ أضف سجل سعر جديدًا."
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.is_locked():
            raise LedgerError(
                "This rate is used by posted entries and cannot be deleted. / "
                "هذا السعر مستخدم في قيود مرحّلة ولا يمكن حذفه."
            )
        return super().delete(*args, **kwargs)

    def __str__(self):
        scope = self.entity.code if self.entity_id else "group"
        return f"{self.date} {self.from_currency_id}->{self.to_currency_id} {self.rate} ({self.rate_type}, {self.source}, {scope})"


class RevaluationRun(models.Model):
    """One period-end FX revaluation per entity and period (ledger-fx.md 3.6, R21 to R26)."""

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="revaluation_runs")
    period = models.ForeignKey(FiscalPeriod, on_delete=models.PROTECT, related_name="revaluation_runs")
    rate_date = models.DateField(bi("Rate date", "تاريخ السعر"))
    entry = models.OneToOneField(
        JournalEntry, null=True, blank=True, on_delete=models.PROTECT, related_name="revaluation_run",
        verbose_name=bi("Revaluation entry", "قيد إعادة التقييم"),
    )
    reversal = models.OneToOneField(
        JournalEntry, null=True, blank=True, on_delete=models.PROTECT, related_name="revaluation_reversal_of",
        verbose_name=bi("Automatic reversal", "العكس التلقائي"),
    )
    run_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    run_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = bi("FX revaluation run", "تشغيل إعادة تقييم العملات")
        verbose_name_plural = bi("FX revaluation runs", "تشغيلات إعادة تقييم العملات")
        constraints = [models.UniqueConstraint(fields=["entity", "period"], name="one_revaluation_per_period")]

    def save(self, *args, **kwargs):
        if self.period.entity_id != self.entity_id:
            raise LedgerError("The period belongs to another entity. / الفترة تخص كيانًا آخر.")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.entity.code} {self.period.name} revaluation"
