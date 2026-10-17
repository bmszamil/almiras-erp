"""ZATCA Phase 2 e-invoicing records (docs/specs/zatca.md §3). Every model has an ``entity`` FK (R-1).

Logic lives in ``zatca.services``; the guards here keep written evidence from changing (R-17, R-18):
signed and cleared XML, ICV/PIH/hash, attempts, messages and events never change once written, and
status changes come only from the services (``obj._service_write = True`` right before the save).
On PostgreSQL migration ``0002_immutability_triggers`` repeats these rules for raw SQL.
Secrets are never stored: only secret-store references (D-3, R-6).
"""
import uuid as uuid_lib

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from core.models import Entity, bi

from .crypto import INITIAL_PIH
from .messages import ZatcaError

USER = settings.AUTH_USER_MODEL


def _immutable(en="This ZATCA record is evidence and never changes.", ar="هذا السجل دليل لدى الهيئة ولا يتغير."):
    return ZatcaError(en, ar, code="immutable")


class Environment(models.TextChoices):
    SANDBOX = "sandbox", bi("Sandbox", "البيئة التجريبية")
    SIMULATION = "simulation", bi("Simulation (unconfirmed, OQ-26)", "بيئة المحاكاة (غير مؤكدة)")
    PRODUCTION = "production", bi("Production", "بيئة الإنتاج")


class Source(models.TextChoices):
    HUMAN = "human", bi("Human", "شخص")
    IMPORT = "import", bi("Import", "استيراد")
    AGENT = "agent", bi("Agent", "وكيل")
    SYSTEM = "system", bi("System", "النظام")


class Tracked(models.Model):
    created_at = models.DateTimeField(bi("Created at", "تاريخ الإنشاء"), default=timezone.now)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+",
                                   verbose_name=bi("Created by", "أنشأه"))
    source = models.CharField(bi("Source", "المصدر"), max_length=10, choices=Source.choices, default=Source.HUMAN)

    class Meta:
        abstract = True


class NoBulkQuerySet(models.QuerySet):
    """Bulk writes skip save()/delete() guards, so they are refused for evidence models."""

    def update(self, **kwargs):
        raise _immutable()

    def delete(self):
        raise _immutable()


class AppendOnly(models.Model):
    """Written once; never updated or deleted (R-18)."""

    objects = NoBulkQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.pk and type(self)._base_manager.filter(pk=self.pk).exists():
            raise _immutable()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise _immutable()


class ServiceWritten(models.Model):
    """Rows created and changed only by zatca.services; never deleted."""

    objects = NoBulkQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        allowed, self._service_write = getattr(self, "_service_write", False), False
        if not allowed:
            raise ZatcaError("ZATCA records change only through zatca.services.",
                             "تتغير سجلات الهيئة من خلال خدمات الربط فقط.", code="service_only")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise _immutable()


# --- 3.1 profile, 3.2 EGS unit, 3.3 credential ---------------------------------------------------

class ZatcaProfile(Tracked, ServiceWritten):
    class SellerScheme(models.TextChoices):
        CRN = "CRN", bi("Commercial registration", "السجل التجاري")
        MOM = "MOM", bi("MOMRA licence", "ترخيص وزارة الشؤون البلدية")
        MLS = "MLS", bi("MHRSD licence", "ترخيص وزارة الموارد البشرية")
        UNIFIED = "700", bi("Unified number (700)", "الرقم الموحد (700)")
        SAG = "SAG", bi("MISA licence", "ترخيص وزارة الاستثمار")
        OTH = "OTH", bi("Other ID", "هوية أخرى")

    entity = models.OneToOneField(Entity, on_delete=models.PROTECT, related_name="zatca_profile",
                                  verbose_name=bi("Entity", "الكيان"))
    seller_id_scheme = models.CharField(bi("Seller ID scheme", "نوع معرّف البائع"), max_length=3,
                                        choices=SellerScheme.choices, default=SellerScheme.CRN)
    seller_id_value = models.CharField(
        bi("Seller ID (other schemes)", "معرّف البائع (الأنواع الأخرى)"), max_length=40, blank=True,
        help_text=bi("Only for MOM, MLS, SAG or OTH; CRN and 700 come from the entity (R-48)",
                     "لأنواع MOM وMLS وSAG وOTH فقط؛ السجل التجاري والرقم الموحد من بيانات الكيان"),
    )
    environment = models.CharField(bi("Environment", "البيئة"), max_length=10, choices=Environment.choices,
                                   default=Environment.SANDBOX)
    go_live_date = models.DateField(bi("Go-live date", "تاريخ بدء الربط"), null=True, blank=True)
    is_enabled = models.BooleanField(bi("Enabled", "مفعّل"), default=False)

    class Meta:
        verbose_name = bi("ZATCA profile", "ملف الربط مع الهيئة")
        verbose_name_plural = bi("ZATCA profiles", "ملفات الربط مع الهيئة")

    def __str__(self):
        return f"{self.entity.code} ({self.environment})"


