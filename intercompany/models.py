"""Intercompany billing. Spec: docs/specs/intercompany-billing.md.

Rules enforced on the models (the services in intercompany.services are the only supported way to
change state; every change of status goes through them):
- R4  one invoice per agreement and billing period (DB constraint on live invoices).
- R5  billed timesheet entries are locked.
- R6  a billed cost pool period is locked.
- R13 issued invoices and credit notes are never edited or deleted; corrections are credit notes.
- R16 mirror bills: only expense account and cost centre may change, never amounts, dates or VAT.
- R18 there is no interest, finance charge or late-payment penalty field anywhere (D11).
- R22 zatca_status is written only by the ZATCA module (flag `_zatca`).
Bulk update()/delete() is refused on every document model: it would bypass these locks.
"""
from contextlib import contextmanager
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from access.errors import RuleError
from core.models import Entity
from ledger.models import Account, FiscalPeriod, JournalEntry

ZERO = Decimal("0")
MONEY = {"max_digits": 18, "decimal_places": 2}


class Source(models.TextChoices):
    """D10: who or what created the record."""

    HUMAN = "human", "Human"
    IMPORT = "import", "Import / system"
    AGENT = "agent", "AI agent"
    INTEGRATION = "integration", "Integration"


#: Sources that carry a name in source_agent (agent or integration name).
NAMED_SOURCES = ("agent", "integration")

#: ZATCA VAT exemption reason codes (VATEX) allowed per VAT category (Z / E / O).
VATEX_CODES = {
    "Z": {"VATEX-SA-32", "VATEX-SA-33", "VATEX-SA-34-1", "VATEX-SA-34-2", "VATEX-SA-34-3", "VATEX-SA-34-4",
          "VATEX-SA-34-5", "VATEX-SA-35", "VATEX-SA-36", "VATEX-SA-EDU", "VATEX-SA-HEA", "VATEX-SA-MLTRY"},
    "E": {"VATEX-SA-29", "VATEX-SA-29-7", "VATEX-SA-30"},
    "O": {"VATEX-SA-OOS"},
}
TREATMENT_CATEGORY = {"standard": "S", "zero_rated": "Z", "exempt": "E", "out_of_scope": "O"}


def vatex_problem(treatment, code):
    """Return an error text when the VATEX code does not fit the VAT treatment, else None."""
    category = TREATMENT_CATEGORY.get(treatment)
    if category == "S":
        return "Standard-rated supplies have no exemption code. / لا يوجد رمز إعفاء للتوريدات الخاضعة للنسبة الأساسية." \
            if code else None
    if category and code not in VATEX_CODES[category]:
        return (f"A ZATCA exemption code (VATEX) for category {category} is required. / "
                f"رمز الإعفاء من زاتكا مطلوب للفئة {category}.")
    return None


def locked_error(message_en, message_ar, detail=None):
    return RuleError("document_locked", message_en, message_ar, detail=detail)


@contextmanager
def workflow(*objs, flag="_workflow"):
    """The only way services move workflow fields; models refuse those changes without the flag."""
    for o in objs:
        setattr(o, flag, True)
    try:
        yield
    finally:
        for o in objs:
            setattr(o, flag, False)


class GuardedQuerySet(models.QuerySet):
    """Bulk update/delete would skip the per-row locks; services change one row at a time."""

    def update(self, **kwargs):
        raise RuleError("bulk_change_refused", "Bulk changes are not allowed on intercompany documents.",
                        "التعديل الجماعي غير مسموح على مستندات المجموعة.")

    def delete(self):
        raise RuleError("bulk_change_refused", "Bulk deletion is not allowed on intercompany documents.",
                        "الحذف الجماعي غير مسموح على مستندات المجموعة.")


class Sourced(models.Model):
    """D10 provenance on every record: source (human / import / agent) and source_agent for agents."""

    source = models.CharField(max_length=12, choices=Source.choices, default=Source.HUMAN)
    source_agent = models.CharField(max_length=60, blank=True,
                                    help_text="Agent or integration name; required when source is agent or integration")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="+", help_text="Empty for records the system creates (mirror bills)")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = GuardedQuerySet.as_manager()

    class Meta:
        abstract = True
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(source__in=NAMED_SOURCES) & ~models.Q(source_agent=""))
                | (~models.Q(source__in=NAMED_SOURCES) & models.Q(source_agent="")),
                name="%(app_label)s_%(class)s_source_agent",
            )
        ]

    @property
    def source_label(self):
        """Approvals / audit format: human, import, agent:<name> or integration:<name> (approvals spec D8)."""
        return f"{self.source}:{self.source_agent}" if self.source in NAMED_SOURCES else self.source

    def _stored(self):
        if self._state.adding or not self.pk:
            return None
        return type(self)._base_manager.filter(pk=self.pk).first()

    def _changed(self, stored):
        return {f.attname for f in self._meta.concrete_fields if getattr(stored, f.attname) != getattr(self, f.attname)}


def _validate_bilingual(obj, *bases):
    """R21: every printed text has English and Arabic."""
    errors = {}
    for base in bases:
        for lang in ("en", "ar"):
            name = f"{base}_{lang}"
            if not (getattr(obj, name, "") or "").strip():
                errors[name] = "English and Arabic text are both required. / النص العربي والإنجليزي مطلوبان."
    return errors


# ---------------------------------------------------------------- reference data


class ServiceType(models.Model):
    """Group-wide reference data (spec 3.2)."""

    class Category(models.TextChoices):
        SERVICE = "service", "Shared service"
        RENTAL = "rental", "Asset rental"
        IP = "ip", "IP licence"
        SECONDMENT = "secondment", "Secondment"
        CONSULTATION = "consultation", "Consultation"

    code = models.CharField(max_length=20, unique=True)
    name_en = models.CharField(max_length=200)
    name_ar = models.CharField(max_length=200)
    category = models.CharField(max_length=20, choices=Category.choices)
    income_account_code = models.CharField(max_length=20, help_text="Issuer default, e.g. 4210 / 4110 / 4300")
    expense_account_code = models.CharField(max_length=20, help_text="Receiver default (spec 5.6)")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["code"]

    def clean(self):
        errors = _validate_bilingual(self, "name")
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.code} {self.name_en}"


