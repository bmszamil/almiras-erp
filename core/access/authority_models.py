"""Authority matrix, approval engine, delegation, assistants and governance report models (access.md §10.3).

Loaded by ``core.access.models`` so they belong to the ``core`` app. Every model has an ``entity`` FK except the
global ``ApprovalActivity`` catalogue (like ``Role``). Records are append-only except where a docstring says which
single write is allowed; writes go through ``core.access`` services (``_service_write``). On PostgreSQL, migration
``core.0030`` adds triggers freezing non-draft matrix versions and their rows (R77) and generated reports (R85).
"""
import hashlib
import json
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.models import Currency, Entity, bi

from . import authority_defaults
from .models import AccessLog, AppendOnlyError, DualControlRequest, Role


def canonical_hash(value):
    """SHA-256 of ``value`` as canonical JSON (matrix rows, report content)."""
    data = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


class ServiceWrittenQuerySet(models.QuerySet):
    """Bulk writes would skip the model guards (R77, R85)."""

    message = "These records change only through core.access services. / لا تتغير هذه السجلات إلا من خلال خدمات الصلاحيات."

    def update(self, **kwargs):
        raise AppendOnlyError(self.message)

    def delete(self):
        raise AppendOnlyError(self.message)

    def _service_update(self, **kwargs):
        return models.QuerySet.update(self, **kwargs)


class ApprovalActivity(models.Model):
    """One approvable document type (3.8, 10.5). Catalogue data mirrored from ``authority_defaults`` by migration."""

    code = models.CharField(max_length=60, unique=True)
    name_en = models.CharField(max_length=120)
    name_ar = models.CharField(max_length=120)
    permission = models.CharField(max_length=100, help_text="The catalogue approval permission checked (3.6)")
    amount_basis = models.CharField(max_length=20, choices=authority_defaults.AMOUNT_BASES)
    is_pending = models.BooleanField(default=False, help_text="The module has not built this document yet")

    class Meta:
        ordering = ["code"]
        verbose_name = bi("approval activity", "نشاط اعتماد")
        verbose_name_plural = bi("approval activities", "أنشطة الاعتماد")

    @property
    def spec(self):
        return authority_defaults.get(self.code)

    def __str__(self):
        return f"{self.code} — {self.name_en} / {self.name_ar}"


