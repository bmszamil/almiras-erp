"""Banking (docs/specs/banking.md §3): bank accounts, statements, matches, bank transactions, reconciliations.

Every business record has an ``entity``; ``Bank`` is global reference data like ``Currency``. Statements, their
lines, match parts and the event log are append-only (model + queryset guards here, PostgreSQL triggers in
migration 0003). Status changes come only from ``banking.services``, which set ``obj._service_write = True``
right before the one save they make. Money is ``Decimal``: account-currency amounts ``Decimal(21,3)`` checked
against the currency's own decimals, functional amounts ``Decimal(18,2)``.
"""
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from core.models import Currency, Entity, bi

from . import iban as iban_rules
from .crypto import EncryptedTextField

ACCOUNT_MONEY = dict(max_digits=21, decimal_places=3)
FUNCTIONAL_MONEY = dict(max_digits=18, decimal_places=2)


class BankingError(ValidationError):
    """A banking rule is broken. Messages are "English / العربية"."""

    def __init__(self, message, code=None, params=None):
        super().__init__(message, code=code, params=params)
        self.code = code


def error(en, ar, code=None):
    return BankingError(f"{en} / {ar}", code=code)


SERVICE_ONLY = ("This record changes only through banking.services.", "يتغير هذا السجل من خلال خدمات البنوك فقط.")
APPEND_ONLY = ("This banking record is append-only: it is never changed or deleted.",
               "هذا السجل البنكي للإضافة فقط: لا يُعدَّل ولا يُحذف.")


def _service_only():
    return error(*SERVICE_ONLY, code="service_only")


def _append_only():
    return error(*APPEND_ONLY, code="append_only")


class AppendOnlyQuerySet(models.QuerySet):
    """Bulk writes skip save()/delete(); append-only rows are refused here too (R15, R45)."""

    def update(self, **kwargs):
        raise _append_only()

    def delete(self):
        raise _append_only()

    def bulk_update(self, objs, fields, *args, **kwargs):
        raise _append_only()


class AppendOnly(models.Model):
    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.pk is not None and type(self)._base_manager.filter(pk=self.pk).exists():
            raise _append_only()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise _append_only()


class ServiceQuerySet(models.QuerySet):
    """Bulk writes are refused: these records change one by one through banking.services."""

    def update(self, **kwargs):
        raise _service_only()

    def delete(self):
        raise _service_only()

    def bulk_update(self, objs, fields, *args, **kwargs):
        raise _service_only()


class ServiceWritten(models.Model):
    """Saved only by banking.services (``_service_write``); never deleted."""

    objects = ServiceQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        allowed, self._service_write = getattr(self, "_service_write", False), False
        if not allowed:
            raise _service_only()
        if self.pk is not None:
            stored_entity = type(self)._base_manager.filter(pk=self.pk).values_list("entity_id", flat=True).first()
            if stored_entity is not None and stored_entity != self.entity_id:
                raise error("A banking record never moves to another entity.", "لا ينتقل السجل البنكي إلى كيان آخر.",
                            code="entity_fixed")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise _service_only()


def _changed_fields(obj):
    """Concrete fields whose value differs from the stored row (empty for a new object)."""
    if obj.pk is None:
        return set()
    stored = type(obj)._base_manager.filter(pk=obj.pk).values(*[f.attname for f in obj._meta.concrete_fields]).first()
    if stored is None:
        return set()
    return {name for name, value in stored.items() if getattr(obj, name) != value}


def _stored_status(obj):
    if obj.pk is None:
        return None
    return type(obj)._base_manager.filter(pk=obj.pk).values_list("status", flat=True).first()


# --- 3.1 Bank (global reference data) -----------------------------------------------------------------------

class Bank(models.Model):
    code = models.SlugField(bi("Code", "الرمز"), max_length=20, unique=True)
    name_en = models.CharField(bi("Name (English)", "الاسم (بالإنجليزية)"), max_length=120)
    name_ar = models.CharField(bi("Name (Arabic)", "الاسم (بالعربية)"), max_length=120)
    country = models.CharField(bi("Country", "الدولة"), max_length=2, help_text=bi("ISO 3166 alpha-2", "رمز الدولة"))
    sa_bank_code = models.CharField(
        bi("Saudi IBAN bank code", "رمز البنك في الآيبان السعودي"), max_length=2, null=True, blank=True,
        help_text=bi("Positions 5-6 of a Saudi IBAN [CC-verify]", "الخانتان 5-6 في الآيبان السعودي"),
    )
    swift_bic = models.CharField(bi("SWIFT BIC", "رمز السويفت"), max_length=11, blank=True)
    verified = models.BooleanField(
        bi("Verified", "متحقق منه"), default=False,
        help_text=bi("Set by the accountant after the compliance check of code and BIC", "يحدده المحاسب بعد التحقق"),
    )
    is_active = models.BooleanField(bi("Active", "نشط"), default=True)

    class Meta:
        verbose_name = bi("bank", "بنك")
        verbose_name_plural = bi("banks", "البنوك")
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} — {self.name_en} / {self.name_ar}"


