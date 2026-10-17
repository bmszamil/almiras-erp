"""Generic approval engine: document types, policy matrix (data, not code), requests,
append-only actions, delegations, in-app notifications and push-device hooks.

Business rules live in approvals.services. Model guards here only protect data that must
never change (used policies, append-only actions).
"""
import datetime
from decimal import Decimal

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models import Q
from django.utils import timezone

from access.errors import RuleError
from access.models import AppendOnlyModel, Role
from core.models import Entity

USER = settings.AUTH_USER_MODEL


class ApprovableType(models.Model):
    code = models.CharField(max_length=60, unique=True, help_text="<module>.<document>, e.g. ledger.journal_entry")
    name_en = models.CharField(max_length=100)
    name_ar = models.CharField(max_length=100)
    content_type = models.ForeignKey(ContentType, on_delete=models.PROTECT, related_name="+")
    is_spending = models.BooleanField(default=False, help_text="Payments/spending covered by D2")

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return self.code


class ApprovalPolicy(models.Model):
    entity = models.ForeignKey(Entity, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                               help_text="Empty = group-wide unless an entity-specific policy exists")
    document_type = models.ForeignKey(ApprovableType, on_delete=models.PROTECT, related_name="policies")
    amount_min = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True, help_text="Inclusive")
    amount_max = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True, help_text="Exclusive")
    currency = models.CharField(max_length=3, default="SAR")
    priority = models.IntegerField(default=0)
    name_en = models.CharField(max_length=200)
    name_ar = models.CharField(max_length=200)
    valid_from = models.DateField(default=timezone.localdate)
    valid_to = models.DateField(null=True, blank=True, help_text="Inclusive; empty = open-ended")
    expire_after_hours = models.PositiveIntegerField(
        null=True, blank=True, help_text="Pending requests expire after this many hours. Empty = never (OQ8)."
    )
    is_active = models.BooleanField(default=True)

    # Fields that may still change after the policy has been used (retiring it).
    RETIRE_FIELDS = {"is_active", "valid_to"}

    class Meta:
        verbose_name_plural = "approval policies"
        ordering = ["document_type", "entity", "-priority", "amount_min"]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount_min__isnull=True) | Q(amount_max__isnull=True) | Q(amount_max__gt=models.F("amount_min")),
                name="policy_amount_range_valid",
            ),
        ]

    @property
    def is_used(self):
        return self.pk is not None and self.requests.exists()

    def overlaps(self, other):
        """Same scope (entity, type, currency, priority) with overlapping amount and date ranges."""
        if (self.entity_id, self.document_type_id, self.currency, self.priority) != (
            other.entity_id, other.document_type_id, other.currency, other.priority
        ):
            return False
        inf = Decimal("1e30")
        a_lo, a_hi = self.amount_min if self.amount_min is not None else -inf, self.amount_max if self.amount_max is not None else inf
        b_lo, b_hi = other.amount_min if other.amount_min is not None else -inf, other.amount_max if other.amount_max is not None else inf
        if not (a_lo < b_hi and b_lo < a_hi):
            return False
        far = datetime.date.max
        return self.valid_from <= (other.valid_to or far) and other.valid_from <= (self.valid_to or far)

    def save(self, *args, **kwargs):
        if self.pk is not None and self.is_used:
            stored = ApprovalPolicy.objects.get(pk=self.pk)
            changed = {
                f.attname for f in self._meta.concrete_fields
                if getattr(stored, f.attname) != getattr(self, f.attname)
            }
            if changed - self.RETIRE_FIELDS:
                raise RuleError(
                    "policy_in_use",
                    "This policy has been used. Create a new policy version instead of editing it.",
                    "تم استخدام هذه السياسة. أنشئ نسخة جديدة بدلاً من تعديلها.",
                )
        if self.is_active:
            # T20 / R5: no overlapping ranges at the same entity, type, currency and priority.
            # SQLite has no exclusion constraints; on PostgreSQL this guard runs inside the
            # caller's transaction (see Build notes).
            others = ApprovalPolicy.objects.filter(
                document_type_id=self.document_type_id, entity_id=self.entity_id,
                currency=self.currency, priority=self.priority, is_active=True,
            ).exclude(pk=self.pk)
            if any(self.overlaps(o) for o in others):
                raise RuleError(
                    "policy_overlap",
                    "Another active policy with the same entity, type and priority covers an overlapping amount range.",
                    "توجد سياسة نشطة أخرى بنفس المنشأة والنوع والأولوية تغطي نطاق مبالغ متداخلاً.",
                )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.is_used:
            raise RuleError("policy_in_use", "Used policies cannot be deleted; deactivate them.",
                            "لا يمكن حذف سياسة مستخدمة؛ قم بتعطيلها.")
        return super().delete(*args, **kwargs)

    def __str__(self):
        scope = self.entity.code if self.entity_id else "group"
        return f"{self.document_type.code} [{scope}] {self.amount_min or '-'}..{self.amount_max or '-'} {self.currency} p{self.priority}"