class EgsUnit(Tracked, ServiceWritten):
    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        CSR_READY = "csr_ready", bi("CSR ready", "طلب الشهادة جاهز")
        COMPLIANCE_CSID = "compliance_csid", bi("Compliance CSID issued", "شهادة الامتثال صادرة")
        COMPLIANCE_PASSED = "compliance_passed", bi("Compliance checks passed", "اجتاز فحوص الامتثال")
        ACTIVE = "active", bi("Active", "نشطة")
        RENEWAL_DUE = "renewal_due", bi("Renewal due", "مستحقة التجديد")
        EXPIRED = "expired", bi("Expired", "منتهية")
        REVOKED = "revoked", bi("Revoked", "ملغاة")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="zatca_egs_units",
                               verbose_name=bi("Entity", "الكيان"))
    code = models.CharField(bi("Code", "الرمز"), max_length=20)
    name_en = models.CharField(bi("Name (English)", "الاسم (بالإنجليزية)"), max_length=200)
    name_ar = models.CharField(bi("Name (Arabic)", "الاسم (بالعربية)"), max_length=200)
    branch_name_en = models.CharField(bi("Branch (English)", "الفرع (بالإنجليزية)"), max_length=200, blank=True)
    branch_name_ar = models.CharField(bi("Branch (Arabic)", "الفرع (بالعربية)"), max_length=200, blank=True)
    serial_number = models.CharField(bi("Serial number", "الرقم التسلسلي"), max_length=200,
                                     help_text=bi("1-<solution>|2-<model>|3-<serial>", "1-الحل|2-الطراز|3-التسلسل"))
    location = models.CharField(bi("Location", "الموقع"), max_length=200)
    industry = models.CharField(bi("Industry", "النشاط"), max_length=200)
    supports_standard = models.BooleanField(bi("Standard invoices", "الفواتير القياسية"), default=True)
    supports_simplified = models.BooleanField(bi("Simplified invoices", "الفواتير المبسطة"), default=False)
    status = models.CharField(bi("Status", "الحالة"), max_length=20, choices=Status.choices, default=Status.DRAFT)
    last_icv = models.PositiveBigIntegerField(bi("Last invoice counter", "آخر عداد فاتورة"), default=0)
    last_invoice_hash = models.CharField(bi("Last invoice hash (next PIH)", "تجزئة آخر فاتورة"), max_length=100,
                                         default=INITIAL_PIH)
    private_key_ref = models.CharField(bi("Private key reference", "مرجع المفتاح الخاص"), max_length=300, blank=True)
    csr_pem = models.TextField(bi("CSR (public)", "طلب الشهادة (عام)"), blank=True)

    class Meta:
        verbose_name = bi("EGS unit", "وحدة إصدار الفواتير")
        verbose_name_plural = bi("EGS units", "وحدات إصدار الفواتير")
        constraints = [models.UniqueConstraint(fields=["entity", "code"], name="zatca_egs_code_per_entity")]

    def __str__(self):
        return f"{self.entity.code}/{self.code} ({self.status})"

    @property
    def invoice_type_flag(self):
        """CSR title (2.5.4.12): ``f"{std}{simp}00"`` (SFIS p.17; DTG p.28)."""
        return f"{int(self.supports_standard)}{int(self.supports_simplified)}00"