# --- statement formats and CSV layouts ------------------------------------------------------------------------

class StatementFormat(models.TextChoices):
    MT940 = "mt940", bi("SWIFT MT940", "سويفت MT940")
    CAMT053 = "camt053", bi("ISO 20022 camt.053", "كشف ISO 20022 camt.053")
    CSV = "csv", bi("Bank CSV export (layout)", "ملف CSV من البنك (حسب التنسيق)")


class CsvLayout(ServiceWritten):
    """How one bank's CSV export maps to statement fields (R10; per bank, OQ-1). Data, not code.

    ``columns`` maps statement fields to header names: booking_date (required), value_date, amount (signed) or
    debit + credit, direction (with ``credit_values`` / ``debit_values``), bank_reference, end_to_end_id,
    customer_reference, transaction_code, counterparty_name, counterparty_iban, narrative, balance (running
    balance; lets the importer derive opening and closing balances), currency.
    """

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    code = models.SlugField(bi("Code", "الرمز"), max_length=30)
    name_en = models.CharField(bi("Name (English)", "الاسم (بالإنجليزية)"), max_length=120)
    name_ar = models.CharField(bi("Name (Arabic)", "الاسم (بالعربية)"), max_length=120)
    bank = models.ForeignKey(Bank, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Bank", "البنك"))
    delimiter = models.CharField(bi("Delimiter", "الفاصل"), max_length=1, default=",")
    encoding = models.CharField(bi("Encoding", "الترميز"), max_length=20, default="utf-8")
    date_format = models.CharField(bi("Date format", "صيغة التاريخ"), max_length=20, default="%Y-%m-%d")
    decimal_separator = models.CharField(bi("Decimal separator", "الفاصل العشري"), max_length=1, default=".")
    thousands_separator = models.CharField(bi("Thousands separator", "فاصل الآلاف"), max_length=1, blank=True, default=",")
    skip_rows = models.PositiveSmallIntegerField(bi("Rows before the header", "أسطر قبل العناوين"), default=0)
    columns = models.JSONField(bi("Column mapping", "ربط الأعمدة"), default=dict)
    credit_values = models.JSONField(bi("Credit indicator values", "قيم مؤشر الإيداع"), default=list, blank=True)
    debit_values = models.JSONField(bi("Debit indicator values", "قيم مؤشر السحب"), default=list, blank=True)
    sample_verified = models.BooleanField(
        bi("Checked against a bank sample", "متحقق منه مقابل عينة من البنك"), default=False,
        help_text=bi("OQ-1: unverified layouts import with a warning", "التنسيقات غير المتحقق منها تُستورد مع تنبيه"),
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    modified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    modified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = bi("CSV statement layout", "تنسيق كشف CSV")
        verbose_name_plural = bi("CSV statement layouts", "تنسيقات كشوف CSV")
        constraints = [models.UniqueConstraint(fields=["entity", "code"], name="csv_layout_code_per_entity")]

    def __str__(self):
        return f"{self.entity.code} {self.code}"


# --- 3.2 BankAccount ---------------------------------------------------------------------------------------

class BankAccount(ServiceWritten):
    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        ACTIVE = "active", bi("Active", "نشط")
        CLOSED = "closed", bi("Closed", "مغلق")

    class ShariaBasis(models.TextChoices):
        ISLAMIC = "islamic", bi("Islamic (profit)", "إسلامي (أرباح)")
        CONVENTIONAL = "conventional", bi("Conventional (interest)", "تقليدي (فوائد)")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="bank_accounts",
                               verbose_name=bi("Entity", "الكيان"))
    code = models.SlugField(bi("Code", "الرمز"), max_length=30)
    name_en = models.CharField(bi("Name (English)", "الاسم (بالإنجليزية)"), max_length=120)
    name_ar = models.CharField(bi("Name (Arabic)", "الاسم (بالعربية)"), max_length=120)
    bank = models.ForeignKey(Bank, on_delete=models.PROTECT, related_name="accounts", verbose_name=bi("Bank", "البنك"))
    iban = EncryptedTextField(bi("IBAN", "الآيبان"), plain_max_length=34, blank=True, default="")
    iban_hash = models.CharField(max_length=64, blank=True, default="", editable=False)
    iban_last4 = models.CharField(max_length=4, blank=True, default="", editable=False)
    account_number = EncryptedTextField(bi("Account number", "رقم الحساب"), plain_max_length=40, blank=True, default="")
    account_number_last4 = models.CharField(max_length=4, blank=True, default="", editable=False)
    currency = models.ForeignKey(Currency, to_field="code", on_delete=models.PROTECT, related_name="+",
                                 verbose_name=bi("Currency", "العملة"))
    gl_account = models.ForeignKey("ledger.Account", on_delete=models.PROTECT, related_name="+",
                                   verbose_name=bi("GL account", "حساب الأستاذ"))
    sharia_basis = models.CharField(bi("Sharia basis", "الأساس الشرعي"), max_length=12, choices=ShariaBasis.choices)
    statement_format = models.CharField(bi("Statement format", "صيغة الكشف"), max_length=10,
                                        choices=StatementFormat.choices)
    csv_layout = models.ForeignKey(CsvLayout, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                                   verbose_name=bi("CSV layout", "تنسيق CSV"))
    opening_statement_date = models.DateField(bi("Opening statement date", "تاريخ الرصيد الافتتاحي للكشف"))
    opening_statement_balance = models.DecimalField(bi("Opening statement balance", "الرصيد الافتتاحي للكشف"),
                                                    **ACCOUNT_MONEY)
    active_from = models.DateField(bi("Active from", "نشط من"))
    active_to = models.DateField(bi("Active to", "نشط حتى"), null=True, blank=True)
    status = models.CharField(bi("Status", "الحالة"), max_length=6, choices=Status.choices, default=Status.DRAFT)
    warnings = models.JSONField(bi("Warnings", "تنبيهات"), default=list, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    modified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    modified_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = bi("bank account", "حساب بنكي")
        verbose_name_plural = bi("bank accounts", "الحسابات البنكية")
        ordering = ["entity", "code"]
        constraints = [
            models.UniqueConstraint(fields=["entity", "code"], name="bank_account_code_per_entity"),
            # One IBAN belongs to one entity: unique across all entities (3.2).
            models.UniqueConstraint(fields=["iban_hash", "currency"], condition=~models.Q(iban_hash=""),
                                    name="bank_account_iban_currency_unique"),
            # R5: one active bank account per GL account, so the GL balance is that account's book balance.
            models.UniqueConstraint(fields=["gl_account"], condition=models.Q(status="active"),
                                    name="bank_account_one_active_per_gl"),
        ]

    @property
    def masked_number(self):
        """Masked IBAN or account number (R4): the only form used in labels, logs and messages."""
        if self.iban_last4:
            return iban_rules.masked(self.bank.country if self.bank_id else "", self.iban_last4)
        if self.account_number_last4:
            return f"•••• {self.account_number_last4}"
        return ""

    def __str__(self):
        return f"{self.code} {self.masked_number}".strip()


class BankAccountChange(ServiceWritten):
    """A proposed change of IBAN, account number, currency or GL account of an active account (R7, OQ-9)."""

    class Status(models.TextChoices):
        DRAFT = "draft", bi("Waiting for approval", "بانتظار الاعتماد")
        APPROVED = "approved", bi("Approved", "معتمد")
        REJECTED = "rejected", bi("Rejected", "مرفوض")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    bank_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, related_name="changes")
    iban = EncryptedTextField(plain_max_length=34, blank=True, default="")
    iban_hash = models.CharField(max_length=64, blank=True, default="")
    iban_last4 = models.CharField(max_length=4, blank=True, default="")
    account_number = EncryptedTextField(plain_max_length=40, blank=True, default="")
    currency = models.ForeignKey(Currency, to_field="code", null=True, blank=True, on_delete=models.PROTECT,
                                 related_name="+")
    gl_account = models.ForeignKey("ledger.Account", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    reason = models.CharField(bi("Reason", "السبب"), max_length=300)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.DRAFT)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = bi("bank account change", "تعديل حساب بنكي")
        verbose_name_plural = bi("bank account changes", "تعديلات الحسابات البنكية")


# --- 3.8 BankingSettings -------------------------------------------------------------------------------------

class BankingSettings(ServiceWritten):
    entity = models.OneToOneField(Entity, on_delete=models.PROTECT, related_name="banking_settings")
    m1_date_window_days = models.PositiveSmallIntegerField(bi("M1 date window (days)", "نافذة تاريخ M1 (أيام)"), default=7)
    m2_date_window_days = models.PositiveSmallIntegerField(bi("M2 date window (days)", "نافذة تاريخ M2 (أيام)"), default=3)
    m3_max_items = models.PositiveSmallIntegerField(bi("M3 maximum items", "الحد الأقصى لبنود M3"), default=5)
    auto_confirm_m1 = models.BooleanField(bi("Auto-confirm M1 matches", "تأكيد مطابقات M1 تلقائيًا"), default=True)
    modified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    modified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = bi("banking settings", "إعدادات البنوك")
        verbose_name_plural = bi("banking settings", "إعدادات البنوك")
        constraints = [
            models.CheckConstraint(condition=models.Q(m3_max_items__gte=2), name="banking_m3_at_least_two"),
        ]


# --- 3.3 StatementImport and 3.4 StatementLine ---------------------------------------------------------------

class StatementImport(AppendOnly):
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    bank_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, related_name="statements")
    file_sha256 = models.CharField(max_length=64)
    file_content = EncryptedTextField(bi("Original file (base64)", "الملف الأصلي"))
    file_name = models.CharField(bi("File name", "اسم الملف"), max_length=200)
    format = models.CharField(bi("Format", "الصيغة"), max_length=10, choices=StatementFormat.choices)
    statement_id = models.CharField(bi("Statement id", "رقم الكشف"), max_length=35)
    sequence_no = models.PositiveIntegerField(bi("Sequence", "التسلسل"), null=True, blank=True)
    period_start = models.DateField(bi("Period start", "بداية الفترة"))
    period_end = models.DateField(bi("Period end", "نهاية الفترة"))
    currency = models.ForeignKey(Currency, to_field="code", on_delete=models.PROTECT, related_name="+")
    opening_balance = models.DecimalField(bi("Opening balance", "الرصيد الافتتاحي"), **ACCOUNT_MONEY)
    closing_balance = models.DecimalField(bi("Closing balance", "الرصيد الختامي"), **ACCOUNT_MONEY)
    line_count = models.PositiveIntegerField(default=0)
    total_credits = models.DecimalField(**ACCOUNT_MONEY, default=Decimal("0"))
    total_debits = models.DecimalField(**ACCOUNT_MONEY, default=Decimal("0"))
    accepted_duplicates = models.JSONField(default=list, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    imported_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    imported_at = models.DateTimeField(default=timezone.now)
    source = models.CharField(max_length=60, default="human")

    class Meta:
        verbose_name = bi("statement import", "استيراد كشف")
        verbose_name_plural = bi("statement imports", "استيرادات الكشوف")
        ordering = ["bank_account", "period_start", "sequence_no", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["bank_account", "file_sha256"], name="statement_file_once_per_account"),
            models.CheckConstraint(condition=models.Q(period_end__gte=models.F("period_start")),
                                   name="statement_period_order"),
        ]

    def __str__(self):
        return f"{self.bank_account.code} {self.statement_id} {self.period_start}..{self.period_end}"