class VatRate(models.Model):
    """Tax settings: the standard VAT rate by date (R8: rate comes from tax settings at supply date).
    Group-wide (all entities are in KSA today). The ZATCA / tax module takes this over later."""

    country = models.CharField(max_length=2, default="SA")
    rate = models.DecimalField(max_digits=5, decimal_places=2)
    valid_from = models.DateField()
    valid_to = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["country", "valid_from"]
        unique_together = [("country", "valid_from")]

    def __str__(self):
        return f"{self.country} {self.rate}% from {self.valid_from}"


class NumberSequence(models.Model):
    """Gap-free numbering per entity, document prefix and year (rows locked while numbering)."""

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    prefix = models.CharField(max_length=10)
    year = models.PositiveIntegerField()
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = [("entity", "prefix", "year")]


class DraftEditor(models.Model):
    """Every user who created or edited an intercompany draft (same idea as ledger.JournalEntryEditor,
    review A). Editors count as creators for segregation of duties (approvals R8, D10). Append-only."""

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    object_type = models.CharField(max_length=60)
    object_id = models.CharField(max_length=40)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    action = models.CharField(max_length=10, choices=[("create", "Create"), ("edit", "Edit")])
    at = models.DateTimeField(auto_now_add=True)

    objects = GuardedQuerySet.as_manager()

    class Meta:
        ordering = ["object_type", "object_id", "at", "id"]
        indexes = [models.Index(fields=["object_type", "object_id"])]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise locked_error("Editor records cannot be changed.", "لا يمكن تعديل سجلات المحررين.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise locked_error("Editor records cannot be deleted.", "لا يمكن حذف سجلات المحررين.")


# ---------------------------------------------------------------- cost pools (3.5, D12)


class CostPool(Sourced):
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="ic_cost_pools")
    code = models.CharField(max_length=20)
    name_en = models.CharField(max_length=200)
    name_ar = models.CharField(max_length=200)
    accounts = models.ManyToManyField(Account, blank=True, related_name="+",
                                      help_text="Expense accounts of the same entity; never Sharia-prohibited (R7)")
    is_active = models.BooleanField(default=True)

    class Meta(Sourced.Meta):
        unique_together = [("entity", "code")]
        ordering = ["entity", "code"]

    def clean(self):
        errors = _validate_bilingual(self, "name")
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.entity.code} {self.code}"