class EgsCredential(Tracked, ServiceWritten):
    class Kind(models.TextChoices):
        COMPLIANCE = "compliance", bi("Compliance CSID", "شهادة الامتثال")
        PRODUCTION = "production", bi("Production CSID", "شهادة الإنتاج")

    class Status(models.TextChoices):
        ACTIVE = "active", bi("Active", "نشطة")
        SUPERSEDED = "superseded", bi("Superseded", "مستبدلة")
        EXPIRED = "expired", bi("Expired", "منتهية")
        REVOKED = "revoked", bi("Revoked", "ملغاة")

    FIXED = ("entity_id", "egs_unit_id", "kind", "environment", "certificate_pem", "secret_ref", "zatca_request_id",
             "valid_from", "valid_to")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    egs_unit = models.ForeignKey(EgsUnit, on_delete=models.PROTECT, related_name="credentials",
                                 verbose_name=bi("EGS unit", "وحدة الفوترة"))
    kind = models.CharField(bi("Kind", "النوع"), max_length=10, choices=Kind.choices)
    environment = models.CharField(bi("Environment", "البيئة"), max_length=10, choices=Environment.choices)
    certificate_pem = models.TextField(bi("Certificate (public)", "الشهادة (عامة)"))
    secret_ref = models.CharField(bi("Secret reference", "مرجع السر"), max_length=300)
    zatca_request_id = models.CharField(bi("ZATCA request ID", "رقم طلب الهيئة"), max_length=100)
    valid_from = models.DateTimeField(bi("Valid from", "ساري من"))
    valid_to = models.DateTimeField(bi("Valid to", "ساري حتى"))
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.ACTIVE)
    replaced_by = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                                    verbose_name=bi("Replaced by", "استُبدلت بـ"))

    class Meta:
        verbose_name = bi("EGS credential", "شهادة وحدة الفوترة")
        verbose_name_plural = bi("EGS credentials", "شهادات وحدات الفوترة")
        constraints = [models.UniqueConstraint(fields=["egs_unit", "kind"], condition=models.Q(status="active"),
                                               name="zatca_one_active_credential_per_kind")]

    def save(self, *args, **kwargs):
        if self.entity_id != self.egs_unit.entity_id:  # R-1
            raise ZatcaError("A credential belongs to its EGS unit's entity.", "تتبع الشهادة كيان وحدة الفوترة.",
                             code="entity_mismatch")
        if self.pk:
            stored = type(self)._base_manager.filter(pk=self.pk).values(*self.FIXED, "status", "replaced_by_id").first()
            if stored:
                if any(stored[f] != getattr(self, f) for f in self.FIXED):
                    raise _immutable()
                if stored["status"] != self.Status.ACTIVE and stored["status"] != self.status:
                    raise _immutable()
                if stored["replaced_by_id"] and stored["replaced_by_id"] != self.replaced_by_id:
                    raise _immutable()
        super().save(*args, **kwargs)


# --- 3.4 e-invoice, 3.5 attempt, 3.6 message -------------------------------------------------------