class ApprovalStep(models.Model):
    policy = models.ForeignKey(ApprovalPolicy, on_delete=models.PROTECT, related_name="steps")
    sequence = models.PositiveIntegerField(default=1, help_text="Equal sequence = parallel steps")
    approver_role = models.ForeignKey(Role, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    approver_user = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    min_approvals = models.PositiveIntegerField(default=1)
    escalate_after_hours = models.PositiveIntegerField(null=True, blank=True)
    escalate_to_role = models.ForeignKey(Role, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    escalate_to_user = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["policy", "sequence", "id"]
        constraints = [
            models.CheckConstraint(
                condition=(Q(approver_role__isnull=False) & Q(approver_user__isnull=True))
                | (Q(approver_role__isnull=True) & Q(approver_user__isnull=False)),
                name="step_one_approver_kind",
            ),
            models.CheckConstraint(condition=Q(min_approvals__gte=1), name="step_min_approvals_positive"),
        ]

    def _guard(self):
        if self.policy_id and ApprovalPolicy.objects.get(pk=self.policy_id).is_used:
            raise RuleError("policy_in_use", "Steps of a used policy cannot change. Create a new policy version.",
                            "لا يمكن تغيير خطوات سياسة مستخدمة. أنشئ نسخة جديدة.")

    def save(self, *args, **kwargs):
        self._guard()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self._guard()
        return super().delete(*args, **kwargs)

    def __str__(self):
        who = self.approver_role.code if self.approver_role_id else self.approver_user.username
        return f"step {self.sequence}: {who}"


class ApprovalRequestQuerySet(models.QuerySet):
    """Requests change only one at a time through approvals.services: no bulk update/delete/create."""

    def update(self, **kwargs):
        raise RuleError("request_bulk_change", "Approval requests cannot be bulk-changed.",
                        "لا يمكن تعديل طلبات الموافقة جماعياً.")

    def delete(self):
        raise RuleError("request_no_delete", "Approval requests cannot be deleted.", "لا يمكن حذف طلبات الموافقة.")

    def bulk_create(self, *args, **kwargs):
        raise RuleError("request_bulk_change", "Approval requests are created by approvals.services.submit only.",
                        "تُنشأ طلبات الموافقة عبر خدمة التقديم فقط.")


class ApprovalRequest(models.Model):
    objects = ApprovalRequestQuerySet.as_manager()

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        RETURNED = "returned", "Returned"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"

    FINAL = {Status.APPROVED, Status.REJECTED, Status.CANCELLED, Status.EXPIRED, Status.RETURNED}

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="approval_requests")
    document_type = models.ForeignKey(ApprovableType, on_delete=models.PROTECT, related_name="requests")
    object_id = models.CharField(max_length=64)
    document_version = models.CharField(max_length=64)
    amount = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    summary_en = models.CharField(max_length=300)
    summary_ar = models.CharField(max_length=300)
    policy = models.ForeignKey(ApprovalPolicy, on_delete=models.PROTECT, related_name="requests")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    current_sequence = models.PositiveIntegerField(default=1)
    step_started_at = models.DateTimeField(default=timezone.now)
    submitted_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    document_created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    source = models.CharField(max_length=60, default="human")
    submitted_at = models.DateTimeField(default=timezone.now)
    decided_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-submitted_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=["pending", "approved", "rejected", "returned", "cancelled", "expired"]),
                name="request_status_valid",
            ),
            models.UniqueConstraint(
                fields=["document_type", "object_id"], condition=Q(status="pending"),
                name="one_pending_request_per_document",
            ),
        ]
        indexes = [models.Index(fields=["document_type", "object_id"])]

    @property
    def is_final(self):
        return self.status in self.FINAL

    def save(self, *args, **kwargs):
        if self.pk is not None:
            stored = ApprovalRequest.objects.filter(pk=self.pk).values_list("status", flat=True).first()
            if stored in self.FINAL:
                raise RuleError("request_final", "This request is already decided.", "تم البت في هذا الطلب مسبقاً.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuleError("request_no_delete", "Approval requests cannot be deleted.", "لا يمكن حذف طلبات الموافقة.")

    def __str__(self):
        return f"{self.document_type.code}#{self.object_id} ({self.status})"


class ApprovalAction(AppendOnlyModel):
    class Action(models.TextChoices):
        SUBMIT = "submit", "Submit"
        APPROVE = "approve", "Approve"
        REJECT = "reject", "Reject"
        RETURN = "return", "Return"
        CANCEL = "cancel", "Cancel"
        ESCALATE = "escalate", "Escalate"
        EXPIRE = "expire", "Expire"
        COMMENT = "comment", "Comment"

    class Channel(models.TextChoices):
        WEB = "web", "Web"
        API = "api", "API"
        MOBILE = "mobile", "Mobile"
        EMAIL_LINK = "email_link", "Email link"
        AGENT_GATEWAY = "agent_gateway", "Agent gateway"
        SYSTEM = "system", "System"

    request = models.ForeignKey(ApprovalRequest, on_delete=models.PROTECT, related_name="actions")
    step = models.ForeignKey(ApprovalStep, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    actor = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    on_behalf_of = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    action = models.CharField(max_length=10, choices=Action.choices)
    comment = models.TextField(blank=True)
    self_approval_override = models.BooleanField(default=False)
    escalated_to_role = models.ForeignKey(Role, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    escalated_to_user = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    at = models.DateTimeField(default=timezone.now)
    channel = models.CharField(max_length=20, choices=Channel.choices, default=Channel.SYSTEM)

    class Meta:
        ordering = ["at", "id"]

    @property
    def principal_id(self):
        return self.on_behalf_of_id or self.actor_id


class Delegation(models.Model):
    delegator = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="delegations_given")
    delegate = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="delegations_received")
    entity = models.ForeignKey(Entity, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                               help_text="Empty = all entities where the delegator approves")
    document_types = models.ManyToManyField(ApprovableType, blank=True, related_name="+", help_text="Empty = all")
    amount_max = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True, help_text="Inclusive")
    currency = models.CharField(max_length=3, default="SAR", help_text="Currency of amount_max")
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    reason = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-starts_at"]
        constraints = [
            models.CheckConstraint(condition=Q(ends_at__gt=models.F("starts_at")), name="delegation_dates_valid"),
            models.CheckConstraint(condition=~Q(delegator=models.F("delegate")), name="delegation_not_to_self"),
        ]

    def __str__(self):
        return f"{self.delegator} -> {self.delegate} {self.starts_at:%Y-%m-%d}..{self.ends_at:%Y-%m-%d}"


