"""Fixed-asset register (docs/specs/fixed-assets.md §3, slice 1).

Every model carries ``entity`` (R1, R30: never changes). Records change only through ``fixed_assets.services``: a
service sets ``obj._service_write = True`` right before the one save it makes; any other save is refused, and nothing
is ever deleted (R42; repeated on PostgreSQL by triggers, migration 0002). Money is functional SAR, 2 decimals
(CLAUDE.md rule 7, R39). Bilingual names are ``*_en`` / ``*_ar`` (Arabic required).
"""
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import Entity, bi

from .messages import error

MONEY = dict(max_digits=18, decimal_places=2)
ZERO = Decimal("0")


class AssetClass(models.TextChoices):
    LAND = "land", bi("Land", "أراضٍ")
    BUILDING = "building", bi("Building", "مبانٍ")
    IP_LAND = "ip_land", bi("Investment property: land", "عقار استثماري: أرض")
    IP_BUILDING = "ip_building", bi("Investment property: building", "عقار استثماري: مبنى")
    VEHICLE = "vehicle", bi("Vehicle", "سيارة")
    FURNITURE_OFFICE = "furniture_office", bi("Furniture and office equipment", "أثاث ومعدات مكتبية")
    IT_EQUIPMENT = "it_equipment", bi("IT equipment", "أجهزة حاسب وتقنية")
    MACHINERY = "machinery", bi("Machinery", "آلات ومعدات")
    LEASEHOLD_IMPROVEMENT = "leasehold_improvement", bi("Leasehold improvements", "تحسينات على عقارات مستأجرة")
    CWIP = "cwip", bi("Capital work in progress", "مشروعات تحت التنفيذ")
    OTHER = "other", bi("Other", "أخرى")


NON_DEPRECIABLE_CLASSES = frozenset({AssetClass.LAND, AssetClass.IP_LAND, AssetClass.CWIP})  # R12
IP_CLASSES = frozenset({AssetClass.IP_LAND, AssetClass.IP_BUILDING})
# §3.1 (compliance S2, team default; Needs expert sign-off: tax): real-estate retention class.
REAL_ESTATE_CLASSES = frozenset({AssetClass.LAND, AssetClass.BUILDING, AssetClass.IP_LAND, AssetClass.IP_BUILDING,
                                 AssetClass.LEASEHOLD_IMPROVEMENT, AssetClass.CWIP})


class RetentionClass(models.TextChoices):
    REAL_ESTATE = "capital_asset_real_estate", bi("Capital asset: real estate", "أصل رأسمالي: عقار")
    MOVABLE = "movable_capital_asset", bi("Movable capital asset", "أصل رأسمالي منقول")


class Method(models.TextChoices):
    STRAIGHT_LINE = "straight_line", bi("Straight line", "القسط الثابت")
    REDUCING_BALANCE = "reducing_balance", bi("Reducing balance", "القسط المتناقص")


class Usage(models.TextChoices):
    OWN_USE = "own_use", bi("Own use", "استخدام ذاتي")
    RENTAL = "rental_operating_lease", bi("Rented out (operating lease)", "مؤجر (إيجار تشغيلي)")
    INVESTMENT_PROPERTY = "investment_property", bi("Investment property", "عقار استثماري")


class HeldFor(models.TextChoices):
    OWN_USE = "own_use", bi("Own use", "استخدام ذاتي")
    RENTAL = "rental", bi("Rental", "التأجير")
    CAPITAL_APPRECIATION = "capital_appreciation", bi("Capital appreciation", "ارتفاع القيمة الرأسمالية")
    TRADE = "trade", bi("Trade", "المتاجرة")


class VatUse(models.TextChoices):
    TAXABLE = "taxable", bi("Taxable supplies", "توريدات خاضعة")
    EXEMPT = "exempt", bi("Exempt supplies", "توريدات معفاة")
    MIXED = "mixed", bi("Mixed use", "استخدام مختلط")


# --- base -------------------------------------------------------------------------------------------------------

class KeptQuerySet(models.QuerySet):
    """R42: nothing is deleted, and bulk updates skip the save guard, so neither is allowed."""

    def delete(self):
        raise error("never_deleted")

    def update(self, **kwargs):
        raise error("service_only")


class Kept(models.Model):
    """Saved only by fixed_assets.services (``_service_write``), never deleted, entity never changes (R30, R42)."""

    objects = KeptQuerySet.as_manager()

    class Meta:
        abstract = True

    def _stored(self, *fields):
        if not self.pk:
            return None
        return type(self)._base_manager.filter(pk=self.pk).values(*fields).first()

    def _guard(self, stored):
        """Extra checks of a subclass before a refused direct save (e.g. vat_use, R47)."""

    def save(self, *args, **kwargs):
        allowed, self._service_write = getattr(self, "_service_write", False), False
        stored = self._stored("entity_id") if hasattr(self, "entity_id") else None
        if stored is not None and stored["entity_id"] != self.entity_id:
            raise error("entity_immutable")
        if not allowed:
            self._guard(stored)
            raise error("service_only")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise error("never_deleted")