class StatementLine(AppendOnly):
    class Direction(models.TextChoices):
        CREDIT = "credit", bi("Credit (money in)", "إيداع (وارد)")
        DEBIT = "debit", bi("Debit (money out)", "سحب (صادر)")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    bank_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, related_name="lines")
    statement = models.ForeignKey(StatementImport, on_delete=models.PROTECT, related_name="lines")
    line_no = models.PositiveIntegerField(bi("Line", "السطر"))
    booking_date = models.DateField(bi("Booking date", "تاريخ القيد"))
    value_date = models.DateField(bi("Value date", "تاريخ الاستحقاق"), null=True, blank=True)
    direction = models.CharField(bi("Direction", "الاتجاه"), max_length=6, choices=Direction.choices)
    amount = models.DecimalField(bi("Amount", "المبلغ"), **ACCOUNT_MONEY)
    currency = models.ForeignKey(Currency, to_field="code", on_delete=models.PROTECT, related_name="+")
    original_amount = models.DecimalField(**ACCOUNT_MONEY, null=True, blank=True)
    original_currency = models.ForeignKey(Currency, to_field="code", null=True, blank=True, on_delete=models.PROTECT,
                                          related_name="+")
    bank_reference = models.CharField(bi("Bank reference", "مرجع البنك"), max_length=35, blank=True)
    end_to_end_id = models.CharField(bi("End-to-end id", "المعرّف الشامل"), max_length=35, blank=True)
    customer_reference = models.CharField(bi("Customer reference", "مرجع العميل"), max_length=35, blank=True)
    transaction_code = models.CharField(bi("Transaction code", "رمز العملية"), max_length=10, blank=True)
    counterparty_name = EncryptedTextField(bi("Counterparty", "الطرف المقابل"), plain_max_length=140, blank=True,
                                           default="")
    counterparty_iban = EncryptedTextField(bi("Counterparty IBAN", "آيبان الطرف المقابل"), plain_max_length=34,
                                           blank=True, default="")
    narrative = EncryptedTextField(bi("Narrative", "البيان"), blank=True, default="")
    line_hash = models.CharField(max_length=64, db_index=True)
    duplicate_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        verbose_name = bi("statement line", "سطر كشف")
        verbose_name_plural = bi("statement lines", "أسطر الكشوف")
        ordering = ["bank_account", "booking_date", "statement_id", "line_no"]
        constraints = [
            models.UniqueConstraint(fields=["statement", "line_no"], name="statement_line_no_unique"),
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="statement_line_amount_positive"),
            models.CheckConstraint(condition=models.Q(direction__in=["credit", "debit"]),
                                   name="statement_line_direction_known"),
        ]

    @property
    def signed_amount(self):
        return self.amount if self.direction == self.Direction.CREDIT else -self.amount

    def __str__(self):  # no counterparty or narrative: personal data (R4)
        return f"{self.bank_account.code} #{self.statement_id}/{self.line_no} {self.booking_date} {self.direction} {self.amount}"