class EInvoice(Tracked, ServiceWritten):
    class Category(models.TextChoices):
        STANDARD = "standard", bi("Standard (clearance)", "قياسية (اعتماد مسبق)")
        SIMPLIFIED = "simplified", bi("Simplified (reporting)", "مبسطة (إبلاغ)")

    class TypeCode(models.TextChoices):
        INVOICE = "388", bi("Invoice", "فاتورة")
        CREDIT_NOTE = "381", bi("Credit note", "إشعار دائن")
        DEBIT_NOTE = "383", bi("Debit note", "إشعار مدين")

    class Status(models.TextChoices):
        PENDING = "pending", bi("Pending", "قيد الانتظار")
        STAMPED = "stamped", bi("Stamped", "مختومة")
        QUEUED = "queued", bi("Queued", "في الطابور")
        SUBMITTING = "submitting", bi("Submitting", "قيد الإرسال")
        CLEARED = "cleared", bi("Cleared", "معتمدة")
        CLEARED_WITH_WARNINGS = "cleared_with_warnings", bi("Cleared with warnings", "معتمدة مع تحذيرات")
        REPORTED = "reported", bi("Reported", "مُبلَّغ عنها")
        REPORTED_WITH_WARNINGS = "reported_with_warnings", bi("Reported with warnings", "مُبلَّغ عنها مع تحذيرات")
        REPORTED_CLEARANCE_DISABLED = "reported_clearance_disabled", bi(
            "Reported, clearance disabled (not issued, OQ-7)", "مُبلَّغ عنها والاعتماد معطّل (غير صادرة)")
        REJECTED = "rejected", bi("Rejected", "مرفوضة")
        FAILED_RETRYABLE = "failed_retryable", bi("Failed, will retry", "متعثرة، ستُعاد المحاولة")
        FAILED_FINAL = "failed_final", bi("Failed", "متعثرة نهائيًا")

    class Hold(models.TextChoices):
        NONE = "", bi("None", "لا يوجد")
        LOCAL_VALIDATION = "local_validation", bi("Local validation failed", "لم تجتز الفحص المحلي")
        NOT_LIVE = "not_live", bi("Entity not live on Phase 2", "الكيان لم يبدأ المرحلة الثانية")
        NO_EGS = "no_egs", bi("No active EGS unit", "لا توجد وحدة فوترة نشطة")
        EGS_NOT_READY = "egs_not_ready", bi("EGS unit or credential not usable", "وحدة الفوترة أو شهادتها غير صالحة")

    class Api(models.TextChoices):
        CLEARANCE = "clearance", bi("Clearance", "الاعتماد")
        REPORTING = "reporting", bi("Reporting", "الإبلاغ")

    class Retention(models.TextChoices):
        STANDARD = "standard", bi("Standard (6 years)", "عادي (6 سنوات)")
        REAL_ESTATE = "capital_asset_real_estate", bi("Real estate capital asset (15 years)", "أصل عقاري (15 سنة)")
        MOVABLE = "movable_capital_asset", bi("Movable capital asset", "أصل رأسمالي منقول")

    STAMP_FIELDS = ("icv", "pih", "invoice_hash", "signed_xml", "qr_tlv", "signing_time")
    FIXED = ("entity_id", "source_document_id", "uuid", "zatca_category", "type_code")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    source_document = models.OneToOneField("invoicing.SalesDocument", on_delete=models.PROTECT,
                                           related_name="einvoice", verbose_name=bi("Document", "المستند"))
    egs_unit = models.ForeignKey(EgsUnit, null=True, blank=True, on_delete=models.PROTECT, related_name="einvoices",
                                 verbose_name=bi("EGS unit", "وحدة الفوترة"))
    uuid = models.UUIDField(bi("UUID", "المعرّف الفريد"), default=uuid_lib.uuid4)
    zatca_category = models.CharField(bi("Category", "الفئة"), max_length=10, choices=Category.choices)
    type_code = models.CharField(bi("Type code", "رمز النوع"), max_length=3, choices=TypeCode.choices)
    subtype_code = models.CharField(bi("Subtype code", "رمز النوع الفرعي"), max_length=7, blank=True)
    billing_reference = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="notes",
                                          verbose_name=bi("Original e-invoice", "الفاتورة الإلكترونية الأصلية"))
    issue_datetime = models.DateTimeField(bi("Issue time (UTC)", "وقت الإصدار"))
    icv = models.PositiveBigIntegerField(bi("Invoice counter", "عداد الفاتورة"), null=True, blank=True)
    pih = models.CharField(bi("Previous invoice hash", "تجزئة الفاتورة السابقة"), max_length=100, blank=True)
    invoice_hash = models.CharField(bi("Invoice hash", "تجزئة الفاتورة"), max_length=44, blank=True)
    signed_xml = models.TextField(bi("Signed XML", "الفاتورة الموقعة"), blank=True)
    signing_time = models.DateTimeField(bi("Signing time", "وقت التوقيع"), null=True, blank=True)
    qr_tlv = models.TextField(bi("QR (base64 TLV)", "رمز الاستجابة السريعة"), blank=True)
    cleared_xml = models.TextField(bi("Cleared XML", "الفاتورة المعتمدة"), blank=True)
    status = models.CharField(bi("Status", "الحالة"), max_length=30, choices=Status.choices, default=Status.PENDING)
    hold_reason = models.CharField(bi("Held because", "سبب التوقف"), max_length=20, choices=Hold.choices, blank=True)
    api = models.CharField(bi("API", "الواجهة"), max_length=10, choices=Api.choices)
    clearance_disabled = models.BooleanField(bi("Clearance disabled (HTTP 303)", "الاعتماد معطّل"), default=False)
    reporting_deadline = models.DateTimeField(bi("Reporting deadline", "مهلة الإبلاغ"), null=True, blank=True)
    deadline_alerted_at = models.DateTimeField(null=True, blank=True, editable=False)
    retention_class = models.CharField(bi("Retention class", "فئة الحفظ"), max_length=30, choices=Retention.choices,
                                       default=Retention.STANDARD)
    retention_until = models.DateField(bi("Keep until", "يُحفظ حتى"), null=True, blank=True)
    acquisition_date = models.DateField(bi("Acquisition date", "تاريخ الاقتناء"), null=True, blank=True)
    transient_failures = models.PositiveIntegerField(default=0, editable=False)
    next_attempt_at = models.DateTimeField(null=True, blank=True, editable=False)
    invoicing_synced = models.CharField(max_length=30, blank=True, editable=False,
                                        help_text="status last reported to invoicing (on_zatca_result)")

    class Meta:
        verbose_name = bi("e-invoice", "فاتورة إلكترونية")
        verbose_name_plural = bi("e-invoices", "الفواتير الإلكترونية")
        constraints = [
            models.UniqueConstraint(fields=["egs_unit", "icv"], name="zatca_icv_per_egs"),
            models.UniqueConstraint(fields=["entity", "uuid"], name="zatca_uuid_per_entity"),
        ]

    def __str__(self):
        return f"{self.entity.code} {self.type_code} ICV {self.icv or '-'} ({self.status})"

    def save(self, *args, **kwargs):
        if self.egs_unit_id and self.egs_unit.entity_id != self.entity_id:  # R-1 / R-2
            raise ZatcaError("An e-invoice is signed only by an EGS unit of its own entity.",
                             "توقّع الفاتورة الإلكترونية وحدة فوترة تابعة لكيانها فقط.", code="entity_mismatch")
        if self.source_document.entity_id != self.entity_id:
            raise ZatcaError("An e-invoice belongs to its document's entity.", "تتبع الفاتورة الإلكترونية كيان مستندها.",
                             code="entity_mismatch")
        if self.billing_reference_id and self.billing_reference.entity_id != self.entity_id:  # R-19, AT-27
            raise ZatcaError("A note references an e-invoice of its own entity.", "يشير الإشعار إلى فاتورة لكيانه.",
                             code="entity_mismatch")
        if self.pk:
            fields = (*self.FIXED, *self.STAMP_FIELDS, "cleared_xml", "egs_unit_id", "retention_until")
            stored = type(self)._base_manager.filter(pk=self.pk).values(*fields).first()
            if stored:
                if any(stored[f] != getattr(self, f) for f in self.FIXED):
                    raise _immutable()
                for f in (*self.STAMP_FIELDS, "cleared_xml"):  # R-17: written once
                    if stored[f] not in (None, "") and stored[f] != getattr(self, f):
                        raise _immutable("Signed or cleared XML, ICV, PIH and hash never change (R-17).",
                                         "لا تتغير الفاتورة الموقعة أو المعتمدة ولا العداد أو التجزئة.")
                if stored["icv"] is not None and stored["egs_unit_id"] != self.egs_unit_id:
                    raise _immutable()
                if stored["retention_until"] and (self.retention_until is None
                                                  or self.retention_until < stored["retention_until"]):
                    raise _immutable("Retention is never shortened (R-38).", "لا تُقصَّر مدة الحفظ أبدًا.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):  # R-18: only pending rows with no ICV may go
        stored = type(self)._base_manager.filter(pk=self.pk).values("status", "icv").first()
        if not stored or stored["status"] != self.Status.PENDING or stored["icv"] is not None:
            raise _immutable()
        return models.Model.delete(self, *args, **kwargs)


class SubmissionAttempt(AppendOnly):
    class Outcome(models.TextChoices):
        ACCEPTED = "accepted", bi("Accepted", "مقبول")
        ACCEPTED_WITH_WARNINGS = "accepted_with_warnings", bi("Accepted with warnings", "مقبول مع تحذيرات")
        REJECTED = "rejected", bi("Rejected", "مرفوض")
        CLEARANCE_DISABLED = "clearance_disabled", bi("Clearance disabled (303)", "الاعتماد معطّل")
        TRANSIENT_ERROR = "transient_error", bi("Transient error", "خطأ مؤقت")
        PERMANENT_ERROR = "permanent_error", bi("Permanent error", "خطأ دائم")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    einvoice = models.ForeignKey(EInvoice, on_delete=models.PROTECT, related_name="attempts",
                                 verbose_name=bi("E-invoice", "الفاتورة الإلكترونية"))
    attempt_no = models.PositiveIntegerField(bi("Attempt", "المحاولة"))
    api = models.CharField(bi("API", "الواجهة"), max_length=20,
                           choices=[("compliance_check", "compliance_check"), ("clearance", "clearance"),
                                    ("reporting", "reporting")])
    environment = models.CharField(bi("Environment", "البيئة"), max_length=10, choices=Environment.choices)
    request_sha256 = models.CharField(bi("Request SHA-256", "تجزئة الطلب"), max_length=64)
    http_status = models.IntegerField(bi("HTTP status", "رمز الاستجابة"), null=True, blank=True)
    response_json = models.JSONField(bi("Response", "الاستجابة"), null=True, blank=True)
    outcome = models.CharField(bi("Outcome", "النتيجة"), max_length=30, choices=Outcome.choices)
    started_at = models.DateTimeField(bi("Started at", "بدأت"))
    finished_at = models.DateTimeField(bi("Finished at", "انتهت"))
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")

    class Meta:
        verbose_name = bi("submission attempt", "محاولة إرسال")
        verbose_name_plural = bi("submission attempts", "محاولات الإرسال")
        ordering = ["einvoice", "attempt_no"]
        constraints = [models.UniqueConstraint(fields=["einvoice", "attempt_no"], name="zatca_attempt_no_unique")]

    def save(self, *args, **kwargs):
        if self.entity_id != self.einvoice.entity_id:
            raise ZatcaError("An attempt belongs to its e-invoice's entity.", "تتبع المحاولة كيان الفاتورة.",
                             code="entity_mismatch")
        super().save(*args, **kwargs)


class ValidationMessage(AppendOnly):
    class Level(models.TextChoices):
        ERROR = "error", bi("Error", "خطأ")
        WARNING = "warning", bi("Warning", "تحذير")
        INFO = "info", bi("Information", "معلومة")

    class Origin(models.TextChoices):
        LOCAL_XSD = "local_xsd", bi("Local XSD check", "فحص المخطط المحلي")
        LOCAL_RULES = "local_rules", bi("Local rules", "القواعد المحلية")
        ZATCA = "zatca", bi("ZATCA", "الهيئة")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    attempt = models.ForeignKey(SubmissionAttempt, null=True, blank=True, on_delete=models.PROTECT,
                                related_name="messages", verbose_name=bi("Attempt", "المحاولة"))
    einvoice = models.ForeignKey(EInvoice, on_delete=models.PROTECT, related_name="messages",
                                 verbose_name=bi("E-invoice", "الفاتورة الإلكترونية"))
    level = models.CharField(bi("Level", "المستوى"), max_length=10, choices=Level.choices)
    origin = models.CharField(bi("Origin", "المصدر"), max_length=12, choices=Origin.choices)
    code = models.CharField(bi("Code", "الرمز"), max_length=100)
    category = models.CharField(bi("Category", "التصنيف"), max_length=100, blank=True)
    message_en = models.TextField(bi("Message (English)", "الرسالة (بالإنجليزية)"))
    message_ar = models.TextField(bi("Message (Arabic)", "الرسالة (بالعربية)"), blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = bi("validation message", "رسالة تحقق")
        verbose_name_plural = bi("validation messages", "رسائل التحقق")
        ordering = ["einvoice", "id"]

    @property
    def display_ar(self):
        from .messages import UNTRANSLATED_AR

        return self.message_ar or f"{UNTRANSLATED_AR}: {self.message_en}"

    def save(self, *args, **kwargs):
        if self.entity_id != self.einvoice.entity_id:
            raise ZatcaError("A message belongs to its e-invoice's entity.", "تتبع الرسالة كيان الفاتورة.",
                             code="entity_mismatch")
        super().save(*args, **kwargs)


# --- 3.7 events, 3.8 tasks, alerts -----------------------------------------------------------------

class OnboardingEvent(AppendOnly):
    class Action(models.TextChoices):
        CSR_GENERATED = "csr_generated", bi("CSR generated", "أُنشئ طلب الشهادة")
        OTP_ENTERED = "otp_entered", bi("OTP entered", "أُدخل رمز التحقق")
        OTP_REJECTED = "otp_rejected", bi("OTP rejected", "رُفض رمز التحقق")
        COMPLIANCE_CSID_ISSUED = "compliance_csid_issued", bi("Compliance CSID issued", "صدرت شهادة الامتثال")
        COMPLIANCE_CHECKS_PASSED = "compliance_checks_passed", bi("Compliance checks passed", "اجتازت فحوص الامتثال")
        COMPLIANCE_CHECKS_FAILED = "compliance_checks_failed", bi("Compliance checks failed", "لم تجتز فحوص الامتثال")
        PCSID_ISSUED = "pcsid_issued", bi("Production CSID issued", "صدرت شهادة الإنتاج")
        PCSID_RENEWED = "pcsid_renewed", bi("Production CSID renewed", "جُددت شهادة الإنتاج")
        REVOKED = "revoked", bi("Revoked", "أُلغيت")
        ENVIRONMENT_CHANGED = "environment_changed", bi("Environment changed", "تغيرت البيئة")
        DELEGATION_GRANTED = "delegation_granted", bi("Delegation granted", "مُنح التفويض")
        DELEGATION_REVOKED = "delegation_revoked", bi("Delegation revoked", "أُلغي التفويض")
        VAT_STATUS_CHANGED = "vat_status_changed", bi("VAT status changed", "تغيرت الحالة الضريبية")
        SECRET_STORE_UNAVAILABLE = "secret_store_unavailable", bi("Secret store unavailable", "مخزن الأسرار غير متاح")
        SECRET_STORE_AVAILABLE = "secret_store_available", bi("Secret store available", "مخزن الأسرار متاح")
        MASTER_KEY_CREATED = "master_key_created", bi("Master key created", "أُنشئ المفتاح الرئيسي")
        MASTER_KEY_ROTATED = "master_key_rotated", bi("Master key rotated", "دُوِّر المفتاح الرئيسي")
        SUPER_KEY_REGISTERED = "super_key_registered", bi("Group super key registered", "سُجل المفتاح الأعلى")
        PROFILE_ENABLED = "profile_enabled", bi("Profile enabled", "فُعّل الملف")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    egs_unit = models.ForeignKey(EgsUnit, null=True, blank=True, on_delete=models.PROTECT, related_name="events",
                                 verbose_name=bi("EGS unit", "وحدة الفوترة"))
    action = models.CharField(bi("Action", "الإجراء"), max_length=30, choices=Action.choices)
    actor = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Actor", "المنفذ"))
    at = models.DateTimeField(bi("At", "الوقت"), default=timezone.now)
    detail = models.JSONField(bi("Detail (never secrets)", "التفاصيل (دون أسرار)"), default=dict)

    class Meta:
        verbose_name = bi("onboarding event", "حدث تسجيل")
        verbose_name_plural = bi("onboarding events", "أحداث التسجيل")
        ordering = ["at", "id"]


class ComplianceTask(Tracked, ServiceWritten):
    class Kind(models.TextChoices):
        LATE_REPORTING_NOTICE = "late_reporting_notice", bi("Late reporting notice", "إشعار تأخر الإبلاغ")

    class Status(models.TextChoices):
        OPEN = "open", bi("Open", "مفتوحة")
        NOTIFIED = "notified", bi("ZATCA notified", "أُبلغت الهيئة")
        DONE = "done", bi("Done", "منجزة")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    einvoice = models.ForeignKey(EInvoice, on_delete=models.PROTECT, related_name="tasks",
                                 verbose_name=bi("E-invoice", "الفاتورة الإلكترونية"))
    kind = models.CharField(bi("Kind", "النوع"), max_length=30, choices=Kind.choices)
    title_en = models.CharField(bi("Title (English)", "العنوان (بالإنجليزية)"), max_length=300)
    title_ar = models.CharField(bi("Title (Arabic)", "العنوان (بالعربية)"), max_length=300)
    instructions_en = models.TextField(bi("Instructions (English)", "التعليمات (بالإنجليزية)"))
    instructions_ar = models.TextField(bi("Instructions (Arabic)", "التعليمات (بالعربية)"))
    status = models.CharField(bi("Status", "الحالة"), max_length=10, choices=Status.choices, default=Status.OPEN)
    notified_at = models.DateTimeField(bi("ZATCA notified at", "تاريخ إبلاغ الهيئة"), null=True, blank=True)
    notice_reference = models.CharField(bi("Notice reference", "مرجع الإشعار"), max_length=200, blank=True)
    recovered_notified_at = models.DateTimeField(bi("Recovery notified at", "تاريخ إشعار انتهاء العطل"), null=True,
                                                 blank=True)
    recovery_notice_reference = models.CharField(bi("Recovery notice reference", "مرجع إشعار انتهاء العطل"),
                                                 max_length=200, blank=True)
    evidence = models.FileField(bi("Evidence", "الدليل"), upload_to="zatca/tasks/", null=True, blank=True)
    done_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                                verbose_name=bi("Done by", "أنجزها"))

    class Meta:
        verbose_name = bi("compliance task", "مهمة امتثال")
        verbose_name_plural = bi("compliance tasks", "مهام الامتثال")
        constraints = [models.UniqueConstraint(fields=["einvoice", "kind"], name="zatca_one_task_per_kind")]

    def save(self, *args, **kwargs):
        if self.entity_id != self.einvoice.entity_id:
            raise ZatcaError("A task belongs to its e-invoice's entity.", "تتبع المهمة كيان الفاتورة.",
                             code="entity_mismatch")
        if self.pk:
            stored = type(self)._base_manager.filter(pk=self.pk).values_list("status", flat=True).first()
            if stored == self.Status.DONE:  # done is final
                raise _immutable()
        super().save(*args, **kwargs)


class ZatcaAlert(AppendOnly):
    """An alert to a named person (en/ar), e.g. R-10, R-23, R-44, R-47. Delivery channels are not built."""

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    recipient = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+",
                                  verbose_name=bi("Recipient", "المستلم"))
    kind = models.CharField(bi("Kind", "النوع"), max_length=40)
    einvoice = models.ForeignKey(EInvoice, null=True, blank=True, on_delete=models.PROTECT, related_name="alerts")
    egs_unit = models.ForeignKey(EgsUnit, null=True, blank=True, on_delete=models.PROTECT, related_name="alerts")
    message_en = models.TextField(bi("Message (English)", "الرسالة (بالإنجليزية)"))
    message_ar = models.TextField(bi("Message (Arabic)", "الرسالة (بالعربية)"))
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = bi("ZATCA alert", "تنبيه الربط مع الهيئة")
        verbose_name_plural = bi("ZATCA alerts", "تنبيهات الربط مع الهيئة")
        ordering = ["-created_at", "-id"]