class EntityGovernance(models.Model):
    """Top authority and self-approval switch of one entity (3.8, 10.3). Changed only through
    ``core.services.change_entity_settings`` with a dual-control request (R67, R76)."""

    entity = models.OneToOneField(Entity, on_delete=models.PROTECT, related_name="governance")
    top_authority_role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="+")
    self_approval_enabled = models.BooleanField(default=True)
    governance_report_recipients = models.ManyToManyField(Role, related_name="+", blank=True)

    objects = ServiceWrittenQuerySet.as_manager()

    class Meta:
        verbose_name = bi("entity governance", "حوكمة الكيان")

    def save(self, *args, **kwargs):
        if not getattr(self, "_service_write", False):
            raise AppendOnlyError("Entity governance changes only through change_entity_settings (R67, R76). / "
                                  "تتغير حوكمة الكيان من خلال خدمة إعدادات الكيان فقط.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError("Entity governance is never deleted. / لا تُحذف حوكمة الكيان.")

    def __str__(self):
        return f"{self.entity.code}: {self.top_authority_role.code}"


class AuthorityMatrixVersion(models.Model):
    """One version of an entity's authority matrix (10.3). Only ``draft`` versions change (R76, R77)."""

    class Status(models.TextChoices):
        DRAFT = "draft", bi("Draft", "مسودة")
        SUBMITTED = "submitted", bi("Submitted", "مقدّم")
        APPROVED = "approved", bi("Approved", "معتمد")
        REJECTED = "rejected", bi("Rejected", "مرفوض")
        WITHDRAWN = "withdrawn", bi("Withdrawn", "مسحوب")
        SUPERSEDED = "superseded", bi("Superseded", "مستبدل")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="authority_versions")
    version_no = models.PositiveIntegerField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    effective_from = models.DateField()
    summary_en = models.TextField()
    summary_ar = models.TextField()
    rows_hash = models.CharField(max_length=64, blank=True)
    dual_control_request = models.ForeignKey(DualControlRequest, null=True, blank=True, on_delete=models.PROTECT,
                                             related_name="+")
    proposed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    proposed_at = models.DateTimeField(default=timezone.now)
    submitted_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=500, blank=True)
    superseded_by = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    objects = ServiceWrittenQuerySet.as_manager()

    class Meta:
        ordering = ["entity", "version_no"]
        verbose_name = bi("authority matrix version", "إصدار مصفوفة الصلاحيات")
        constraints = [
            models.UniqueConstraint(fields=["entity", "version_no"], name="matrix_version_unique_no"),
            models.CheckConstraint(condition=Q(decided_by__isnull=True) | ~Q(decided_by=models.F("proposed_by")),
                                   name="matrix_version_decider_not_proposer"),
        ]

    def save(self, *args, **kwargs):
        if not getattr(self, "_service_write", False):
            raise AppendOnlyError("Matrix versions change only through core.access.matrix (R76, R77). / "
                                  "تتغير إصدارات المصفوفة من خلال خدماتها فقط.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError("Matrix versions are never deleted. / لا تُحذف إصدارات المصفوفة.")

    @property
    def is_draft(self):
        return self.status == self.Status.DRAFT

    def __str__(self):
        return f"{self.entity.code} v{self.version_no} ({self.status})"


class AuthorityRule(models.Model):
    """One row of a matrix version (3.8 amended by 10.3): who approves one step of an activity in an amount band."""

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="authority_rules")
    version = models.ForeignKey(AuthorityMatrixVersion, on_delete=models.PROTECT, related_name="rules")
    activity = models.ForeignKey(ApprovalActivity, on_delete=models.PROTECT, related_name="rules")
    band_currency = models.ForeignKey(Currency, to_field="code", null=True, blank=True, on_delete=models.PROTECT,
                                      related_name="+", help_text="Empty: the entity's functional currency (R73)")
    amount_from = models.DecimalField(max_digits=18, decimal_places=3, default=Decimal("0"))
    amount_to = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True,
                                    help_text="Inclusive upper limit; empty: no upper limit")
    step_no = models.PositiveSmallIntegerField(default=1)
    min_approvals = models.PositiveSmallIntegerField(default=1)
    approver_roles = models.ManyToManyField(Role, blank=True, related_name="+")
    approver_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                      related_name="+")
    delegation_allowed = models.BooleanField(default=True)
    note_en = models.CharField(max_length=300, blank=True)
    note_ar = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    objects = ServiceWrittenQuerySet.as_manager()

    class Meta:
        ordering = ["version", "activity", "band_currency", "amount_from", "step_no", "id"]
        verbose_name = bi("authority matrix row", "سطر مصفوفة الصلاحيات")
        constraints = [
            models.CheckConstraint(condition=Q(amount_to__isnull=True) | Q(amount_to__gte=models.F("amount_from")),
                                   name="authority_rule_band_order"),
            models.CheckConstraint(condition=Q(amount_from__gte=0), name="authority_rule_band_not_negative"),
            models.CheckConstraint(condition=Q(min_approvals__gte=1) & Q(step_no__gte=1),
                                   name="authority_rule_step_and_count"),
        ]

    def _version_is_draft(self):
        status = AuthorityMatrixVersion.objects.filter(pk=self.version_id).values_list("status", flat=True).first()
        return status == AuthorityMatrixVersion.Status.DRAFT

    def save(self, *args, **kwargs):
        if not getattr(self, "_service_write", False) or not self._version_is_draft():
            raise AppendOnlyError("Rows of a submitted or approved matrix version never change; draft a new version "
                                  "(R77). / لا تتغير أسطر إصدار مقدّم أو معتمد؛ أعدّ إصدارًا جديدًا.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if not getattr(self, "_service_write", False) or not self._version_is_draft():
            raise AppendOnlyError("Rows of a submitted or approved matrix version never change (R77). / "
                                  "لا تتغير أسطر إصدار مقدّم أو معتمد.")
        super().delete(*args, **kwargs)

    def band_text(self):
        to = "∞" if self.amount_to is None else self.amount_to
        return f"{self.band_currency_id or self.entity.base_currency_id} {self.amount_from}–{to}"

    def snapshot(self):
        """The row as plain data (rows hash, diffs, governance report)."""
        return {
            "id": self.pk, "activity": self.activity.code, "band_currency": self.band_currency_id,
            "amount_from": str(self.amount_from), "amount_to": None if self.amount_to is None else str(self.amount_to),
            "step_no": self.step_no, "min_approvals": self.min_approvals,
            "approver_roles": sorted(self.approver_roles.values_list("code", flat=True)),
            "approver_user": self.approver_user_id, "delegation_allowed": self.delegation_allowed,
            "note_en": self.note_en, "note_ar": self.note_ar,
        }

    def __str__(self):
        return f"{self.entity.code} v{self.version.version_no} {self.activity.code} step {self.step_no}"


class Revocable(models.Model):
    """Append-only rows whose only change is one revocation write (delegations, assistant assignments)."""

    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="+")
    revoke_reason = models.CharField(max_length=500, blank=True)

    objects = ServiceWrittenQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not getattr(self, "_service_write", False):
            raise AppendOnlyError("These records change only through core.access services; a change is a revocation "
                                  "and a new record. / لا تتغير إلا من خلال خدمات الصلاحيات.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError("These records are never deleted. / لا تُحذف هذه السجلات.")

    def is_active_at(self, at):
        return self.valid_from <= at < self.valid_to and (self.revoked_at is None or self.revoked_at > at)


def active_window_q(at):
    return Q(valid_from__lte=at, valid_to__gt=at) & (Q(revoked_at__isnull=True) | Q(revoked_at__gt=at))


class ApprovalDelegation(Revocable):
    """Dated delegation of approval authority (3.9, R62; amended by R87, R88). Always ends (``valid_to``)."""

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="approval_delegations")
    delegator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    delegate = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    activities = models.ManyToManyField(ApprovalActivity, blank=True, related_name="+",
                                        help_text="Empty: every activity the delegator may approve")
    amount_max = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    valid_from = models.DateTimeField()
    valid_to = models.DateTimeField()
    reason_en = models.CharField(max_length=500)
    reason_ar = models.CharField(max_length=500)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    notified_expiry_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["entity", "valid_from", "id"]
        verbose_name = bi("approval delegation", "تفويض اعتماد")
        constraints = [
            models.CheckConstraint(condition=Q(valid_to__gt=models.F("valid_from")), name="delegation_ends_after_start"),
            models.CheckConstraint(condition=~Q(delegator=models.F("delegate")), name="delegation_not_to_self"),
        ]

    def __str__(self):
        return f"{self.entity.code}: {self.delegator} → {self.delegate}"