# --- 3.7 Reconciliation (before Match, which points to it) ----------------------------------------------------

class Reconciliation(ServiceWritten):
    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        SUBMITTED = "submitted", bi("Submitted", "مقدَّمة")
        APPROVED = "approved", bi("Approved", "معتمدة")
        SUPERSEDED = "superseded", bi("Superseded (reopened)", "ملغاة (أعيد فتحها)")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    bank_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, related_name="reconciliations")
    period = models.ForeignKey("ledger.FiscalPeriod", on_delete=models.PROTECT, related_name="+")
    as_of = models.DateField(bi("As of", "حتى تاريخ"))
    bank_balance = models.DecimalField(bi("Bank balance", "رصيد البنك"), **ACCOUNT_MONEY)
    book_balance = models.DecimalField(bi("Book balance", "الرصيد الدفتري"), **ACCOUNT_MONEY)
    book_balance_functional = models.DecimalField(bi("Book balance (functional)", "الرصيد الدفتري (العملة الوظيفية)"),
                                                  **FUNCTIONAL_MONEY)
    outstanding_book_debits = models.DecimalField(**ACCOUNT_MONEY)
    outstanding_book_credits = models.DecimalField(**ACCOUNT_MONEY)
    unmatched_bank_credits = models.DecimalField(**ACCOUNT_MONEY)
    unmatched_bank_debits = models.DecimalField(**ACCOUNT_MONEY)
    difference = models.DecimalField(bi("Difference", "الفرق"), **ACCOUNT_MONEY)
    items = models.JSONField(default=list)
    snapshot_sha256 = models.CharField(max_length=64)
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.DRAFT)
    prepared_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    prepared_at = models.DateTimeField(default=timezone.now)
    modified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    modified_at = models.DateTimeField(null=True, blank=True)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+")
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    reopened_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    reopened_at = models.DateTimeField(null=True, blank=True)
    reopen_reason = models.CharField(max_length=300, blank=True)

    class Meta:
        verbose_name = bi("bank reconciliation", "تسوية بنكية")
        verbose_name_plural = bi("bank reconciliations", "التسويات البنكية")
        ordering = ["bank_account", "as_of"]
        constraints = [
            models.UniqueConstraint(fields=["bank_account", "period"], condition=~models.Q(status="superseded"),
                                    name="one_reconciliation_per_account_period"),
        ]

    @property
    def created_by_id(self):
        """Segregation (access R14): the preparer is the reconciliation's creator."""
        return self.prepared_by_id

    def save(self, *args, **kwargs):
        """R40: an approved reconciliation is frozen; reopening only marks it superseded. Superseded never changes."""
        stored = _stored_status(self)
        changed = _changed_fields(self)
        if stored == self.Status.SUPERSEDED and changed:
            raise error("A superseded reconciliation never changes.", "التسوية الملغاة لا تتغير.", code="reconciliation_locked")
        if stored == self.Status.APPROVED and (
                self.status != self.Status.SUPERSEDED
                or changed - {"status", "reopened_by_id", "reopened_at", "reopen_reason"}):
            raise error("An approved reconciliation is frozen; only reopening changes it (R40).",
                        "التسوية المعتمدة مجمّدة؛ لا يغيرها إلا إعادة فتحها.", code="reconciliation_locked")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.bank_account.code} {self.as_of} ({self.status})"