class Written(Kept):
    """Who wrote a record and when (segregation, access R14 / R35; D8 source)."""

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
                                   verbose_name=bi("Created by", "أنشأه"))
    created_at = models.DateTimeField(bi("Created at", "تاريخ الإنشاء"), auto_now_add=True)
    modified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=bi("Last modified by", "آخر من عدّله"))
    modified_at = models.DateTimeField(bi("Last modified at", "تاريخ آخر تعديل"), null=True, blank=True)
    source = models.CharField(bi("Source", "المصدر"), max_length=60, default="human",
                              help_text=bi("human, import, system:<code> or agent:<name>", "شخص أو استيراد أو نظام أو وكيل"))

    class Meta:
        abstract = True


def _entity_fk(related_name):
    return models.ForeignKey(Entity, on_delete=models.PROTECT, related_name=related_name,
                             verbose_name=bi("Entity", "الكيان"))


def _account_fk(en, ar, null=True):
    return models.ForeignKey("ledger.Account", on_delete=models.PROTECT, related_name="+", null=null, blank=null,
                             verbose_name=bi(en, ar))


# --- setup data (3.1, 3.7, 3.9) -----------------------------------------------------------------------------------

class AssetCategory(Written):
    entity = _entity_fk("asset_categories")
    code = models.CharField(bi("Code", "الرمز"), max_length=20)
    name_en = models.CharField(bi("Name (English)", "الاسم (بالإنجليزية)"), max_length=100)
    name_ar = models.CharField(bi("Name (Arabic)", "الاسم (بالعربية)"), max_length=100)
    asset_class = models.CharField(bi("Asset class", "فئة الأصل"), max_length=24, choices=AssetClass.choices)
    cost_account = _account_fk("Cost account", "حساب التكلفة", null=False)
    accum_depr_account = _account_fk("Accumulated depreciation account", "حساب مجمع الاستهلاك")
    depr_expense_account = _account_fk("Depreciation expense account", "حساب مصروف الاستهلاك")
    accum_impairment_account = _account_fk("Accumulated impairment account", "حساب مجمع هبوط القيمة")
    impairment_expense_account = _account_fk("Impairment expense account", "حساب خسائر هبوط القيمة")
    disposal_account = _account_fk("Disposal gain or loss account", "حساب أرباح وخسائر الاستبعاد")
    depreciable = models.BooleanField(bi("Depreciable", "قابلة للاستهلاك"))
    retention_class = models.CharField(bi("Retention class", "فئة الحفظ"), max_length=30,
                                       choices=RetentionClass.choices)
    rental_fleet = models.BooleanField(bi("Rental fleet", "أسطول تأجير"), default=False)
    is_active = models.BooleanField(bi("Active", "نشطة"), default=True)

    class Meta:
        verbose_name = bi("asset category", "فئة أصول")
        verbose_name_plural = bi("asset categories", "فئات الأصول")
        ordering = ["entity", "code"]
        constraints = [models.UniqueConstraint(fields=["entity", "code"], name="fa_category_code_per_entity")]

    def __str__(self):
        return f"{self.code} {self.name_en}"

    @property
    def label(self):
        return f"{self.code} ({self.name_en} / {self.name_ar})"


class AssetLocation(Written):
    entity = _entity_fk("asset_locations")
    code = models.CharField(bi("Code", "الرمز"), max_length=20)
    name_en = models.CharField(bi("Name (English)", "الاسم (بالإنجليزية)"), max_length=120, blank=True)
    name_ar = models.CharField(bi("Name (Arabic)", "الاسم (بالعربية)"), max_length=120)
    property_ref = models.CharField(bi("Property / unit (billing)", "العقار / الوحدة (الفوترة)"), max_length=60,
                                    blank=True)
    city_en = models.CharField(bi("City (English)", "المدينة (بالإنجليزية)"), max_length=100, blank=True)
    city_ar = models.CharField(bi("City (Arabic)", "المدينة (بالعربية)"), max_length=100, blank=True)

    class Meta:
        verbose_name = bi("asset location", "موقع أصول")
        verbose_name_plural = bi("asset locations", "مواقع الأصول")
        ordering = ["entity", "code"]
        constraints = [models.UniqueConstraint(fields=["entity", "code"], name="fa_location_code_per_entity")]

    def __str__(self):
        return f"{self.code} {self.name_ar}"