class CostPoolPeriod(Sourced):
    """Pool cost of one fiscal period, computed from posted ledger lines (R6, R7). Locked once billed."""

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    cost_pool = models.ForeignKey(CostPool, on_delete=models.PROTECT, related_name="periods")
    period = models.ForeignKey(FiscalPeriod, on_delete=models.PROTECT, related_name="+")
    amount = models.DecimalField(**MONEY, default=ZERO)
    computed_at = models.DateTimeField(null=True, blank=True)
    locked = models.BooleanField(default=False)

    class Meta(Sourced.Meta):
        unique_together = [("cost_pool", "period")]
        ordering = ["cost_pool", "period__start_date"]

    def save(self, *args, **kwargs):
        stored = self._stored()
        if stored is not None:
            changed = self._changed(stored)
            if "locked" in changed and not getattr(self, "_workflow", False):
                raise locked_error("Pool locks change only through billing.", "يتغير قفل المجمع عبر الفوترة فقط.")
            if stored.locked and changed - {"locked"}:
                raise locked_error("This cost pool period was billed and is locked (R6).",
                                   "تمت فوترة فترة مجمع التكاليف هذه وهي مقفلة.")
        elif self.locked:
            raise locked_error("New pool periods start unlocked.", "تبدأ فترات المجمع غير مقفلة.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise locked_error("Cost pool periods are kept for audit.", "تُحفظ فترات مجمع التكاليف للتدقيق.")


class AllocationDriver(Sourced):
    """D12: headcount or revenue of each entity sharing a pool in a period. The share billed to a
    counterparty is its value divided by the sum of all rows of the pool, period and basis (including
    the issuer's own row, which stays unbilled; see Build notes)."""

    class Basis(models.TextChoices):
        HEADCOUNT = "headcount", "Headcount"
        REVENUE = "revenue", "Revenue"

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    cost_pool = models.ForeignKey(CostPool, on_delete=models.PROTECT, related_name="drivers")
    period = models.ForeignKey(FiscalPeriod, on_delete=models.PROTECT, related_name="+")
    basis = models.CharField(max_length=10, choices=Basis.choices)
    target_entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+",
                                      help_text="Entity whose headcount or revenue this is (may be the issuer)")
    value = models.DecimalField(**MONEY)

    class Meta(Sourced.Meta):
        unique_together = [("cost_pool", "period", "basis", "target_entity")]

    def clean(self):
        if self.value is not None and self.value < ZERO:
            raise ValidationError({"value": "Driver values cannot be negative. / لا يمكن أن تكون القيمة سالبة."})

    def save(self, *args, **kwargs):
        if CostPoolPeriod.objects.filter(cost_pool_id=self.cost_pool_id, period_id=self.period_id, locked=True).exists():
            raise locked_error("The pool period was billed; its drivers are locked.",
                               "تمت فوترة فترة المجمع؛ أسس التوزيع مقفلة.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise locked_error("Drivers are kept for audit; set the value instead.", "تُحفظ أسس التوزيع للتدقيق.")


# ---------------------------------------------------------------- agreements (3.3)


def needs_transfer_pricing(counterparty):
    """Counterparties outside the zakat consolidation group: associates, unconsolidated or partly owned."""
    return counterparty.consolidation in ("equity", "none") or counterparty.ownership_pct < Decimal("100")


class ServiceAgreement(Sourced):
    class Pricing(models.TextChoices):
        TIMESHEET = "timesheet", "Timesheet (hours x rate)"
        FIXED_FEE = "fixed_fee", "Fixed fee per period"
        COST_PLUS = "cost_plus", "Cost plus markup"

    class AllocationBasis(models.TextChoices):
        FIXED_PCT = "fixed_pct", "Fixed percentage"
        HEADCOUNT = "headcount", "Headcount"
        REVENUE = "revenue", "Revenue"

    class VatTreatment(models.TextChoices):
        STANDARD = "standard", "Standard rate"
        ZERO_RATED = "zero_rated", "Zero-rated"
        EXEMPT = "exempt", "Exempt"
        OUT_OF_SCOPE = "out_of_scope", "Out of scope"

    class Frequency(models.TextChoices):
        MONTHLY = "monthly", "Monthly"
        QUARTERLY = "quarterly", "Quarterly"
        ANNUAL = "annual", "Annual"
        ONE_OFF = "one_off", "One-off"

    class TpMethod(models.TextChoices):
        CUP = "cup", "Comparable uncontrolled price"
        COST_PLUS = "cost_plus", "Cost plus"
        TNMM = "tnmm", "Transactional net margin"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PENDING_APPROVAL = "pending_approval", "Pending approval"
        ACTIVE = "active", "Active"
        SUSPENDED = "suspended", "Suspended"
        ENDED = "ended", "Ended"

    #: Fields each pricing method needs (R2); every other pricing field must be empty.
    PRICING_FIELDS = {
        Pricing.TIMESHEET: {"hourly_rate"},
        Pricing.FIXED_FEE: {"fixed_fee"},
        Pricing.COST_PLUS: {"markup_pct", "cost_pool", "allocation_basis"},
    }
    ALL_PRICING_FIELDS = {"hourly_rate", "fixed_fee", "markup_pct", "cost_pool", "allocation_basis", "allocation_pct"}
    #: income/expense codes are copied from the service type at approval (review 5).
    WORKFLOW_FIELDS = {"status", "activated_at", "end_date", "income_account_code", "expense_account_code"}

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="ic_agreements_issued")
    counterparty = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="ic_agreements_received")
    number = models.CharField(max_length=40, help_text="<ENTITY>-AGR-<YYYY>-<nnnn>")
    service_type = models.ForeignKey(ServiceType, on_delete=models.PROTECT, related_name="agreements")
    description_en = models.TextField()
    description_ar = models.TextField()
    pricing_method = models.CharField(max_length=10, choices=Pricing.choices)
    currency = models.CharField(max_length=3, help_text="Issuer base currency (Q10 open: no foreign currency yet)")
    hourly_rate = models.DecimalField(**MONEY, null=True, blank=True)
    fixed_fee = models.DecimalField(**MONEY, null=True, blank=True, help_text="Per billing period")
    markup_pct = models.DecimalField(max_digits=6, decimal_places=3, null=True, blank=True)
    cost_pool = models.ForeignKey(CostPool, null=True, blank=True, on_delete=models.PROTECT, related_name="agreements")
    allocation_basis = models.CharField(max_length=10, choices=AllocationBasis.choices, blank=True, help_text="D12")
    allocation_pct = models.DecimalField(max_digits=6, decimal_places=3, null=True, blank=True,
                                         help_text="Share of the pool for basis fixed_pct")
    vat_treatment = models.CharField(max_length=12, choices=VatTreatment.choices)
    vat_exemption_reason_en = models.CharField(max_length=300, blank=True,
                                               help_text="Required unless VAT is standard (ZATCA needs a reason)")
    vat_exemption_reason_ar = models.CharField(max_length=300, blank=True)
    vat_exemption_code = models.CharField(max_length=20, blank=True,
                                          help_text="ZATCA VATEX code; required unless VAT is standard")
    billing_frequency = models.CharField(max_length=10, choices=Frequency.choices)
    payment_terms_days = models.PositiveIntegerField()
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    income_account_code = models.CharField(max_length=20, blank=True, help_text="Overrides the service type")
    expense_account_code = models.CharField(max_length=20, blank=True, help_text="Overrides the service type")
    tp_method = models.CharField(max_length=10, choices=TpMethod.choices, blank=True)
    tp_documentation_ref = models.CharField(max_length=200, blank=True,
                                            help_text="Reference to the transfer-pricing file (document module later)")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    activated_at = models.DateTimeField(null=True, blank=True)
    source_type = models.CharField(max_length=60, blank=True, help_text="Feeding module, e.g. rental.lease (D9)")
    source_ref = models.CharField(max_length=100, blank=True)

    class Meta(Sourced.Meta):
        unique_together = [("entity", "number")]
        ordering = ["entity", "number"]

    def clean(self):
        errors = _validate_bilingual(self, "description")
        P, F = self.Pricing, self.Frequency
        # R1
        if self.entity_id and self.counterparty_id:
            if self.entity_id == self.counterparty_id:
                errors["counterparty"] = "The counterparty must be another entity. / يجب أن يكون الطرف الآخر منشأة أخرى."
            elif not (self.entity.is_active and self.counterparty.is_active):
                errors["counterparty"] = "Both entities must be active. / يجب أن تكون المنشأتان نشطتين."
            elif self.counterparty.base_currency != self.entity.base_currency:
                errors["counterparty"] = ("Counterparties with another base currency are not supported yet (Q10). / "
                                          "الأطراف بعملة أساس مختلفة غير مدعومة بعد.")
        if self.entity_id and self.currency and self.currency != self.entity.base_currency:
            errors["currency"] = "Agreements are in the issuer's base currency (Q10). / الاتفاقيات بعملة المُصدر."
        # R2
        needed = set(self.PRICING_FIELDS.get(self.pricing_method, set()))
        if self.pricing_method == P.COST_PLUS and self.allocation_basis == self.AllocationBasis.FIXED_PCT:
            needed.add("allocation_pct")
        for name in sorted(self.ALL_PRICING_FIELDS):
            value = getattr(self, f"{name}_id" if name == "cost_pool" else name)
            present = value not in (None, "")
            if name in needed and not present:
                errors[name] = f"Required for {self.pricing_method} pricing. / مطلوب لطريقة التسعير هذه."
            elif name not in needed and present:
                errors[name] = f"Not used by {self.pricing_method} pricing; leave it empty. / غير مستخدم لهذه الطريقة."
        if self.hourly_rate is not None and self.hourly_rate <= ZERO:
            errors["hourly_rate"] = "The hourly rate must be above zero. / يجب أن يكون سعر الساعة أكبر من صفر."
        if self.fixed_fee is not None and self.fixed_fee <= ZERO:
            errors["fixed_fee"] = "The fee must be above zero. / يجب أن تكون الرسوم أكبر من صفر."
        if self.markup_pct is not None and self.markup_pct < ZERO:
            errors["markup_pct"] = "The markup cannot be negative. / لا يمكن أن يكون هامش الربح سالباً."
        if self.allocation_pct is not None and not (ZERO < self.allocation_pct <= Decimal("100")):
            errors["allocation_pct"] = "The allocation must be above 0% and at most 100%. / النسبة بين 0 و100."
        if self.cost_pool_id and self.entity_id and self.cost_pool.entity_id != self.entity_id:
            errors["cost_pool"] = "The cost pool must belong to the issuer. / يجب أن يتبع المجمع المُصدر."
        if self.pricing_method == P.COST_PLUS and self.billing_frequency and self.billing_frequency != F.MONTHLY:
            errors["billing_frequency"] = "Cost-plus agreements are billed monthly. / تُفوتر اتفاقيات التكلفة زائد شهرياً."
        if self.billing_frequency == F.ONE_OFF and self.pricing_method != P.FIXED_FEE:
            errors["billing_frequency"] = "One-off billing is for fixed fees only. / الفوترة لمرة واحدة للرسوم الثابتة."
        if self.vat_treatment and self.vat_treatment != self.VatTreatment.STANDARD:
            errors.update(_validate_bilingual(self, "vat_exemption_reason"))
        if self.vat_treatment and (problem := vatex_problem(self.vat_treatment, self.vat_exemption_code)):
            errors["vat_exemption_code"] = problem
        if self.counterparty_id and needs_transfer_pricing(self.counterparty):
            if not self.tp_method:
                errors["tp_method"] = ("A transfer-pricing method is required for counterparties outside the zakat "
                                       "consolidation group. / طريقة تسعير التحويل مطلوبة لهذا الطرف.")
            if not (self.tp_documentation_ref or "").strip():
                errors["tp_documentation_ref"] = ("A benchmark / TP file reference is required for this counterparty. / "
                                                  "مرجع دراسة المقارنة مطلوب لهذا الطرف.")
        if self.end_date and self.start_date and self.end_date < self.start_date:
            errors["end_date"] = "The agreement ends before it starts. / تنتهي الاتفاقية قبل بدايتها."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        stored = self._stored()
        if stored is not None:
            changed = self._changed(stored)
            wf = getattr(self, "_workflow", False)
            if wf:
                if changed - self.WORKFLOW_FIELDS:
                    raise locked_error("Workflow may change only status and dates.", "سير العمل يغير الحالة فقط.")
            elif stored.status != self.Status.DRAFT and changed:
                raise locked_error("Only draft agreements can be edited. Create a new agreement instead.",
                                   "يمكن تعديل مسودات الاتفاقيات فقط. أنشئ اتفاقية جديدة.", detail=sorted(changed))
            elif changed & ({"status", "activated_at", "number", "entity_id", "source", "source_agent", "created_by_id"}):
                raise locked_error("Status and numbering change only through intercompany services.",
                                   "تتغير الحالة والترقيم عبر خدمات المجموعة فقط.")
        elif self.status != self.Status.DRAFT:
            raise locked_error("New agreements start as drafts.", "تبدأ الاتفاقيات كمسودات.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise locked_error("Agreements are never deleted; end them instead.", "لا تُحذف الاتفاقيات؛ أنهِها بدلاً من ذلك.")

    def __str__(self):
        return f"{self.number} {self.entity.code}->{self.counterparty.code}"


class AgreementSuspension(Sourced):
    """A period in which an agreement was suspended (re-review A). Suspended days are never billed;
    an open suspension (no end date) runs until the agreement is resumed. Only the end date is ever set,
    once, when an approval resumes the agreement."""

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    agreement = models.ForeignKey("ServiceAgreement", on_delete=models.PROTECT, related_name="suspensions")
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True, help_text="Last suspended day; empty while suspended")
    reason = models.CharField(max_length=300)

    class Meta(Sourced.Meta):
        ordering = ["agreement", "start_date", "id"]

    def save(self, *args, **kwargs):
        stored = self._stored()
        if stored is not None:
            changed = self._changed(stored)
            if changed - {"end_date"} or stored.end_date is not None or not getattr(self, "_workflow", False):
                raise locked_error("Suspension periods change only when the agreement is resumed.",
                                   "تتغير فترات الإيقاف عند استئناف الاتفاقية فقط.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise locked_error("Suspension periods are kept for audit.", "تُحفظ فترات الإيقاف للتدقيق.")


class AgreementRate(models.Model):
    """Optional per-role hourly rate (3.3). Pricing data: editable only while the agreement is a draft."""

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    agreement = models.ForeignKey(ServiceAgreement, on_delete=models.PROTECT, related_name="rates")
    role_en = models.CharField(max_length=200)
    role_ar = models.CharField(max_length=200)
    hourly_rate = models.DecimalField(**MONEY)
    valid_from = models.DateField()
    valid_to = models.DateField(null=True, blank=True)

    objects = GuardedQuerySet.as_manager()

    def clean(self):
        errors = _validate_bilingual(self, "role")
        if self.hourly_rate is not None and self.hourly_rate <= ZERO:
            errors["hourly_rate"] = "The hourly rate must be above zero. / يجب أن يكون سعر الساعة أكبر من صفر."
        if self.valid_to and self.valid_from and self.valid_to < self.valid_from:
            errors["valid_to"] = "Ends before it starts. / ينتهي قبل أن يبدأ."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if ServiceAgreement.objects.filter(pk=self.agreement_id).exclude(status=ServiceAgreement.Status.DRAFT).exists():
            raise locked_error("Rates of an approved agreement are locked.", "أسعار الاتفاقية المعتمدة مقفلة.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if ServiceAgreement.objects.filter(pk=self.agreement_id).exclude(status=ServiceAgreement.Status.DRAFT).exists():
            raise locked_error("Rates of an approved agreement are locked.", "أسعار الاتفاقية المعتمدة مقفلة.")
        return super().delete(*args, **kwargs)

    def covers(self, day):
        return self.valid_from <= day and (self.valid_to is None or day <= self.valid_to)


# ---------------------------------------------------------------- timesheets (3.4) and billable items (3.6)


class TimesheetEntry(Sourced):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTED = "submitted", "Submitted"
        APPROVED = "approved", "Approved"
        BILLED = "billed", "Billed"
        REJECTED = "rejected", "Rejected"

    WORKFLOW_FIELDS = {"status", "invoice_line_id", "approved_by_id", "approved_at"}

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    agreement = models.ForeignKey(ServiceAgreement, on_delete=models.PROTECT, related_name="timesheets")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="ic_timesheets")
    rate = models.ForeignKey(AgreementRate, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    date = models.DateField()
    hours = models.DecimalField(max_digits=6, decimal_places=2)
    description_en = models.CharField(max_length=300)
    description_ar = models.CharField(max_length=300)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    invoice_line = models.ForeignKey("InvoiceLine", null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="timesheets")
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta(Sourced.Meta):
        verbose_name_plural = "timesheet entries"
        ordering = ["entity", "date", "id"]

    def clean(self):
        errors = _validate_bilingual(self, "description")
        a = self.agreement if self.agreement_id else None
        if a is not None:
            if a.pricing_method != ServiceAgreement.Pricing.TIMESHEET:
                errors["agreement"] = "The agreement is not priced by timesheet. / الاتفاقية لا تُسعّر بالساعات."
            if self.entity_id and a.entity_id != self.entity_id:
                errors["entity"] = "Timesheets belong to the issuing entity. / سجلات الوقت للمنشأة المُصدرة."
            if self.date and (self.date < a.start_date or (a.end_date and self.date > a.end_date)):
                errors["date"] = "The date is outside the agreement dates. / التاريخ خارج مدة الاتفاقية."
        if self.rate_id:
            if self.rate.agreement_id != self.agreement_id:
                errors["rate"] = "The rate belongs to another agreement. / السعر لاتفاقية أخرى."
            elif self.date and not self.rate.covers(self.date):
                errors["rate"] = "The rate is not valid on this date. / السعر غير سارٍ في هذا التاريخ."
        if self.hours is not None:
            if self.hours <= ZERO:
                errors["hours"] = "Hours must be above zero. / يجب أن تكون الساعات أكبر من صفر."
            elif self.user_id and self.date:
                others = (TimesheetEntry.objects.filter(user_id=self.user_id, date=self.date)
                          .exclude(status=self.Status.REJECTED).exclude(pk=self.pk)
                          .aggregate(total=models.Sum("hours"))["total"] or ZERO)
                if others + self.hours > Decimal("24"):
                    errors["hours"] = "More than 24 hours for this person on this day. / أكثر من 24 ساعة في اليوم."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        stored = self._stored()
        wf = getattr(self, "_workflow", False)
        if stored is None:
            if self.status != self.Status.DRAFT or self.invoice_line_id:
                raise locked_error("New timesheet entries start as drafts.", "تبدأ سجلات الوقت كمسودات.")
        else:
            changed = self._changed(stored)
            if stored.status == self.Status.BILLED and changed and not (wf and changed <= self.WORKFLOW_FIELDS):
                raise locked_error("Billed timesheet entries are locked (R5).", "سجلات الوقت المفوترة مقفلة.")
            if wf and changed - self.WORKFLOW_FIELDS:
                raise locked_error("Workflow may change only the status.", "سير العمل يغير الحالة فقط.")
            if not wf and (changed & self.WORKFLOW_FIELDS or (stored.status != self.Status.DRAFT and changed)):
                raise locked_error("Only draft timesheet entries can be edited.", "يمكن تعديل مسودات سجلات الوقت فقط.",
                                   detail=sorted(changed))
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self._stored() is None or self._stored().status != self.Status.DRAFT:
            raise locked_error("Only draft timesheet entries can be deleted.", "يمكن حذف مسودات سجلات الوقت فقط.")
        return super().delete(*args, **kwargs)


class BillableItem(Sourced):
    """Generic feed of billable lines from other modules (D9; later rental leases, fuel/mileage)."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        BILLED = "billed", "Billed"

    WORKFLOW_FIELDS = {"status", "invoice_line_id"}

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    agreement = models.ForeignKey(ServiceAgreement, on_delete=models.PROTECT, related_name="billable_items")
    period_start = models.DateField()
    period_end = models.DateField()
    description_en = models.CharField(max_length=300)
    description_ar = models.CharField(max_length=300)
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    unit_price = models.DecimalField(max_digits=18, decimal_places=4)
    source_type = models.CharField(max_length=60, blank=True)
    source_ref = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    invoice_line = models.ForeignKey("InvoiceLine", null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="billable_items")

    class Meta(Sourced.Meta):
        ordering = ["entity", "period_start", "id"]

    def clean(self):
        errors = _validate_bilingual(self, "description")
        if self.agreement_id and self.entity_id and self.agreement.entity_id != self.entity_id:
            errors["entity"] = "Items belong to the issuing entity. / البنود للمنشأة المُصدرة."
        if self.period_end and self.period_start and self.period_end < self.period_start:
            errors["period_end"] = "The period ends before it starts. / تنتهي الفترة قبل بدايتها."
        if self.quantity is not None and self.quantity <= ZERO:
            errors["quantity"] = "The quantity must be above zero. / يجب أن تكون الكمية أكبر من صفر."
        if self.unit_price is not None and self.unit_price < ZERO:
            errors["unit_price"] = "The price cannot be negative. / لا يمكن أن يكون السعر سالباً."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        stored = self._stored()
        wf = getattr(self, "_workflow", False)
        if stored is None:
            if self.status != self.Status.OPEN or self.invoice_line_id:
                raise locked_error("New items start open.", "تبدأ البنود مفتوحة.")
        else:
            changed = self._changed(stored)
            if wf and changed - self.WORKFLOW_FIELDS:
                raise locked_error("Workflow may change only the status.", "سير العمل يغير الحالة فقط.")
            if not wf and (changed & self.WORKFLOW_FIELDS or (stored.status != self.Status.OPEN and changed)):
                raise locked_error("Billed items are locked.", "البنود المفوترة مقفلة.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise locked_error("Billable items are kept for audit.", "تُحفظ البنود للتدقيق.")


# ---------------------------------------------------------------- billing runs (3.9)


class BillingRun(Sourced):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    period = models.ForeignKey(FiscalPeriod, on_delete=models.PROTECT, related_name="+")
    run_date = models.DateField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RUNNING)
    invoices_created = models.PositiveIntegerField(default=0)
    log = models.JSONField(default=list, blank=True)

    class Meta(Sourced.Meta):
        ordering = ["-created_at", "-id"]

    @property
    def started_by(self):
        return self.created_by

    def delete(self, *args, **kwargs):
        raise locked_error("Billing runs are kept for audit.", "تُحفظ عمليات الفوترة للتدقيق.")


# ---------------------------------------------------------------- invoices and credit notes (3.7)


class IntercompanyInvoice(Sourced):
    class Kind(models.TextChoices):
        INVOICE = "invoice", "Invoice"
        CREDIT_NOTE = "credit_note", "Credit note"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PENDING_APPROVAL = "pending_approval", "Pending approval"
        APPROVED = "approved", "Approved"
        ISSUED = "issued", "Issued (posted)"
        REJECTED = "rejected", "Rejected"
        CANCELLED_DRAFT = "cancelled_draft", "Cancelled draft"

    class ZatcaStatus(models.TextChoices):
        NOT_SUBMITTED = "not_submitted", "Not submitted"
        SUBMITTED = "submitted", "Submitted"
        REPORTED = "reported", "Reported"
        CLEARED = "cleared", "Cleared"
        REJECTED = "rejected", "Rejected by ZATCA"

    DEAD = (Status.CANCELLED_DRAFT, Status.REJECTED)
    WORKFLOW_FIELDS = {"status", "number", "journal_entry_id", "issued_at", "approved_by_id", "zatca_uuid",
                       "zatca_ready", "seller_vat_number", "buyer_vat_number", "issue_date", "due_date",
                       "deadline_alerted_at"}
    ZATCA_FIELDS = {"zatca_status", "zatca_hash", "zatca_updated_at"}

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="ic_invoices_issued")
    counterparty = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="ic_invoices_received")
    agreement = models.ForeignKey(ServiceAgreement, on_delete=models.PROTECT, related_name="invoices")
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.INVOICE)
    credit_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="credit_notes")
    credit_reason_en = models.CharField(max_length=300, blank=True)
    credit_reason_ar = models.CharField(max_length=300, blank=True)
    trigger_date = models.DateField(null=True, blank=True, help_text="Credit notes: date of the event (D13)")
    late_credit_warning = models.BooleanField(
        default=False, help_text="Credit note created after the 15th of the month following the trigger month")
    deadline_alerted_at = models.DateTimeField(null=True, blank=True, help_text="15-day issue alert sent (D13)")
    number = models.CharField(max_length=40, blank=True, help_text="Assigned on issue; gap-free per entity and year")
    issue_date = models.DateField(help_text="D13: provisional on drafts; set to the actual issue date on issue")
    supply_date_from = models.DateField()
    supply_date_to = models.DateField()
    due_date = models.DateField()
    billing_period = models.CharField(max_length=7, help_text="YYYY-MM, YYYY-Qn or YYYY")
    currency = models.CharField(max_length=3)
    net_total = models.DecimalField(**MONEY, default=ZERO)
    vat_total = models.DecimalField(**MONEY, default=ZERO)
    gross_total = models.DecimalField(**MONEY, default=ZERO)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    journal_entry = models.OneToOneField(JournalEntry, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    billing_run = models.ForeignKey(BillingRun, null=True, blank=True, on_delete=models.PROTECT, related_name="invoices")
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", help_text="Final approver (approvals engine)")
    issued_at = models.DateTimeField(null=True, blank=True)
    # ZATCA hand-off (separate module). This app sets zatca_ready/zatca_uuid at issue; only the ZATCA
    # module writes zatca_status / zatca_hash (R22).
    invoice_type_code = models.CharField(max_length=3, help_text="388 tax invoice, 381 credit note")
    seller_vat_number = models.CharField(max_length=15, blank=True)
    buyer_vat_number = models.CharField(max_length=15, blank=True)
    zatca_uuid = models.UUIDField(null=True, blank=True)
    zatca_ready = models.BooleanField(default=False, help_text="Issued and posted; ready for the ZATCA module")
    zatca_status = models.CharField(max_length=16, choices=ZatcaStatus.choices, default=ZatcaStatus.NOT_SUBMITTED)
    zatca_hash = models.CharField(max_length=128, blank=True)
    zatca_updated_at = models.DateTimeField(null=True, blank=True)

    class Meta(Sourced.Meta):
        ordering = ["entity", "-issue_date", "-id"]
        constraints = Sourced.Meta.constraints + [
            models.UniqueConstraint(fields=["entity", "number"], condition=~models.Q(number=""),
                                    name="ic_unique_invoice_number_per_entity"),
            # R4: one live invoice per agreement and billing period.
            models.UniqueConstraint(
                fields=["agreement", "billing_period"],
                condition=models.Q(kind="invoice") & ~models.Q(status__in=["cancelled_draft", "rejected"]),
                name="ic_one_invoice_per_agreement_period",
            ),
            models.CheckConstraint(
                condition=(models.Q(kind="invoice") & models.Q(credit_of__isnull=True))
                | (models.Q(kind="credit_note") & models.Q(credit_of__isnull=False)),
                name="ic_credit_note_has_original",
            ),
            models.CheckConstraint(condition=~models.Q(entity=models.F("counterparty")),
                                   name="ic_invoice_counterparty_differs"),
        ]

    @property
    def is_credit_note(self):
        return self.kind == self.Kind.CREDIT_NOTE

    def save(self, *args, **kwargs):
        stored = self._stored()
        wf = getattr(self, "_workflow", False)
        zatca = getattr(self, "_zatca", False)
        if stored is None:
            if self.status != self.Status.DRAFT or self.number or self.journal_entry_id:
                raise locked_error("New invoices start as unnumbered drafts.", "تبدأ الفواتير كمسودات بلا رقم.")
            if self.zatca_status != self.ZatcaStatus.NOT_SUBMITTED:
                raise locked_error("zatca_status is set by the ZATCA module only (R22).", "حالة زاتكا تحددها وحدة زاتكا فقط.")
        else:
            changed = self._changed(stored)
            if changed & self.ZATCA_FIELDS and not zatca:
                raise locked_error("zatca_status is set by the ZATCA module only (R22).", "حالة زاتكا تحددها وحدة زاتكا فقط.")
            if zatca:
                if changed - self.ZATCA_FIELDS or stored.status != self.Status.ISSUED:
                    raise locked_error("The ZATCA module may only update ZATCA fields of issued invoices.",
                                       "تحدّث وحدة زاتكا حقول زاتكا للفواتير الصادرة فقط.")
            elif stored.status == self.Status.ISSUED and changed:
                raise locked_error(
                    "Issued invoices cannot be edited. Raise a credit note instead (R13).",
                    "لا يمكن تعديل الفاتورة الصادرة. أصدر إشعاراً دائناً بدلاً من ذلك.", detail=sorted(changed))
            elif wf:
                if changed - self.WORKFLOW_FIELDS:
                    raise locked_error("Workflow may change only status and issue fields.", "سير العمل يغير الحالة فقط.")
            elif changed & self.WORKFLOW_FIELDS:
                raise locked_error("Status and numbering change only through intercompany services.",
                                   "تتغير الحالة والترقيم عبر خدمات المجموعة فقط.")
            elif stored.status != self.Status.DRAFT and changed:
                raise locked_error("This invoice is in approval or closed and cannot be edited.",
                                   "هذه الفاتورة قيد الاعتماد أو مغلقة ولا يمكن تعديلها.", detail=sorted(changed))
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        stored = self._stored()
        if stored is not None and stored.status == self.Status.ISSUED:
            raise locked_error("Issued invoices cannot be deleted. Raise a credit note instead (R13).",
                               "لا يمكن حذف الفاتورة الصادرة. أصدر إشعاراً دائناً بدلاً من ذلك.")
        raise locked_error("Invoices are never deleted; cancel the draft instead (R14).",
                           "لا تُحذف الفواتير؛ ألغِ المسودة بدلاً من ذلك.")

    def __str__(self):
        return f"{self.number or 'DRAFT'} {self.entity.code}->{self.counterparty.code} {self.gross_total}"


class InvoiceLine(models.Model):
    class VatCategory(models.TextChoices):
        """ZATCA / UNCL5305 category codes."""

        STANDARD = "S", "Standard"
        ZERO = "Z", "Zero-rated"
        EXEMPT = "E", "Exempt"
        OUT_OF_SCOPE = "O", "Out of scope"

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    invoice = models.ForeignKey(IntercompanyInvoice, on_delete=models.PROTECT, related_name="lines")
    agreement = models.ForeignKey(ServiceAgreement, on_delete=models.PROTECT, related_name="+")
    description_en = models.CharField(max_length=300)
    description_ar = models.CharField(max_length=300)
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    unit_price = models.DecimalField(max_digits=18, decimal_places=4)
    net_amount = models.DecimalField(**MONEY)
    vat_treatment = models.CharField(max_length=12, choices=ServiceAgreement.VatTreatment.choices)
    vat_category = models.CharField(max_length=1, choices=VatCategory.choices)
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2)
    vat_amount = models.DecimalField(**MONEY)
    vat_exemption_reason_en = models.CharField(max_length=300, blank=True)
    vat_exemption_reason_ar = models.CharField(max_length=300, blank=True)
    vat_exemption_code = models.CharField(max_length=20, blank=True)
    income_account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="+")
    pricing_basis = models.CharField(max_length=200, blank=True, help_text='e.g. "12.5 h x 450" or "cost 80,000 + 10%"')
    cost_pool_period = models.ForeignKey(CostPoolPeriod, null=True, blank=True, on_delete=models.PROTECT,
                                         related_name="invoice_lines")
    pool_amount = models.DecimalField(**MONEY, null=True, blank=True, help_text="Pool cost this line is based on")
    pool_share = models.DecimalField(max_digits=14, decimal_places=10, null=True, blank=True,
                                     help_text="Share of the pool billed (after pro-rating, D14)")
    adjustment_period = models.CharField(max_length=7, blank=True,
                                         help_text="Adjustment line for an earlier month (late hours / late pool costs)")
    credits_line = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="credited_by",
                                     help_text="Credit notes: the original invoice line")

    objects = GuardedQuerySet.as_manager()

    class Meta:
        ordering = ["invoice", "id"]
        constraints = [
            models.CheckConstraint(condition=models.Q(net_amount__gte=0) & models.Q(vat_amount__gte=0),
                                   name="ic_line_amounts_not_negative"),
        ]

    @property
    def gross_amount(self):
        return self.net_amount + self.vat_amount

    def _invoice_draft(self):
        return IntercompanyInvoice.objects.filter(pk=self.invoice_id, status=IntercompanyInvoice.Status.DRAFT).exists()

    def save(self, *args, **kwargs):
        if not self._invoice_draft():
            raise locked_error("Lines change only while the invoice is a draft (R13).",
                               "تتغير البنود فقط ما دامت الفاتورة مسودة.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise locked_error("Invoice lines are never deleted; cancel the draft instead.", "لا تُحذف بنود الفواتير.")


# ---------------------------------------------------------------- mirror bills (3.8, D8)


class IntercompanyBill(Sourced):
    class Kind(models.TextChoices):
        BILL = "bill", "Bill"
        CREDIT = "credit", "Bill credit"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PENDING_APPROVAL = "pending_approval", "Pending approval"
        APPROVED = "approved", "Approved"
        POSTED = "posted", "Posted"
        DISPUTED = "disputed", "Disputed"

    WORKFLOW_FIELDS = {"status", "journal_entry_id", "dispute_reason", "prepared_by_id"}

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="ic_bills_received")
    counterparty = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="ic_bills_issued")
    invoice = models.OneToOneField(IntercompanyInvoice, on_delete=models.PROTECT, related_name="mirror_bill")
    kind = models.CharField(max_length=6, choices=Kind.choices, default=Kind.BILL)
    credit_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="credits")
    number = models.CharField(max_length=40, help_text="<ENTITY>-ICB-<YYYY>-<nnnnnn> (receiver's sequence)")
    bill_date = models.DateField()
    due_date = models.DateField()
    currency = models.CharField(max_length=3)
    net_total = models.DecimalField(**MONEY)
    vat_total = models.DecimalField(**MONEY)
    gross_total = models.DecimalField(**MONEY)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    dispute_reason = models.CharField(max_length=500, blank=True)
    journal_entry = models.OneToOneField(JournalEntry, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    prepared_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", help_text="Receiver user who reviewed and submitted the bill")

    class Meta(Sourced.Meta):
        ordering = ["entity", "-bill_date", "-id"]
        constraints = Sourced.Meta.constraints + [
            models.UniqueConstraint(fields=["entity", "number"], name="ic_unique_bill_number_per_entity"),
        ]

    def save(self, *args, **kwargs):
        stored = self._stored()
        if stored is None:
            if not getattr(self, "_system", False) or self.status != self.Status.DRAFT:
                raise locked_error("Mirror bills are created by the system only, as drafts (D8).",
                                   "تُنشأ الفواتير المقابلة آلياً كمسودات فقط.")
        else:
            changed = self._changed(stored)
            if changed and not (getattr(self, "_workflow", False) and changed <= self.WORKFLOW_FIELDS):
                raise locked_error(
                    "Mirror bill amounts, dates and VAT cannot change (R16). Dispute the bill instead; "
                    "the issuer corrects it with a credit note.",
                    "لا يمكن تغيير مبالغ الفاتورة المقابلة أو تواريخها أو ضريبتها. اعترض عليها ليصدر المُصدر إشعاراً دائناً.",
                    detail=sorted(changed))
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise locked_error("Mirror bills are never deleted.", "لا تُحذف الفواتير المقابلة.")

    def __str__(self):
        return f"{self.number} {self.entity.code}<-{self.counterparty.code} {self.gross_total}"


class IntercompanyBillLine(models.Model):
    CODING_FIELDS = {"expense_account_id", "cost_centre"}

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    bill = models.ForeignKey(IntercompanyBill, on_delete=models.PROTECT, related_name="lines")
    invoice_line = models.OneToOneField(InvoiceLine, on_delete=models.PROTECT, related_name="mirror_line")
    description_en = models.CharField(max_length=300)
    description_ar = models.CharField(max_length=300)
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    unit_price = models.DecimalField(max_digits=18, decimal_places=4)
    net_amount = models.DecimalField(**MONEY)
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2)
    vat_amount = models.DecimalField(**MONEY)
    expense_account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="+")
    cost_centre = models.CharField(max_length=40, blank=True)

    objects = GuardedQuerySet.as_manager()

    class Meta:
        ordering = ["bill", "id"]

    def clean(self):
        acc = self.expense_account if self.expense_account_id else None
        if acc is not None:
            if acc.entity_id != self.entity_id:
                raise ValidationError({"expense_account": "Use an account of the receiving entity. / استخدم حساباً للمنشأة المستلمة."})
            if acc.type != Account.Type.EXPENSE or not acc.is_postable or not acc.is_active:
                raise ValidationError({"expense_account": "Choose an open expense account. / اختر حساب مصروف مفتوحاً."})
            if acc.sharia_prohibited:
                raise ValidationError({"expense_account": "This account is Sharia-prohibited. / هذا الحساب محظور شرعاً."})

    def save(self, *args, **kwargs):
        if self._state.adding:
            if not getattr(self, "_system", False):
                raise locked_error("Mirror bill lines are created by the system only.", "تُنشأ بنود الفاتورة المقابلة آلياً فقط.")
        else:
            stored = type(self)._base_manager.get(pk=self.pk)
            changed = {f.attname for f in self._meta.concrete_fields if getattr(stored, f.attname) != getattr(self, f.attname)}
            if changed - self.CODING_FIELDS:
                raise locked_error(
                    "Only the expense account and cost centre of a mirror bill can change (R16).",
                    "يمكن تغيير حساب المصروف ومركز التكلفة فقط في الفاتورة المقابلة.", detail=sorted(changed))
            if changed and not IntercompanyBill.objects.filter(
                pk=self.bill_id, status__in=[IntercompanyBill.Status.DRAFT, IntercompanyBill.Status.DISPUTED]
            ).exists():
                raise locked_error("The bill is in approval or posted; its coding is locked.",
                                   "الفاتورة قيد الاعتماد أو مرحّلة؛ الترميز مقفل.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise locked_error("Mirror bill lines are never deleted.", "لا تُحذف بنود الفاتورة المقابلة.")