# --- 3.5 Match and MatchPart ------------------------------------------------------------------------------------

class Match(ServiceWritten):
    class Rule(models.TextChoices):
        M0 = "M0", bi("M0 reversal pair", "M0 قيد وعكسه")
        M1 = "M1", bi("M1 amount and reference", "M1 المبلغ والمرجع")
        M2 = "M2", bi("M2 amount and date", "M2 المبلغ والتاريخ")
        M3 = "M3", bi("M3 grouped", "M3 مجمّعة")
        MANUAL = "manual", bi("Manual", "يدوية")

    class Status(models.TextChoices):
        PROPOSED = "proposed", bi("Proposed", "مقترحة")
        CONFIRMED = "confirmed", bi("Confirmed", "مؤكدة")
        REJECTED = "rejected", bi("Rejected", "مرفوضة")
        UNDONE = "undone", bi("Undone", "ملغاة")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    bank_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, related_name="matches")
    rule = models.CharField(bi("Rule", "القاعدة"), max_length=6, choices=Rule.choices)
    confidence = models.PositiveSmallIntegerField(bi("Confidence", "درجة الثقة"), default=0)
    status = models.CharField(bi("Status", "الحالة"), max_length=9, choices=Status.choices, default=Status.PROPOSED)
    auto = models.BooleanField(bi("Confirmed automatically", "مؤكدة تلقائيًا"), default=False)
    proposed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    proposed_at = models.DateTimeField(default=timezone.now)
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+")
    confirmed_at = models.DateTimeField(null=True, blank=True)
    rejected_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    rejected_at = models.DateTimeField(null=True, blank=True)
    undone_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                  related_name="+")
    undone_at = models.DateTimeField(null=True, blank=True)
    undo_reason = models.CharField(max_length=300, blank=True)
    reconciliation = models.ForeignKey(Reconciliation, null=True, blank=True, on_delete=models.PROTECT,
                                       related_name="locked_matches")

    class Meta:
        verbose_name = bi("bank match", "مطابقة بنكية")
        verbose_name_plural = bi("bank matches", "المطابقات البنكية")
        constraints = [
            models.CheckConstraint(condition=models.Q(confidence__lte=100), name="match_confidence_max_100"),
            models.CheckConstraint(condition=models.Q(reconciliation__isnull=True) | models.Q(status="confirmed"),
                                   name="match_locked_only_when_confirmed"),
        ]

    def save(self, *args, **kwargs):
        stored = _stored_status(self)
        changed = _changed_fields(self)
        if stored in (self.Status.REJECTED, self.Status.UNDONE) and changed:
            raise error("A rejected or undone match never changes.", "المطابقة المرفوضة أو الملغاة لا تتغير.",
                        code="match_locked")
        if stored == self.Status.CONFIRMED and self.status == self.Status.UNDONE and type(self)._base_manager.filter(
                pk=self.pk, reconciliation__isnull=False).exists():
            raise error("This match is locked by an approved reconciliation (R40).",
                        "هذه المطابقة مقفلة بتسوية معتمدة.", code="match_locked")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.bank_account.code} {self.rule} #{self.pk} ({self.status})"