class FixedAssetSettings(Written):
    """Per-entity settings (3.9). Art. 52 periods: Needs expert sign-off: tax (OQ-27)."""

    entity = models.OneToOneField(Entity, on_delete=models.PROTECT, related_name="fixed_asset_settings",
                                  verbose_name=bi("Entity", "الكيان"))
    art52_adjustment_months_movable = models.PositiveSmallIntegerField(
        bi("Art. 52 adjustment period, movable (months)", "فترة التعديل للمادة 52، المنقولات (أشهر)"), default=72)
    art52_adjustment_months_real_estate = models.PositiveSmallIntegerField(
        bi("Art. 52 adjustment period, real estate (months)", "فترة التعديل للمادة 52، العقارات (أشهر)"), default=120)
    normal_credit_days = models.PositiveSmallIntegerField(
        bi("Normal credit terms (days)", "مدة الائتمان المعتادة (أيام)"), null=True, blank=True,
        help_text=bi("Set by the CFO; empty: nothing is flagged as deferred payment (R4a, OQ-25)",
                     "يحددها المدير المالي؛ إن تُركت فارغة لا يُعلَّم شيء كدفع مؤجل"))
    reporting_dates = models.JSONField(bi("Reporting dates", "تواريخ إعداد التقارير"), default=list, blank=True,
                                       help_text=bi("Period ends designated as reporting dates besides the fiscal "
                                                    "year end (ISO dates; R16, R19)",
                                                    "نهايات الفترات المحددة تواريخَ تقارير غير نهاية السنة المالية"))

    class Meta:
        verbose_name = bi("fixed-asset settings", "إعدادات الأصول الثابتة")
        verbose_name_plural = bi("fixed-asset settings", "إعدادات الأصول الثابتة")


class DepreciationPolicy(Written):
    """Versioned policy data approved through the engine (activity ``fixed_assets.policy``, R14). Fixture values in
    tests; real values are the CFO's (OQ-2)."""

    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        SUBMITTED = "submitted", bi("Submitted", "مقدَّمة")
        APPROVED = "approved", bi("Approved", "معتمدة")

    entity = _entity_fk("depreciation_policies")
    category = models.ForeignKey(AssetCategory, on_delete=models.PROTECT, related_name="policies",
                                 verbose_name=bi("Category", "الفئة"))
    effective_from = models.DateField(bi("Effective from", "سارية من"))
    method = models.CharField(bi("Method", "الطريقة"), max_length=20, choices=Method.choices,
                              default=Method.STRAIGHT_LINE)
    useful_life_months = models.PositiveSmallIntegerField(bi("Useful life (months)", "العمر الإنتاجي (أشهر)"),
                                                          null=True, blank=True)
    annual_rate = models.DecimalField(bi("Annual rate", "النسبة السنوية"), max_digits=7, decimal_places=4, null=True,
                                      blank=True, help_text=bi("Reducing balance: e.g. 0.4000 for 40%",
                                                               "القسط المتناقص: مثل 0.4000 لنسبة 40%"))
    residual_pct = models.DecimalField(bi("Residual value %", "نسبة القيمة المتبقية"), max_digits=6, decimal_places=3,
                                       default=ZERO)
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.DRAFT)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+", verbose_name=bi("Submitted by", "قدّمها"))
    submitted_at = models.DateTimeField(bi("Submitted at", "تاريخ التقديم"), null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=bi("Approved by", "اعتمدها"))
    approved_at = models.DateTimeField(bi("Approved at", "تاريخ الاعتماد"), null=True, blank=True)
    basis_note_en = models.TextField(bi("Basis (English)", "الأساس (بالإنجليزية)"), blank=True)
    basis_note_ar = models.TextField(bi("Basis (Arabic)", "الأساس (بالعربية)"))
    version = models.PositiveIntegerField(bi("Version", "رقم النسخة"), default=0, editable=False)

    class Meta:
        verbose_name = bi("depreciation policy", "سياسة استهلاك")
        verbose_name_plural = bi("depreciation policies", "سياسات الاستهلاك")
        ordering = ["entity", "category", "-effective_from"]
        constraints = [models.UniqueConstraint(fields=["category", "effective_from"], condition=Q(status="approved"),
                                               name="fa_one_approved_policy_per_date")]


class AssetSequence(models.Model):
    """Numbers: ``{entity}-FA-{000001}`` per entity, ``{entity}-DEP-{yyyy}-{000001}`` per year."""

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    kind = models.CharField(max_length=10)
    year = models.PositiveSmallIntegerField(default=0)
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["entity", "kind", "year"], name="fa_sequence_unique")]


# --- the register (3.3, 3.4) --------------------------------------------------------------------------------------