# --- 3.9 delegations, 3.10 key hierarchy ----------------------------------------------------------

class KeyCustodianDelegation(Tracked, ServiceWritten):
    class Scope(models.TextChoices):
        ONBOARD_RENEW = "onboard_renew", bi("Onboard and renew", "التسجيل والتجديد")
        REVOKE = "revoke", bi("Revoke", "الإلغاء")
        RESTORE = "restore", bi("Restore", "الاستعادة")
        MASTER_KEY = "master_key", bi("Entity master key custody", "حفظ المفتاح الرئيسي للكيان")
        # "unseal" was withdrawn by D-12 (2026-10-17); it is not a choice and is refused by the services.

    FIXED = ("entity_id", "custodian_id", "scope", "granted_by_id", "granted_at", "valid_until", "openbao_policy_ref")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Entity", "الكيان"))
    custodian = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+",
                                  verbose_name=bi("Custodian", "أمين المفاتيح"))
    scope = models.CharField(bi("Scope", "النطاق"), max_length=20, choices=Scope.choices)
    granted_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+",
                                   verbose_name=bi("Granted by", "منحه"))
    granted_at = models.DateTimeField(bi("Granted at", "تاريخ المنح"), default=timezone.now)
    valid_until = models.DateTimeField(bi("Valid until", "ساري حتى"), null=True, blank=True)
    openbao_policy_ref = models.CharField(bi("OpenBao policy", "سياسة OpenBao"), max_length=200)
    revoked_at = models.DateTimeField(bi("Revoked at", "تاريخ الإلغاء"), null=True, blank=True)
    revoked_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                                   verbose_name=bi("Revoked by", "ألغاه"))
    revoke_reason = models.CharField(bi("Revoke reason", "سبب الإلغاء"), max_length=300, blank=True)

    class Meta:
        verbose_name = bi("key custodian delegation", "تفويض حفظ المفاتيح")
        verbose_name_plural = bi("key custodian delegations", "تفويضات حفظ المفاتيح")

    def save(self, *args, **kwargs):
        if self.pk:
            stored = type(self)._base_manager.filter(pk=self.pk).values(*self.FIXED, "revoked_at").first()
            if stored and (stored["revoked_at"] is not None or any(stored[f] != getattr(self, f) for f in self.FIXED)):
                raise _immutable()  # append-only except the single revocation write
        super().save(*args, **kwargs)

    def is_active(self, at=None):
        at = at or timezone.now()
        return self.revoked_at is None and (self.valid_until is None or self.valid_until > at) and self.granted_at <= at