class MatchPart(AppendOnly):
    class Side(models.TextChoices):
        BANK_IN = "bank_in", bi("Bank credit", "إيداع بنكي")
        BANK_OUT = "bank_out", bi("Bank debit", "سحب بنكي")
        BOOK_DEBIT = "book_debit", bi("Book debit", "مدين دفتري")
        BOOK_CREDIT = "book_credit", bi("Book credit", "دائن دفتري")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    match = models.ForeignKey(Match, on_delete=models.PROTECT, related_name="parts")
    statement_line = models.ForeignKey(StatementLine, null=True, blank=True, on_delete=models.PROTECT,
                                       related_name="match_parts")
    journal_line = models.ForeignKey("ledger.JournalLine", null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+")
    amount = models.DecimalField(**ACCOUNT_MONEY)
    side = models.CharField(max_length=11, choices=Side.choices)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(statement_line__isnull=False) & models.Q(journal_line__isnull=True))
                | (models.Q(statement_line__isnull=True) & models.Q(journal_line__isnull=False)),
                name="match_part_one_side",
            ),
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="match_part_amount_positive"),
        ]


# --- 3.6 BankTransaction -----------------------------------------------------------------------------------------

class BankTransaction(ServiceWritten):
    class Kind(models.TextChoices):
        BANK_CHARGE = "bank_charge", bi("Bank charge", "رسوم بنكية")
        ISLAMIC_PROFIT = "islamic_profit", bi("Islamic deposit profit", "أرباح وديعة إسلامية")
        CONVENTIONAL_INTEREST_CREDIT = "conventional_interest_credit", bi(
            "Conventional interest credited (to purify)", "فوائد تقليدية مقيدة (للتطهير)")
        OWN_TRANSFER = "own_transfer", bi("Transfer between own accounts", "تحويل بين حسابات الكيان")
        INTERCOMPANY_TRANSFER = "intercompany_transfer", bi("Intercompany transfer", "تحويل بين شركات المجموعة")

    class Direction(models.TextChoices):
        IN = "in", bi("Money in", "وارد")
        OUT = "out", bi("Money out", "صادر")

    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        SUBMITTED = "submitted", bi("Submitted", "مقدَّمة")
        POSTED = "posted", bi("Posted", "مرحّلة")
        REVERSED = "reversed", bi("Reversed", "معكوسة")
        DISCARDED = "discarded", bi("Discarded", "ملغاة")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="bank_transactions")
    bank_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, related_name="transactions")
    number = models.CharField(bi("Number", "الرقم"), max_length=40, blank=True)
    kind = models.CharField(bi("Kind", "النوع"), max_length=28, choices=Kind.choices)
    statement_line = models.ForeignKey(StatementLine, null=True, blank=True, on_delete=models.PROTECT,
                                       related_name="bank_transactions")
    date = models.DateField(bi("Date", "التاريخ"))
    to_date = models.DateField(bi("Other leg date", "تاريخ الطرف الآخر"), null=True, blank=True)
    direction = models.CharField(bi("Direction", "الاتجاه"), max_length=3, choices=Direction.choices)
    amount = models.DecimalField(bi("Amount", "المبلغ"), **ACCOUNT_MONEY)
    currency = models.ForeignKey(Currency, to_field="code", on_delete=models.PROTECT, related_name="+")
    to_bank_account = models.ForeignKey(BankAccount, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                                        verbose_name=bi("Other own account", "الحساب الآخر للكيان"))
    to_amount = models.DecimalField(bi("Amount on the other account", "المبلغ في الحساب الآخر"), **ACCOUNT_MONEY,
                                    null=True, blank=True)
    counterparty_entity = models.ForeignKey(Entity, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    intercompany_account = models.ForeignKey("ledger.Account", null=True, blank=True, on_delete=models.PROTECT,
                                             related_name="+")
    settles_line = models.ForeignKey("ledger.JournalLine", null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+")
    description_en = models.CharField(bi("Description (English)", "الوصف (بالإنجليزية)"), max_length=300)
    description_ar = models.CharField(bi("Description (Arabic)", "الوصف (بالعربية)"), max_length=300)
    requires_purification = models.BooleanField(bi("Requires purification", "يتطلب التطهير"), default=False)
    status = models.CharField(bi("Status", "الحالة"), max_length=9, choices=Status.choices, default=Status.DRAFT)
    source = models.CharField(bi("Source", "المصدر"), max_length=60, default="human")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    modified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    modified_at = models.DateTimeField(null=True, blank=True)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+")
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    posted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                  related_name="+")
    posted_at = models.DateTimeField(null=True, blank=True)
    discarded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+")
    discarded_at = models.DateTimeField(null=True, blank=True)
    discard_reason = models.CharField(max_length=300, blank=True)
    fx_exchange_rate = models.ForeignKey("ledger.ExchangeRate", null=True, blank=True, on_delete=models.PROTECT,
                                         related_name="+")
    journal_entry = models.ForeignKey("ledger.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                      related_name="+")
    reversal_entry = models.ForeignKey("ledger.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                       related_name="+")

    class Meta:
        verbose_name = bi("bank transaction", "عملية بنكية")
        verbose_name_plural = bi("bank transactions", "العمليات البنكية")
        ordering = ["entity", "date", "number"]
        constraints = [
            models.UniqueConstraint(fields=["entity", "number"], condition=~models.Q(number=""),
                                    name="bank_transaction_number_per_entity"),
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="bank_transaction_amount_positive"),
        ]

    def save(self, *args, **kwargs):
        """R35: a posted document only becomes reversed (with its reversal) or gets its entry once; reversed and
        discarded documents never change (the PostgreSQL trigger repeats this)."""
        stored = _stored_status(self)
        if stored in (self.Status.REVERSED, self.Status.DISCARDED) and _changed_fields(self):
            raise error("This bank transaction is closed and never changes.", "هذه العملية البنكية مقفلة ولا تتغير.",
                        code="document_locked")
        if stored == self.Status.POSTED:
            changed = _changed_fields(self)
            if changed - {"status", "reversal_entry_id", "journal_entry_id"} or self.status not in (
                    self.Status.POSTED, self.Status.REVERSED) or (
                    "journal_entry_id" in changed and type(self)._base_manager.filter(
                        pk=self.pk, journal_entry__isnull=False).exists()):
                raise error("A posted bank transaction is never edited; reverse it instead (R35).",
                            "لا تُعدَّل العملية البنكية المرحّلة؛ اعكسها بدلًا من ذلك.", code="document_locked")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.number or 'DRAFT'} {self.kind} {self.amount} {self.currency_id} ({self.status})"


class BankingSequence(models.Model):
    """Gap-free ``{entity}-BTX-{year}-{000001}`` numbers; the row is locked until the posting commits."""

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    year = models.PositiveIntegerField()
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["entity", "year"], name="banking_sequence_per_year")]