class FixedAsset(Written):
    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        SUBMITTED = "submitted", bi("Submitted", "مقدَّم")
        ACTIVE = "active", bi("Active", "نشط")
        FULLY_DEPRECIATED = "fully_depreciated", bi("Fully depreciated", "مستهلك بالكامل")
        DISPOSED = "disposed", bi("Disposed", "مستبعد")

    entity = _entity_fk("fixed_assets")
    code = models.CharField(bi("Code", "الرمز"), max_length=40, blank=True)
    category = models.ForeignKey(AssetCategory, on_delete=models.PROTECT, related_name="assets",
                                 verbose_name=bi("Category", "الفئة"))
    description_en = models.CharField(bi("Description (English)", "الوصف (بالإنجليزية)"), max_length=300, blank=True)
    description_ar = models.CharField(bi("Description (Arabic)", "الوصف (بالعربية)"), max_length=300)
    location = models.ForeignKey(AssetLocation, on_delete=models.PROTECT, related_name="assets",
                                 verbose_name=bi("Location", "الموقع"))
    serial_or_plate = models.CharField(bi("Serial or plate number", "الرقم التسلسلي أو رقم اللوحة"), max_length=60,
                                       blank=True)
    custodian_ref = models.CharField(bi("Custodian (employee code)", "العهدة (الرقم الوظيفي)"), max_length=20,
                                     blank=True)
    usage = models.CharField(bi("Usage", "الاستخدام"), max_length=24, choices=Usage.choices, default=Usage.OWN_USE)
    held_for = models.CharField(bi("Held for", "محتفظ به لغرض"), max_length=24, choices=HeldFor.choices, blank=True)
    use_evidence_ref = models.CharField(bi("Evidence of use", "إثبات الاستخدام"), max_length=200, blank=True)
    vat_use = models.CharField(bi("VAT use", "استخدام الضريبة"), max_length=10, choices=VatUse.choices, blank=True)
    initial_recovery_pct = models.DecimalField(bi("Initial VAT recovery %", "نسبة استرداد الضريبة الأولية"),
                                               max_digits=7, decimal_places=4, null=True, blank=True)
    input_vat_amount = models.DecimalField(bi("Input VAT charged (SAR)", "ضريبة المدخلات المفروضة (ريال)"), null=True,
                                           blank=True, **MONEY)
    adjustment_period_end = models.DateField(bi("Art. 52 adjustment period end", "نهاية فترة التعديل للمادة 52"),
                                             null=True, blank=True)
    deferred_payment_flag = models.BooleanField(bi("Deferred payment", "دفع مؤجل"), default=False)
    acquisition_date = models.DateField(bi("Acquisition date", "تاريخ الاقتناء"), null=True, blank=True)
    available_for_use_date = models.DateField(bi("Available for use", "تاريخ الجاهزية للاستخدام"), null=True, blank=True)
    cost = models.DecimalField(bi("Cost (SAR)", "التكلفة (ريال)"), default=ZERO, **MONEY)
    residual_value = models.DecimalField(bi("Residual value (SAR)", "القيمة المتبقية (ريال)"), null=True, blank=True,
                                         help_text=bi("Empty: the policy's residual % of cost at activation",
                                                      "فارغة: نسبة القيمة المتبقية في السياسة من التكلفة عند التفعيل"),
                                         **MONEY)
    method = models.CharField(bi("Method", "الطريقة"), max_length=20, choices=Method.choices, blank=True)
    useful_life_months = models.PositiveSmallIntegerField(bi("Useful life (months)", "العمر الإنتاجي (أشهر)"),
                                                          null=True, blank=True)
    annual_rate = models.DecimalField(bi("Annual rate", "النسبة السنوية"), max_digits=7, decimal_places=4, null=True,
                                      blank=True)
    policy = models.ForeignKey(DepreciationPolicy, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                               verbose_name=bi("Policy copied at activation", "السياسة المنسوخة عند التفعيل"))
    opening_date = models.DateField(bi("Opening (cut-over) date", "تاريخ الرصيد الافتتاحي"), null=True, blank=True)
    opening_accumulated_depreciation = models.DecimalField(
        bi("Opening accumulated depreciation (SAR)", "مجمع الاستهلاك الافتتاحي (ريال)"), default=ZERO, **MONEY)
    opening_remaining_life_months = models.PositiveSmallIntegerField(
        bi("Remaining life at cut-over (months)", "العمر المتبقي عند التحويل (أشهر)"), null=True, blank=True)
    legacy_source_system = models.CharField(bi("Legacy system", "النظام السابق"), max_length=20, blank=True)
    legacy_code = models.CharField(bi("Legacy code", "الرمز السابق"), max_length=40, blank=True)
    status = models.CharField(bi("Status", "الحالة"), max_length=20, choices=Status.choices, default=Status.DRAFT)
    disposal_date = models.DateField(bi("Disposal date", "تاريخ الاستبعاد"), null=True, blank=True)
    transferred_from = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                                         verbose_name=bi("Transferred from", "منقول من"),
                                         help_text=bi("Intercompany origin (R24, slice 2)", "مصدر النقل بين الشركات"))
    retention_class = models.CharField(bi("Retention class", "فئة الحفظ"), max_length=30,
                                       choices=RetentionClass.choices, blank=True)
    retention_until = models.DateField(bi("Keep until", "يُحفظ حتى"), null=True, blank=True)
    retention_floor = models.DateField(bi("Keep at least until", "يُحفظ على الأقل حتى"), null=True, blank=True,
                                       help_text=bi("Carried from a CWIP asset (never shortened, R33)",
                                                    "منقول من أصل تحت التنفيذ (لا يُقصَّر)"))
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+", verbose_name=bi("Submitted by", "قدّمه"))
    submitted_at = models.DateTimeField(bi("Submitted at", "تاريخ التقديم"), null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name=bi("Approved by", "اعتمده"))
    approved_at = models.DateTimeField(bi("Approved at", "تاريخ الاعتماد"), null=True, blank=True)
    version = models.PositiveIntegerField(bi("Version", "رقم النسخة"), default=0, editable=False)

    class Meta:
        verbose_name = bi("fixed asset", "أصل ثابت")
        verbose_name_plural = bi("fixed assets", "الأصول الثابتة")
        ordering = ["entity", "code", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["entity", "code"], condition=~Q(code=""), name="fa_asset_code_per_entity"),
            models.UniqueConstraint(fields=["entity", "legacy_source_system", "legacy_code"], condition=~Q(legacy_code=""),
                                    name="fa_legacy_code_per_entity"),
            models.CheckConstraint(condition=Q(cost__gte=0), name="fa_cost_not_negative"),
        ]

    # Fields that change on an active asset only through posted or approved events (R8, R16, R47).
    EVENT_FIELDS = ("category_id", "cost", "method", "useful_life_months", "annual_rate", "residual_value", "vat_use")

    def _guard(self, stored):
        if self.pk:
            row = type(self)._base_manager.filter(pk=self.pk).values("vat_use").first()
            if row and row["vat_use"] != self.vat_use:
                raise error("vat_use_by_event")

    def __str__(self):
        return self.code or f"{self.entity.code} draft asset {self.pk}"

    @property
    def is_migrated(self):
        return bool(self.legacy_code)