class Notification(models.Model):
    class Kind(models.TextChoices):
        APPROVAL_PENDING = "approval_pending", "Approval pending"
        APPROVAL_DECIDED = "approval_decided", "Approval decided"
        APPROVAL_RETURNED = "approval_returned", "Approval returned"
        ESCALATED = "escalated", "Escalated"
        DELEGATION_STARTED = "delegation_started", "Delegation started"
        ALERT = "alert", "Alert"

    user = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="notifications")
    entity = models.ForeignKey(Entity, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    alert_code = models.CharField(max_length=60, blank=True, help_text="e.g. no_eligible_approver, posting_failed")
    title_en = models.CharField(max_length=200)
    title_ar = models.CharField(max_length=200)
    body_en = models.TextField(blank=True)
    body_ar = models.TextField(blank=True)
    link = models.CharField(max_length=300)
    channels_sent = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def title(self, language):
        return self.title_ar if language == "ar" else self.title_en

    def body(self, language):
        return self.body_ar if language == "ar" else self.body_en


class PushDevice(models.Model):
    """Hook for the later mobile wave; registered but never used to send in this wave."""

    class Platform(models.TextChoices):
        IOS = "ios", "iOS"
        ANDROID = "android", "Android"

    user = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="push_devices")
    platform = models.CharField(max_length=10, choices=Platform.choices)
    token = models.CharField(max_length=500, unique=True)
    last_seen_at = models.DateTimeField(default=timezone.now)
    is_active = models.BooleanField(default=True)


class IdempotencyRecord(models.Model):
    """Stored response of an action call so a retried call with the same key returns the same result (T22)."""

    user = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    key = models.CharField(max_length=100)
    endpoint = models.CharField(max_length=200)
    fingerprint = models.CharField(max_length=64)
    status_code = models.PositiveIntegerField()
    body = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "key"], name="unique_idempotency_key_per_user")]