class ReconciliationNote(AppendOnly):
    """Explains an outstanding item (R38). Append-only: a later note on the same item is the current one."""

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    reconciliation = models.ForeignKey(Reconciliation, on_delete=models.PROTECT, related_name="notes")
    statement_line = models.ForeignKey(StatementLine, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    journal_line = models.ForeignKey("ledger.JournalLine", null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+")
    note_en = models.CharField(bi("Note (English)", "الملاحظة (بالإنجليزية)"), max_length=500, blank=True)
    note_ar = models.CharField(bi("Note (Arabic)", "الملاحظة (بالعربية)"), max_length=500, blank=True)
    sharia_exception = models.BooleanField(
        bi("Sharia exception (OQ-7)", "استثناء شرعي (OQ-7)"), default=False,
        help_text=bi("A conventional interest or penalty debit that cannot be booked", "فائدة أو غرامة تقليدية مدينة لا تُقيد"),
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(statement_line__isnull=False) & models.Q(journal_line__isnull=True))
                | (models.Q(statement_line__isnull=True) & models.Q(journal_line__isnull=False)),
                name="reconciliation_note_one_item",
            ),
            models.CheckConstraint(condition=~models.Q(note_en="", note_ar=""), name="reconciliation_note_text"),
        ]


# --- 3.9 BankingEvent ------------------------------------------------------------------------------------------