class AssetCostComponent(Kept):
    class Kind(models.TextChoices):
        BILL_LINE = "bill_line", bi("Bill line", "بند فاتورة")
        FINANCED_ACQUISITION = "financed_acquisition", bi("Financed acquisition", "اقتناء ممول")
        CWIP_TRANSFER = "cwip_transfer", bi("CWIP transfer", "تحويل من المشروعات تحت التنفيذ")
        OPENING_BALANCE = "opening_balance", bi("Opening balance", "رصيد افتتاحي")
        SUBSEQUENT_COST = "subsequent_cost", bi("Subsequent cost or adjustment", "تكلفة لاحقة أو تسوية")

    entity = _entity_fk("+")
    asset = models.ForeignKey(FixedAsset, on_delete=models.PROTECT, related_name="components",
                              verbose_name=bi("Asset", "الأصل"))
    kind = models.CharField(bi("Kind", "النوع"), max_length=24, choices=Kind.choices)
    purchase_line = models.ForeignKey("invoicing.PurchaseDocumentLine", null=True, blank=True,
                                      on_delete=models.PROTECT, related_name="+",
                                      verbose_name=bi("Bill line", "بند الفاتورة"))
    reverses = models.OneToOneField("self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversed_by",
                                    verbose_name=bi("Reverses component", "يعكس المكوّن"))
    amount = models.DecimalField(bi("Amount (SAR)", "المبلغ (ريال)"), **MONEY)
    accounting_date = models.DateField(bi("Accounting date", "تاريخ القيد"))
    effective_from = models.DateField(bi("Depreciated from", "يُستهلك من"), null=True, blank=True,
                                      help_text=bi("Empty: part of the cost at activation; else the first day of the "
                                                   "month it changes depreciation", "فارغ: جزء من التكلفة عند التفعيل"))
    vat_charged = models.DecimalField(bi("VAT charged (SAR)", "الضريبة المفروضة (ريال)"), default=ZERO, **MONEY)
    vat_recovered = models.DecimalField(bi("VAT recovered (SAR)", "الضريبة المستردة (ريال)"), default=ZERO, **MONEY)
    source_event = models.ForeignKey("AssetEvent", null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                                     verbose_name=bi("Event", "الحدث"))
    deferred_payment = models.BooleanField(bi("Deferred payment", "دفع مؤجل"), default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = bi("asset cost component", "مكوّن تكلفة أصل")
        verbose_name_plural = bi("asset cost components", "مكوّنات تكلفة الأصول")
        ordering = ["asset", "pk"]
        constraints = [
            # R4: a bill line feeds one asset once (a split feeds several assets, each once); the service caps the sum.
            models.UniqueConstraint(fields=["purchase_line", "asset"], condition=Q(purchase_line__isnull=False),
                                    name="fa_line_once_per_asset"),
        ]


# --- documents (3.5, 3.6) -----------------------------------------------------------------------------------------

class DepreciationRun(Written):
    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        SUBMITTED = "submitted", bi("Submitted", "مقدَّم")
        POSTED = "posted", bi("Posted", "مرحّل")
        DISCARDED = "discarded", bi("Discarded", "ملغى")
        REVERSED = "reversed", bi("Reversed", "معكوس")

    entity = _entity_fk("depreciation_runs")
    period = models.ForeignKey("ledger.FiscalPeriod", on_delete=models.PROTECT, related_name="+",
                               verbose_name=bi("Period", "الفترة"))
    number = models.CharField(bi("Number", "الرقم"), max_length=40)
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.DRAFT)
    fingerprint = models.CharField(bi("Fingerprint", "البصمة"), max_length=64)
    total = models.DecimalField(bi("Total (SAR)", "الإجمالي (ريال)"), default=ZERO, **MONEY)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+", verbose_name=bi("Submitted by", "قدّمه"))
    submitted_at = models.DateTimeField(bi("Submitted at", "تاريخ التقديم"), null=True, blank=True)
    posted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                  related_name="+", verbose_name=bi("Posted by", "رحّله"))
    posted_at = models.DateTimeField(bi("Posted at", "تاريخ الترحيل"), null=True, blank=True)
    journal_entry = models.ForeignKey("ledger.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                      related_name="+", verbose_name=bi("Journal entry", "القيد اليومي"))
    reversal_entry = models.ForeignKey("ledger.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                       related_name="+", verbose_name=bi("Reversal entry", "قيد العكس"))
    reversal_requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                              on_delete=models.PROTECT, related_name="+",
                                              verbose_name=bi("Reversal requested by", "طلب العكس"))
    reversal_requested_at = models.DateTimeField(bi("Reversal requested at", "تاريخ طلب العكس"), null=True, blank=True)
    reversal_reason_en = models.TextField(bi("Reversal reason (English)", "سبب العكس (بالإنجليزية)"), blank=True)
    reversal_reason_ar = models.TextField(bi("Reversal reason (Arabic)", "سبب العكس (بالعربية)"), blank=True)
    version = models.PositiveIntegerField(bi("Version", "رقم النسخة"), default=0, editable=False)

    class Meta:
        verbose_name = bi("depreciation run", "مسيّر استهلاك")
        verbose_name_plural = bi("depreciation runs", "مسيرات الاستهلاك")
        ordering = ["entity", "-period__start_date", "-pk"]
        constraints = [
            # R15: one live run (draft, submitted or posted) per entity and period; a reversed period is re-runnable.
            models.UniqueConstraint(fields=["entity", "period"], condition=Q(status__in=("draft", "submitted", "posted")),
                                    name="fa_one_live_run_per_period"),
            models.UniqueConstraint(fields=["entity", "number"], name="fa_run_number_per_entity"),
        ]

    def __str__(self):
        return self.number


class DepreciationLine(Kept):
    entity = _entity_fk("+")
    run = models.ForeignKey(DepreciationRun, on_delete=models.PROTECT, related_name="lines",
                            verbose_name=bi("Run", "المسيّر"))
    asset = models.ForeignKey(FixedAsset, on_delete=models.PROTECT, related_name="depreciation_lines",
                              verbose_name=bi("Asset", "الأصل"))
    days = models.PositiveSmallIntegerField(bi("Days", "الأيام"))
    amount = models.DecimalField(bi("Amount (SAR)", "المبلغ (ريال)"), **MONEY)
    catch_up = models.BooleanField(bi("Catch-up", "استدراك"), default=False)
    opening_nbv = models.DecimalField(bi("Opening net book value", "صافي القيمة الدفترية الافتتاحية"), **MONEY)

    class Meta:
        verbose_name = bi("depreciation line", "بند استهلاك")
        verbose_name_plural = bi("depreciation lines", "بنود الاستهلاك")
        ordering = ["run", "asset", "catch_up"]
        constraints = [models.UniqueConstraint(fields=["run", "asset", "catch_up"], name="fa_line_per_run_asset")]


class AssetEvent(Written):
    class Kind(models.TextChoices):
        FINANCED_ACQUISITION = "financed_acquisition", bi("Financed acquisition", "اقتناء ممول")
        CAPITALISE_CWIP = "capitalise_cwip", bi("CWIP capitalisation", "رسملة المشروعات تحت التنفيذ")
        SUBSEQUENT_COST = "subsequent_cost", bi("Subsequent cost or cost adjustment", "تكلفة لاحقة أو تسوية تكلفة")
        CHANGE_OF_ESTIMATE = "change_of_estimate", bi("Change of estimate", "تغيير تقدير")
        IMPAIRMENT = "impairment", bi("Impairment", "هبوط القيمة")
        IMPAIRMENT_REVERSAL = "impairment_reversal", bi("Impairment reversal", "عكس هبوط القيمة")
        WRITE_OFF = "write_off", bi("Write-off", "شطب")
        VAT_USE_CHANGE = "vat_use_change", bi("Change of VAT use", "تغيير استخدام الضريبة")
        DISPOSAL_SALE = "disposal_sale", bi("Sale (slice 2)", "بيع (المرحلة الثانية)")
        INTERCOMPANY_SALE = "intercompany_sale", bi("Intercompany sale (slice 2)", "بيع بين الشركات (المرحلة الثانية)")
        RECLASSIFICATION = "reclassification", bi("Reclassification (slice 2)", "إعادة تصنيف (المرحلة الثانية)")
        REVALUATION = "revaluation", bi("Revaluation (not built, OQ-8)", "إعادة التقييم (غير متاحة)")
        FAIR_VALUE_CHANGE = "fair_value_change", bi("Fair value change (not built, OQ-6)", "تغير القيمة العادلة (غير متاح)")

    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        SUBMITTED = "submitted", bi("Submitted", "مقدَّم")
        POSTED = "posted", bi("Posted", "مرحّل")
        APPROVED = "approved", bi("Approved (no entry)", "معتمد (بلا قيد)")
        DISCARDED = "discarded", bi("Discarded", "ملغى")
        REVERSED = "reversed", bi("Reversed", "معكوس")

    entity = _entity_fk("asset_events")
    asset = models.ForeignKey(FixedAsset, on_delete=models.PROTECT, related_name="events",
                              verbose_name=bi("Asset", "الأصل"))
    kind = models.CharField(bi("Kind", "النوع"), max_length=24, choices=Kind.choices)
    event_date = models.DateField(bi("Event date", "تاريخ الحدث"))
    amount = models.DecimalField(bi("Amount (SAR)", "المبلغ (ريال)"), null=True, blank=True,
                                 help_text=bi("The amount capitalised, impaired, reversed or written off; the engine "
                                              "measure (SAR)", "المبلغ المرسمل أو الهابط أو المعكوس أو المشطوب"),
                                 **MONEY)
    # financed acquisition (R7, FA8)
    cash_cost_price = models.DecimalField(bi("Cash cost price (SAR)", "سعر التكلفة النقدي (ريال)"), null=True,
                                          blank=True, **MONEY)
    financing_total = models.DecimalField(bi("Financing total (SAR)", "إجمالي التمويل (ريال)"), null=True, blank=True,
                                          **MONEY)
    financing_ref = models.CharField(bi("Financing contract", "عقد التمويل"), max_length=60, blank=True)
    # impairment (R19)
    recoverable_amount = models.DecimalField(bi("Recoverable amount (SAR)", "المبلغ القابل للاسترداد (ريال)"),
                                             null=True, blank=True, **MONEY)
    # CWIP (R44, FA5)
    target_asset = models.ForeignKey(FixedAsset, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                                     verbose_name=bi("Target asset", "الأصل المستهدف"))
    # change of estimate (R16) and VAT use (R47)
    new_method = models.CharField(bi("New method", "الطريقة الجديدة"), max_length=20, choices=Method.choices,
                                  blank=True)
    new_useful_life_months = models.PositiveSmallIntegerField(bi("New total useful life (months)",
                                                                 "العمر الإنتاجي الكلي الجديد (أشهر)"), null=True,
                                                              blank=True)
    new_annual_rate = models.DecimalField(bi("New annual rate", "النسبة السنوية الجديدة"), max_digits=7,
                                          decimal_places=4, null=True, blank=True)
    new_residual_value = models.DecimalField(bi("New residual value (SAR)", "القيمة المتبقية الجديدة (ريال)"),
                                             null=True, blank=True, **MONEY)
    effective_from = models.DateField(bi("Effective from", "يسري من"), null=True, blank=True)
    old_vat_use = models.CharField(bi("Old VAT use", "استخدام الضريبة السابق"), max_length=10,
                                   choices=VatUse.choices, blank=True)
    new_vat_use = models.CharField(bi("New VAT use", "استخدام الضريبة الجديد"), max_length=10,
                                   choices=VatUse.choices, blank=True)
    # subsequent cost and cost adjustments (R2, R4)
    purchase_line = models.ForeignKey("invoicing.PurchaseDocumentLine", null=True, blank=True,
                                      on_delete=models.PROTECT, related_name="+",
                                      verbose_name=bi("Bill line", "بند الفاتورة"))
    reverses_component = models.ForeignKey(AssetCostComponent, null=True, blank=True, on_delete=models.PROTECT,
                                           related_name="+", verbose_name=bi("Reverses component", "يعكس المكوّن"))
    source_journal_line = models.ForeignKey("ledger.JournalLine", null=True, blank=True, on_delete=models.PROTECT,
                                            related_name="+", verbose_name=bi("Source journal line", "بند القيد المصدر"))
    # write-off (R23, R25): what was derecognised
    cost_derecognised = models.DecimalField(bi("Cost derecognised", "التكلفة المستبعدة"), null=True, blank=True,
                                            **MONEY)
    depreciation_derecognised = models.DecimalField(bi("Accumulated depreciation derecognised", "مجمع الاستهلاك المستبعد"),
                                                    null=True, blank=True, **MONEY)
    impairment_derecognised = models.DecimalField(bi("Accumulated impairment derecognised", "مجمع الهبوط المستبعد"),
                                                  null=True, blank=True, **MONEY)
    depreciation_until = models.DateField(bi("Depreciated until", "مستهلك حتى"), null=True, blank=True)
    evidence_ref = models.CharField(bi("Evidence", "المستند المؤيد"), max_length=200, blank=True)
    reason_en = models.TextField(bi("Reason (English)", "السبب (بالإنجليزية)"), blank=True)
    reason_ar = models.TextField(bi("Reason (Arabic)", "السبب (بالعربية)"))
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.DRAFT)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+", verbose_name=bi("Submitted by", "قدّمه"))
    submitted_at = models.DateTimeField(bi("Submitted at", "تاريخ التقديم"), null=True, blank=True)
    posted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                  related_name="+", verbose_name=bi("Posted or approved by", "رحّله أو اعتمده"))
    posted_at = models.DateTimeField(bi("Posted or approved at", "تاريخ الترحيل أو الاعتماد"), null=True, blank=True)
    journal_entry = models.ForeignKey("ledger.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                      related_name="+", verbose_name=bi("Journal entry", "القيد اليومي"))
    reversal_entry = models.ForeignKey("ledger.JournalEntry", null=True, blank=True, on_delete=models.PROTECT,
                                       related_name="+", verbose_name=bi("Reversal entry", "قيد العكس"))
    reversal_requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                              on_delete=models.PROTECT, related_name="+",
                                              verbose_name=bi("Reversal requested by", "طلب العكس"))
    reversal_requested_at = models.DateTimeField(bi("Reversal requested at", "تاريخ طلب العكس"), null=True, blank=True)
    reversal_reason_en = models.TextField(bi("Reversal reason (English)", "سبب العكس (بالإنجليزية)"), blank=True)
    reversal_reason_ar = models.TextField(bi("Reversal reason (Arabic)", "سبب العكس (بالعربية)"), blank=True)
    previous_values = models.JSONField(bi("Values before the change", "القيم قبل التغيير"), default=dict, blank=True)
    version = models.PositiveIntegerField(bi("Version", "رقم النسخة"), default=0, editable=False)

    class Meta:
        verbose_name = bi("asset event", "حدث أصل")
        verbose_name_plural = bi("asset events", "أحداث الأصول")
        ordering = ["entity", "asset", "event_date", "pk"]

    def __str__(self):
        return f"{self.get_kind_display().split(' / ')[0]} #{self.pk} ({self.asset})"