def _key_ref_guard(obj, stored):
    """§3.10 / finding 16: entity fixed; a new reference only with a higher version, a new version only with a new
    reference (the ERP never points an old version number at another key)."""
    if stored is None:
        return
    ref_changed = stored["key_ref"] != obj.key_ref
    version_changed = stored["key_version"] != obj.key_version
    if stored["entity_id"] != obj.entity_id or obj.key_version < stored["key_version"] or ref_changed != version_changed:
        raise _immutable()


class GroupSuperKey(Tracked, ServiceWritten):
    entity = models.OneToOneField(Entity, on_delete=models.PROTECT, related_name="+",
                                  verbose_name=bi("Group parent", "الشركة الأم"))
    key_ref = models.CharField(bi("Key reference", "مرجع المفتاح"), max_length=300)
    key_version = models.PositiveIntegerField(bi("Key version", "نسخة المفتاح"), default=1)
    holder = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+", verbose_name=bi("Holder", "الحائز"))

    class Meta:
        verbose_name = bi("group super key (reference)", "المفتاح الأعلى للمجموعة (مرجع)")
        verbose_name_plural = bi("group super keys (references)", "المفاتيح العليا للمجموعات (مراجع)")

    def save(self, *args, **kwargs):
        if self.pk:
            _key_ref_guard(self, type(self)._base_manager.filter(pk=self.pk).values(
                "entity_id", "key_ref", "key_version").first())
        super().save(*args, **kwargs)