class AssistantAssignment(Revocable):
    """A personal assistant drafts for a principal (10.3, R81): the principal counts as a creator."""

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="assistant_assignments")
    assistant = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    valid_from = models.DateTimeField()
    valid_to = models.DateTimeField()
    reason = models.CharField(max_length=500)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["entity", "valid_from", "id"]
        verbose_name = bi("assistant assignment", "تعيين مساعد شخصي")
        constraints = [
            models.CheckConstraint(condition=Q(valid_to__gt=models.F("valid_from")), name="assistant_ends_after_start"),
            models.CheckConstraint(condition=~Q(assistant=models.F("principal")), name="assistant_not_own_principal"),
        ]

    def __str__(self):
        return f"{self.entity.code}: {self.assistant} drafts for {self.principal}"


class ApprovalRequest(models.Model):
    """One approval of one document version (3.10, 10.3). Rules are resolved and stored when it opens (R77)."""

    class Status(models.TextChoices):
        OPEN = "open", bi("Open", "مفتوح")
        APPROVED = "approved", bi("Approved", "معتمد")
        REJECTED = "rejected", bi("Rejected", "مرفوض")
        RETURNED = "returned", bi("Returned", "معاد")
        SUPERSEDED = "superseded", bi("Superseded by a new version", "حل محله إصدار أحدث")

    class Source(models.TextChoices):
        MATRIX = "matrix", bi("Authority matrix", "مصفوفة الصلاحيات")
        DEFAULT = "default", bi("Code default", "الإعداد الافتراضي في النظام")

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="approval_requests")
    activity = models.ForeignKey(ApprovalActivity, on_delete=models.PROTECT, related_name="+")
    document_type = models.CharField(max_length=100, help_text="app_label.model of the document")
    document_id = models.PositiveBigIntegerField()
    document_version = models.CharField(max_length=64, blank=True)
    label_en = models.CharField(max_length=200, blank=True)
    label_ar = models.CharField(max_length=200, blank=True)
    amount = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    functional_amount = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    matrix_version = models.ForeignKey(AuthorityMatrixVersion, null=True, blank=True, on_delete=models.PROTECT,
                                       related_name="+")
    rule_source = models.CharField(max_length=7, choices=Source.choices)
    rule_ids = models.JSONField(default=list)
    steps = models.JSONField(default=list, help_text="[{step_no, min_approvals, rule_ids, delegation_allowed}]")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    self_approval = models.BooleanField(default=False)
    top_authority = models.BooleanField(default=False)
    drafted_for = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+")
    opened_at = models.DateTimeField(default=timezone.now)
    closed_at = models.DateTimeField(null=True, blank=True)

    objects = ServiceWrittenQuerySet.as_manager()

    class Meta:
        ordering = ["-opened_at", "-id"]
        verbose_name = bi("approval request", "طلب اعتماد")
        indexes = [models.Index(fields=["document_type", "document_id"], name="approval_request_document")]

    def save(self, *args, **kwargs):
        if not getattr(self, "_service_write", False):
            raise AppendOnlyError("Approval requests change only through the approval engine (R61). / "
                                  "لا تتغير طلبات الاعتماد إلا من خلال محرك الاعتماد.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError("Approval requests are never deleted. / لا تُحذف طلبات الاعتماد.")

    def __str__(self):
        return f"{self.entity.code} {self.activity.code} {self.document_type}#{self.document_id} ({self.status})"


class ApprovalDecision(models.Model):
    """One person's decision on one step of a request (append-only, R75)."""

    class Decision(models.TextChoices):
        APPROVED = "approved", bi("Approved", "معتمد")
        REJECTED = "rejected", bi("Rejected", "مرفوض")
        RETURNED = "returned", bi("Returned", "معاد")
        SKIPPED = "skipped", bi("Step skipped: every eligible approver is the beneficiary", "تُجووزت: كل المعتمدين مستفيدون")

    request = models.ForeignKey(ApprovalRequest, on_delete=models.PROTECT, related_name="decisions")
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    step_no = models.PositiveSmallIntegerField()
    decision = models.CharField(max_length=10, choices=Decision.choices)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                             related_name="+")
    on_behalf_of = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+")
    delegation = models.ForeignKey(ApprovalDelegation, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="decisions")
    rule_id = models.PositiveBigIntegerField(null=True, blank=True)
    rule_source = models.CharField(max_length=7, choices=ApprovalRequest.Source.choices)
    self_approval = models.BooleanField(default=False)
    top_authority = models.BooleanField(default=False)
    permission = models.CharField(max_length=100)
    grant_log = models.ForeignKey(AccessLog, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    note = models.CharField(max_length=500, blank=True, help_text="Reason (Arabic, or as given)")
    note_en = models.CharField(max_length=500, blank=True, help_text="Reason in English, optional (web.md R8)")
    at = models.DateTimeField(default=timezone.now)

    objects = ServiceWrittenQuerySet.as_manager()

    class Meta:
        ordering = ["at", "id"]
        verbose_name = bi("approval decision", "قرار اعتماد")

    def save(self, *args, **kwargs):
        if self.pk is not None or not getattr(self, "_service_write", False):
            raise AppendOnlyError("Approval decisions are final. / قرارات الاعتماد نهائية.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError("Approval decisions are final. / قرارات الاعتماد نهائية.")


class AccessAlert(models.Model):
    """A bilingual in-app alert to one user (R72 no eligible approver, R86 unreviewed report, R87 expiry reminder).
    ``key`` makes each alert be written once."""

    class Kind(models.TextChoices):
        NO_ELIGIBLE_APPROVER = "no_eligible_approver", bi("No eligible approver", "لا يوجد معتمد مؤهل")
        DELEGATION_EXPIRING = "delegation_expiring", bi("Delegation expiring", "قرب انتهاء التفويض")
        REPORT_UNREVIEWED = "report_unreviewed", bi("Governance report not reviewed", "تقرير الحوكمة لم يُراجع")
        REPORT_MISSING_USER = "report_missing_user", bi("Governance report not generated", "لم يُنشأ تقرير الحوكمة")
        MFA_LOCKED = "mfa_locked", bi("Two-step verification locked again", "قُفل التحقق بخطوتين مرة أخرى")  # web OQ-30

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="access_alerts")
    kind = models.CharField(max_length=30, choices=Kind.choices)
    message_en = models.TextField()
    message_ar = models.TextField()
    details = models.JSONField(default=dict)
    key = models.CharField(max_length=200, unique=True)
    created_at = models.DateTimeField(default=timezone.now)

    objects = ServiceWrittenQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = bi("access alert", "تنبيه الصلاحيات")

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise AppendOnlyError("Alerts are not edited. / لا تُعدَّل التنبيهات.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError("Alerts are kept. / تُحفظ التنبيهات.")


class GovernanceReport(models.Model):
    """The monthly bilingual governance / internal-audit report of one entity (R84). Immutable once generated
    (R85); the only write after generation is the internal auditor's single review (R86)."""

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="governance_reports")
    month = models.DateField(help_text="First day of the month reported")
    revision = models.PositiveSmallIntegerField(default=1)
    replaces = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    generated_at = models.DateTimeField(default=timezone.now)
    generated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    content = models.JSONField()
    content_hash = models.CharField(max_length=64)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note_en = models.TextField(blank=True)
    review_note_ar = models.TextField(blank=True)

    objects = ServiceWrittenQuerySet.as_manager()

    REVIEW_FIELDS = ("reviewed_by_id", "reviewed_at", "review_note_en", "review_note_ar")
    FROZEN_FIELDS = ("entity_id", "month", "revision", "replaces_id", "generated_at", "generated_by_id", "content",
                     "content_hash")

    class Meta:
        ordering = ["entity", "-month", "-revision"]
        verbose_name = bi("governance report", "تقرير الحوكمة")
        constraints = [
            models.UniqueConstraint(fields=["entity", "month", "revision"], name="governance_report_unique_revision"),
        ]

    def save(self, *args, **kwargs):
        if not getattr(self, "_service_write", False):
            raise AppendOnlyError("Governance reports are generated and reviewed only through their services (R85). / "
                                  "تُنشأ تقارير الحوكمة وتُراجع من خلال خدماتها فقط.")
        if self.pk is not None:
            stored = GovernanceReport.objects.filter(pk=self.pk).values(*self.FROZEN_FIELDS, *self.REVIEW_FIELDS).first()
            if stored and (any(stored[f] != getattr(self, f) for f in self.FROZEN_FIELDS)
                           or stored["reviewed_at"] is not None):
                raise AppendOnlyError("A generated governance report never changes; issue a new revision (R85). / "
                                      "لا يتغير تقرير الحوكمة بعد إنشائه؛ أصدر مراجعة جديدة.")
        elif self.content_hash != canonical_hash(self.content):
            raise AppendOnlyError("The report content does not match its hash. / محتوى التقرير لا يطابق بصمته.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError("Governance reports are never deleted. / لا تُحذف تقارير الحوكمة.")

    def __str__(self):
        return f"{self.entity.code} {self.month:%Y-%m} r{self.revision}"


def _freeze_approver_roles(sender, instance, action, reverse, pk_set, **kwargs):
    """R77 (review of 92c35c2, item 13): ``approver_roles`` of a row in a submitted, approved or superseded version
    never change through the relation (add, remove, set, clear), on any database. Direct writes to the auto-created
    through table send no Django signal (not even pre_save); PostgreSQL's core.0030 trigger refuses them, SQLite
    does not (verification review of 49e1abd, item 9; recorded in access.md 10.10)."""
    if action not in ("pre_add", "pre_remove", "pre_clear"):
        return
    if reverse:  # from the role's side: every row touched
        rule_ids = set(pk_set or ()) if action != "pre_clear" else set(
            sender.objects.filter(role_id=instance.pk).values_list("authorityrule_id", flat=True))
    else:
        rule_ids = {instance.pk}
    frozen = AuthorityRule.objects.filter(pk__in=rule_ids).exclude(
        version__status=AuthorityMatrixVersion.Status.DRAFT).exists()
    if frozen:
        raise AppendOnlyError("Rows of a submitted or approved matrix version never change (R77). / "
                              "لا تتغير أسطر إصدار مقدّم أو معتمد.")


models.signals.m2m_changed.connect(_freeze_approver_roles, sender=AuthorityRule.approver_roles.through,
                                   dispatch_uid="core_access_freeze_approver_roles")