# --- records that post nothing (3.8, reminders) -------------------------------------------------------------------

class IpFairValue(Kept):
    """Investment-property fair value (R48; IAS 40.79(e), s16 justification). Append-only; posts nothing."""

    entity = _entity_fk("+")
    asset = models.ForeignKey(FixedAsset, on_delete=models.PROTECT, related_name="fair_values",
                              verbose_name=bi("Asset", "الأصل"))
    valuation_date = models.DateField(bi("Valuation date", "تاريخ التقييم"))
    amount = models.DecimalField(bi("Fair value (SAR)", "القيمة العادلة (ريال)"), null=True, blank=True, **MONEY)
    valuer_name_en = models.CharField(bi("Valuer (English)", "المقيّم (بالإنجليزية)"), max_length=200, blank=True)
    valuer_name_ar = models.CharField(bi("Valuer (Arabic)", "المقيّم (بالعربية)"), max_length=200, blank=True)
    valuer_licence_ref = models.CharField(bi("Valuer licence (Taqeem)", "ترخيص المقيّم (تقييم)"), max_length=60,
                                          blank=True)
    evidence_ref = models.CharField(bi("Evidence", "المستند المؤيد"), max_length=200, blank=True)
    undue_cost_justification_en = models.TextField(bi("Undue cost or effort (English)", "مبرر التكلفة أو الجهد "
                                                                                          "غير المبرر (بالإنجليزية)"),
                                                   blank=True)
    undue_cost_justification_ar = models.TextField(bi("Undue cost or effort (Arabic)", "مبرر التكلفة أو الجهد غير "
                                                                                         "المبرر (بالعربية)"),
                                                   blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = bi("investment property fair value", "القيمة العادلة لعقار استثماري")
        verbose_name_plural = bi("investment property fair values", "القيم العادلة للعقارات الاستثمارية")
        ordering = ["asset", "-valuation_date", "-pk"]


class ReviewReminder(Kept):
    """R16 / R19 reminders raised by the reminder job (bilingual). Append-only."""

    class Kind(models.TextChoices):
        ESTIMATE_REVIEW = "estimate_review", bi("Review of residual value, useful life and method",
                                                "مراجعة القيمة المتبقية والعمر الإنتاجي والطريقة")
        IMPAIRMENT_INDICATORS = "impairment_indicators", bi("Impairment indicator assessment", "تقييم مؤشرات هبوط القيمة")

    entity = _entity_fk("+")
    kind = models.CharField(bi("Kind", "النوع"), max_length=24, choices=Kind.choices)
    due_date = models.DateField(bi("Reporting date", "تاريخ التقرير"))
    message_en = models.TextField(bi("Message (English)", "الرسالة (بالإنجليزية)"))
    message_ar = models.TextField(bi("Message (Arabic)", "الرسالة (بالعربية)"))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = bi("review reminder", "تذكير مراجعة")
        verbose_name_plural = bi("review reminders", "تذكيرات المراجعة")
        ordering = ["entity", "-due_date", "kind"]
        constraints = [models.UniqueConstraint(fields=["entity", "kind", "due_date"], name="fa_reminder_once")]