class EntityMasterKey(Tracked, ServiceWritten):
    entity = models.OneToOneField(Entity, on_delete=models.PROTECT, related_name="+",
                                  verbose_name=bi("Entity", "الكيان"))
    key_ref = models.CharField(bi("Key reference", "مرجع المفتاح"), max_length=300)
    key_version = models.PositiveIntegerField(bi("Key version", "نسخة المفتاح"), default=1)
    protected_by = models.ForeignKey(GroupSuperKey, on_delete=models.PROTECT, related_name="master_keys",
                                     verbose_name=bi("Protected by", "محمي بـ"))

    class Meta:
        verbose_name = bi("entity master key (reference)", "المفتاح الرئيسي للكيان (مرجع)")
        verbose_name_plural = bi("entity master keys (references)", "المفاتيح الرئيسية للكيانات (مراجع)")

    def save(self, *args, **kwargs):
        if self.pk:
            stored = type(self)._base_manager.filter(pk=self.pk).values("entity_id", "protected_by_id", "key_ref",
                                                                         "key_version").first()
            if stored and (stored["protected_by_id"] != self.protected_by_id
                           or self.key_version <= stored["key_version"]):
                raise _immutable()  # only new versions
            _key_ref_guard(self, stored)
        super().save(*args, **kwargs)


def check_no_secret(value, what="detail"):
    """Refuse obvious key material in free-form fields (R-6)."""
    text = str(value)
    if "PRIVATE KEY" in text:
        raise ValidationError(f"{what} must never hold key material (R-6). / يجب ألا يحتوي على مواد مفاتيح.")