class BankingEvent(AppendOnly):
    class Event(models.TextChoices):
        BANK_ACCOUNT_CREATED = "bank_account_created", bi("Bank account created", "إنشاء حساب بنكي")
        BANK_ACCOUNT_CHANGED = "bank_account_changed", bi("Bank account changed", "تعديل حساب بنكي")
        BANK_ACCOUNT_ACTIVATED = "bank_account_activated", bi("Bank account activated", "تفعيل حساب بنكي")
        BANK_ACCOUNT_CLOSED = "bank_account_closed", bi("Bank account closed", "إغلاق حساب بنكي")
        IBAN_VIEWED = "iban_viewed", bi("IBAN viewed", "عرض الآيبان")
        IMPORT_ACCEPTED = "import_accepted", bi("Statement imported", "استيراد كشف")
        IMPORT_REFUSED = "import_refused", bi("Statement refused", "رفض كشف")
        IMPORT_DUPLICATE_FILE = "import_duplicate_file", bi("Statement file already imported", "الملف مستورد مسبقًا")
        DUPLICATES_ACCEPTED = "duplicates_accepted", bi("Duplicate lines accepted", "قبول أسطر مكررة")
        MATCH_PROPOSED = "match_proposed", bi("Match proposed", "اقتراح مطابقة")
        MATCH_CONFIRMED = "match_confirmed", bi("Match confirmed", "تأكيد مطابقة")
        MATCH_REJECTED = "match_rejected", bi("Match rejected", "رفض مطابقة")
        MATCH_UNDONE = "match_undone", bi("Match undone", "إلغاء مطابقة")
        BANK_TRANSACTION_CREATED = "bank_transaction_created", bi("Bank transaction drafted", "إعداد عملية بنكية")
        BANK_TRANSACTION_CHANGED = "bank_transaction_changed", bi("Bank transaction changed", "تعديل عملية بنكية")
        BANK_TRANSACTION_SUBMITTED = "bank_transaction_submitted", bi("Bank transaction submitted", "تقديم عملية بنكية")
        BANK_TRANSACTION_APPROVED = "bank_transaction_approved", bi("Bank transaction approved and posted",
                                                                    "اعتماد عملية بنكية وترحيلها")
        BANK_TRANSACTION_DISCARDED = "bank_transaction_discarded", bi("Bank transaction discarded", "إلغاء عملية بنكية")
        BANK_TRANSACTION_REVERSED = "bank_transaction_reversed", bi("Bank transaction reversed", "عكس عملية بنكية")
        RECONCILIATION_PREPARED = "reconciliation_prepared", bi("Reconciliation prepared", "إعداد تسوية")
        RECONCILIATION_NOTED = "reconciliation_noted", bi("Reconciliation note added", "إضافة ملاحظة تسوية")
        RECONCILIATION_SUBMITTED = "reconciliation_submitted", bi("Reconciliation submitted", "تقديم تسوية")
        RECONCILIATION_APPROVED = "reconciliation_approved", bi("Reconciliation approved", "اعتماد تسوية")
        RECONCILIATION_REOPENED = "reconciliation_reopened", bi("Reconciliation reopened", "إعادة فتح تسوية")
        SETTINGS_CHANGED = "settings_changed", bi("Settings changed", "تغيير الإعدادات")
        AGENT_READ = "agent_read", bi("Read by an AI agent", "اطلاع وكيل ذكاء اصطناعي")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    at = models.DateTimeField(default=timezone.now)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                              related_name="+")
    actor_kind = models.CharField(max_length=6, default="human")
    event = models.CharField(max_length=30, choices=Event.choices)
    object_type = models.CharField(max_length=60, blank=True)
    object_id = models.PositiveBigIntegerField(null=True, blank=True)
    details = models.JSONField(default=dict, blank=True)
    request_id = models.CharField(max_length=64, blank=True)
    source = models.CharField(max_length=60, blank=True)

    class Meta:
        verbose_name = bi("banking event", "حدث بنكي")
        verbose_name_plural = bi("banking events", "سجل الأحداث البنكية")
        ordering = ["at", "id"]

    def __str__(self):
        return f"{self.at:%Y-%m-%d %H:%M} {self.event} {self.object_type}#{self.object_id}"
