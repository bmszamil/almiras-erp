"""Access models (access.md §3): roles, profiles, memberships, dual control, access log.

Loaded by ``core.models`` so they belong to the ``core`` app.
"""
from datetime import timedelta

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone

from core.models import Entity

from . import catalogue


class Kind(models.TextChoices):
    HUMAN = "human", "Human"
    SYSTEM = "system", "System"
    AGENT = "agent", "Agent"


class SystemCode(models.TextChoices):
    BILLING = "billing", "Billing"
    ZATCA_QUEUE = "zatca_queue", "ZATCA queue"
    FX_IMPORT = "fx_import", "FX import"
    DEFERRED_SALE = "deferred_sale", "Bai' ajel sales"  # invoicing R112: the sale invoice and its profit recognition
    GOVERNANCE = "governance", "Governance report"  # access R84: the monthly governance report job


class AppendOnlyError(ValidationError):
    """Raised when an append-only access record would be edited or deleted."""


class Role(models.Model):
    class Scope(models.TextChoices):
        ENTITY = "entity", "Entity"
        GROUP = "group", "Group"

    code = models.SlugField(max_length=40, unique=True)
    name_en = models.CharField(max_length=120)
    name_ar = models.CharField(max_length=120)
    description_en = models.TextField(blank=True)
    description_ar = models.TextField(blank=True)
    scope = models.CharField(max_length=6, choices=Scope.choices)
    kind = models.CharField(max_length=6, choices=Kind.choices)
    system_code = models.CharField(
        max_length=20, choices=SystemCode.choices, null=True, blank=True,
        help_text="For system roles: the system user this role belongs to (R11, R13)",
    )
    is_seeded = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["code"]

    def clean(self):
        if self.kind == Kind.SYSTEM and not self.system_code:
            raise ValidationError("A system role needs a system code. / يجب تحديد رمز النظام لدور النظام.")
        if self.kind != Kind.SYSTEM and self.system_code:
            raise ValidationError("Only system roles have a system code. / رمز النظام لأدوار النظام فقط.")

    @property
    def permission_codes(self):
        return frozenset(self.permission_rows.values_list("codename", flat=True))

    def __str__(self):
        return f"{self.code} — {self.name_en} / {self.name_ar}"


class RolePermission(models.Model):
    """One catalogue permission held by a role (3.1 ``permissions``).

    Stored as a catalogue codename rather than an ``auth.Permission`` row, because
    the invoicing, billing and zatca apps that would own those rows do not exist yet.
    """

    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="permission_rows")
    codename = models.CharField(max_length=100)

    class Meta:
        unique_together = [("role", "codename")]
        ordering = ["role", "codename"]

    def clean(self):
        if self.codename not in catalogue.CATALOGUE:
            raise ValidationError(f"{self.codename} is not in the permission catalogue. / الصلاحية غير موجودة في الدليل.")
        reason = catalogue.role_may_hold(self.role.kind, self.role.scope, self.role.system_code, catalogue.get(self.codename))
        if reason:
            raise ValidationError(
                f"Role {self.role.code} may not hold {self.codename} ({reason}). / "
                f"لا يجوز أن يحمل الدور {self.role.code} الصلاحية {self.codename}.",
                code=reason,
            )

    def save(self, *args, **kwargs):
        self.full_clean()  # R12, R13: refused at save time
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.role.code}: {self.codename}"


class UserProfile(models.Model):
    class Language(models.TextChoices):
        EN = "en", "English"
        AR = "ar", "العربية"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="access_profile")
    kind = models.CharField(max_length=6, choices=Kind.choices, default=Kind.HUMAN)
    display_name_en = models.CharField(max_length=200, blank=True)
    display_name_ar = models.CharField(max_length=200, blank=True)
    preferred_language = models.CharField(max_length=2, choices=Language.choices, default=Language.EN)
    system_code = models.CharField(max_length=20, choices=SystemCode.choices, null=True, blank=True, unique=True)
    agent_name = models.SlugField(max_length=40, null=True, blank=True, unique=True)
    agent_model = models.CharField(max_length=100, blank=True)
    agent_version = models.CharField(max_length=100, blank=True)
    allow_external_ai = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
        help_text="Null for profiles created by migration, createsuperuser or the signal",
    )

    LOCKED_FIELDS = ("kind", "system_code", "agent_name", "user_id")

    def clean(self):
        if self.kind == Kind.SYSTEM and not self.system_code:
            raise ValidationError("A system user needs a system code. / يجب تحديد رمز النظام لمستخدم النظام.")
        if self.kind == Kind.AGENT and not self.agent_name:
            raise ValidationError("An agent user needs an agent name. / يجب تحديد اسم الوكيل.")
        if self.kind != Kind.SYSTEM and self.system_code:
            raise ValidationError("Only system users have a system code.")
        if self.kind != Kind.AGENT and self.agent_name:
            raise ValidationError("Only agent users have an agent name.")
        if self.kind != Kind.AGENT and self.allow_external_ai:
            raise ValidationError("Only agent users have the external AI setting.")

    def save(self, *args, **kwargs):
        if self.pk:
            stored = UserProfile.objects.filter(pk=self.pk).values(*self.LOCKED_FIELDS).first()
            if stored and any(stored[f] != getattr(self, f) for f in self.LOCKED_FIELDS):
                raise ValidationError(  # R10
                    "A user's kind, system code and agent name never change. / "
                    "لا يمكن تغيير نوع المستخدم أو رمز النظام أو اسم الوكيل."
                )
        self.clean()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError("User profiles are never deleted. / لا تُحذف ملفات المستخدمين.")

    @property
    def source(self):
        """Value for ``source`` on records this user creates (gateway rule 4, R21)."""
        if self.kind == Kind.SYSTEM:
            return f"system:{self.system_code}"
        if self.kind == Kind.AGENT:
            return f"agent:{self.agent_name}"
        return "human"

    def __str__(self):
        return f"{self.user} ({self.kind})"


def active_q(at, prefix=""):
    """Q for memberships active at ``at`` (3.3), excluding the delegation-source check."""
    p = prefix
    return (
        Q(**{f"{p}valid_from__lte": at})
        & (Q(**{f"{p}valid_to__isnull": True}) | Q(**{f"{p}valid_to__gt": at}))
        & (Q(**{f"{p}revoked_at__isnull": True}) | Q(**{f"{p}revoked_at__gt": at}))
        & Q(**{f"{p}role__is_active": True})
        & Q(**{f"{p}user__is_active": True})
    )


# Roles whose memberships must end, and their longest length in months (R60, R89; OQ-36 amended: 12 months).
TIME_LIMITED_ROLES = {"external_auditor": 12}


def add_months(value, months):
    """``value`` (date or datetime) plus whole calendar months, clamped to the month's last day."""
    import calendar

    month = value.month - 1 + months
    year, month = value.year + month // 12, month % 12 + 1
    return value.replace(year=year, month=month, day=min(value.day, calendar.monthrange(year, month)[1]))


# Roles of which at most one membership per entity is active at any time (R47: the group's chairman).
# Also enforced on PostgreSQL by the trigger of migration core.0012.
SINGLE_HOLDER_ROLES = frozenset({"chairman"})

# How far a revocation's revoked_at may be from the clock when it is written (also the PostgreSQL trigger).
REVOCATION_CLOCK_TOLERANCE = timedelta(seconds=10)


class AppendOnlyQuerySet(models.QuerySet):
    """Bulk ``update()`` / ``delete()`` would skip the model guards, so they are refused (R9, R33)."""

    append_only_message = "These records are append-only. / هذه السجلات للإضافة فقط."

    def update(self, **kwargs):
        raise AppendOnlyError(self.append_only_message)

    def delete(self):
        raise AppendOnlyError(self.append_only_message)


class MembershipQuerySet(AppendOnlyQuerySet):
    append_only_message = (
        "Memberships are never edited or deleted in bulk; revoke through core.access.revoke_membership. / "
        "لا تُعدَّل العضويات ولا تُحذف جماعيًا؛ استخدم خدمة إلغاء العضوية."
    )

    def active(self, at=None):
        at = at or timezone.now()
        # A delegation ends when its source ends (R16); delegates cannot delegate, so one level is enough.
        return self.filter(active_q(at)).filter(Q(delegated_from__isnull=True) | active_q(at, "delegated_from__"))


class Membership(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="memberships")
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="memberships")
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="memberships")
    valid_from = models.DateTimeField()
    valid_to = models.DateTimeField(null=True, blank=True)
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
        help_text="Null only for the bootstrap managing_partner membership (R26)",
    )
    granted_at = models.DateTimeField(default=timezone.now)
    reason = models.CharField(max_length=500)
    delegated_from = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="delegations"
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    revoke_reason = models.CharField(max_length=500, blank=True)

    objects = MembershipQuerySet.as_manager()

    REVOCATION_FIELDS = ("revoked_at", "revoked_by_id", "revoke_reason")
    FROZEN_FIELDS = (
        "user_id", "entity_id", "role_id", "valid_from", "valid_to", "granted_by_id", "granted_at",
        "reason", "delegated_from_id",
    )

    class Meta:
        ordering = ["entity", "user", "role", "valid_from"]
        constraints = [
            models.CheckConstraint(
                condition=Q(valid_to__isnull=True) | Q(valid_to__gt=models.F("valid_from")),
                name="membership_valid_to_after_from",
            ),
            models.CheckConstraint(
                condition=Q(delegated_from__isnull=True) | Q(valid_to__isnull=False),
                name="membership_delegation_needs_valid_to",
            ),
        ]

    @property
    def effective_end(self):
        ends = [d for d in (self.valid_to, self.revoked_at) if d is not None]
        return min(ends) if ends else None

    def is_active_at(self, at=None):
        return Membership.objects.active(at).filter(pk=self.pk).exists()

    def clean(self):
        errors = []
        if not self.reason:
            errors.append("A reason is required. / السبب مطلوب.")
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            errors.append("valid_to must be after valid_from. / يجب أن يكون تاريخ الانتهاء بعد تاريخ البدء.")
        if self.granted_by_id is not None and self.granted_by_id == self.user_id:
            errors.append("Nobody grants a membership to themselves. / لا يجوز منح العضوية للنفس.")  # R8
        if not (self.role_id and self.entity_id and self.user_id):
            if errors:
                raise ValidationError(errors)
            return
        role = self.role
        if role.scope == Role.Scope.GROUP and self.entity.consolidation != Entity.Consolidation.PARENT:
            errors.append(  # R6
                "A group role can only be granted on the group parent entity. / "
                "لا يُمنح دور المجموعة إلا على الكيان الأم."
            )
        profile = UserProfile.objects.filter(user_id=self.user_id).first()
        kind = profile.kind if profile else Kind.HUMAN
        if role.kind != kind:
            errors.append(  # R11
                f"A {role.kind} role cannot be given to a {kind} user. / لا يتوافق نوع الدور مع نوع المستخدم."
            )
        elif kind == Kind.SYSTEM and role.system_code != profile.system_code:
            errors.append("This system role belongs to another system user. / دور النظام هذا لمستخدم نظام آخر.")
        if role.code in TIME_LIMITED_ROLES:  # R60, R89 (OQ-36 amended): external auditors are time-limited
            months = TIME_LIMITED_ROLES[role.code]
            if self.valid_to is None:
                errors.append(ValidationError(
                    f"A {role.code} membership needs an end date (valid_to). / عضوية {role.name_ar} تحتاج تاريخ انتهاء.",
                    code="valid_to_required"))
            elif self.valid_to > add_months(self.valid_from, months):
                errors.append(ValidationError(
                    f"A {role.code} membership lasts at most {months} months. / "
                    f"لا تتجاوز عضوية {role.name_ar} {months} شهرًا.", code="valid_to_too_long"))
        if self.delegated_from_id and self.pk is None:  # R16 retired by R62 (access §10)
            errors.append(ValidationError(
                "Approval is delegated with an ApprovalDelegation (R62), not a delegated membership. / "
                "يُفوَّض الاعتماد بتفويض اعتماد، لا بعضوية مفوَّضة.", code="delegated_membership_retired"))
        if self.delegated_from_id:
            src = self.delegated_from
            if self.valid_to is None:
                errors.append("A delegation needs an end date. / يجب تحديد تاريخ انتهاء التفويض.")  # R16
            if src.delegated_from_id:
                errors.append("A delegate cannot delegate further. / لا يجوز للمفوَّض التفويض مرة أخرى.")
            if src.entity_id != self.entity_id or src.role.code != "approver" or role.code != "approver":
                errors.append("A delegation covers an approver membership in the same entity. / التفويض لعضوية معتمد في الكيان نفسه.")
            if src.valid_to is not None and self.valid_to is not None and self.valid_to > src.valid_to:
                errors.append("A delegation cannot outlast the delegator. / لا يجوز أن يتجاوز التفويض مدة عضوية المفوِّض.")
            if src.user_id == self.user_id:
                errors.append("Nobody delegates to themselves. / لا يجوز التفويض للنفس.")
        if errors:
            raise ValidationError(errors)

    def overlapping(self, *, any_user=False):
        """Other unrevoked memberships of the same (user, entity, role) whose dates overlap (R9).

        ``any_user=True``: of the same (entity, role) held by anyone (single-holder roles, R47).
        """
        qs = Membership.objects.filter(entity_id=self.entity_id, role_id=self.role_id).exclude(pk=self.pk)
        if not any_user:
            qs = qs.filter(user_id=self.user_id)
        qs = qs.filter(Q(revoked_at__isnull=True) | Q(revoked_at__gt=self.valid_from))
        qs = qs.filter(Q(valid_to__isnull=True) | Q(valid_to__gt=self.valid_from))
        if self.valid_to is not None:
            qs = qs.filter(valid_from__lt=self.valid_to)
        return qs

    def save(self, *args, **kwargs):
        if self.pk:
            stored = Membership.objects.filter(pk=self.pk).values(*self.FROZEN_FIELDS, *self.REVOCATION_FIELDS).first()
            if stored:
                if any(stored[f] != getattr(self, f) for f in self.FROZEN_FIELDS):
                    raise AppendOnlyError(  # R9
                        "Memberships are never edited; revoke and grant a new one. / "
                        "لا تُعدَّل العضويات؛ ألغِ العضوية وامنح عضوية جديدة."
                    )
                if stored["revoked_at"] is not None:
                    raise AppendOnlyError("A revoked membership is final. / إلغاء العضوية نهائي.")
                if self.revoked_at is None or self.revoked_by_id is None or not self.revoke_reason:
                    raise AppendOnlyError("A revocation needs a time, a person and a reason. / الإلغاء يتطلب وقتًا وشخصًا وسببًا.")
                if abs(self.revoked_at - timezone.now()) > REVOCATION_CLOCK_TOLERANCE:
                    raise AppendOnlyError(  # R9: a revocation is recorded when it happens, never back- or forward-dated
                        "A revocation takes effect now; it cannot be dated in the past or the future. / "
                        "يسري الإلغاء الآن؛ لا يجوز تأريخه في الماضي أو المستقبل."
                    )
        else:
            self.full_clean()
            if self.granted_by_id is None and not getattr(self, "_bootstrap", False):
                raise ValidationError("A membership needs the person who granted it. / يجب تحديد مانح العضوية.")
            if self.overlapping().exists():
                raise ValidationError(  # R9
                    "An overlapping membership for this user, entity and role already exists. / "
                    "توجد عضوية متداخلة لهذا المستخدم والكيان والدور."
                )
            if self.role.code in SINGLE_HOLDER_ROLES:
                with transaction.atomic():
                    # Two grants at the same moment run one after the other (the PostgreSQL trigger does the same).
                    Role.objects.select_for_update().filter(pk=self.role_id).exists()
                    if self.overlapping(any_user=True).exists():
                        raise ValidationError(  # R47
                            f"{self.entity.code} already has an active {self.role.code}; only one person holds this "
                            "role at a time. Revoke the current membership first. / "
                            f"للكيان {self.entity.code} {self.role.name_ar} نشط بالفعل؛ لا يشغل هذا الدور إلا شخص واحد "
                            "في الوقت نفسه. ألغِ العضوية الحالية أولًا.",
                            code="single_holder",
                        )
                    super().save(*args, **kwargs)
                return
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError("Memberships are never deleted. / لا تُحذف العضويات.")

    def __str__(self):
        return f"{self.user} · {self.entity.code} · {self.role.code}"


class DualControlRequestQuerySet(AppendOnlyQuerySet):
    append_only_message = (
        "Dual-control requests change only through core.access services. / "
        "لا تتغير طلبات الرقابة المزدوجة إلا من خلال خدمات الصلاحيات."
    )

    def _transition(self, **kwargs):
        """Conditional status change used only by core.access.services (R19)."""
        return models.QuerySet.update(self, **kwargs)


class DualControlRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        EXPIRED = "expired", "Expired"
        USED = "used", "Used"

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="dual_control_requests")
    permission = models.CharField(max_length=100)
    target_type = models.ForeignKey(ContentType, on_delete=models.PROTECT, related_name="+")
    target_id = models.PositiveBigIntegerField()
    payload_hash = models.CharField(max_length=64)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    requested_at = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.PENDING)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=500, blank=True)
    expires_at = models.DateTimeField()

    objects = DualControlRequestQuerySet.as_manager()

    class Meta:
        ordering = ["-requested_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(decided_by__isnull=True) | ~Q(decided_by=models.F("requested_by")),
                name="dual_control_decider_not_requester",
            )
        ]

    def save(self, *args, **kwargs):
        if self.pk is not None and DualControlRequest.objects.filter(pk=self.pk).exists():
            raise AppendOnlyError(  # R19: status changes go through request/decide/require_dual_control
                "Dual-control requests change only through core.access services. / "
                "لا تتغير طلبات الرقابة المزدوجة إلا من خلال خدمات الصلاحيات."
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError("Dual-control requests are never deleted. / لا تُحذف طلبات الرقابة المزدوجة.")

    def __str__(self):
        return f"{self.entity.code} {self.permission} #{self.target_id} ({self.status})"


class AccessLogQuerySet(AppendOnlyQuerySet):
    append_only_message = "The access log is append-only. / سجل الوصول للإضافة فقط."


class AccessLog(models.Model):
    class Event(models.TextChoices):
        MEMBERSHIP_GRANTED = "membership_granted", "Membership granted"
        MEMBERSHIP_REVOKED = "membership_revoked", "Membership revoked"
        DELEGATION_GRANTED = "delegation_granted", "Delegation granted"
        DELEGATION_REVOKED = "delegation_revoked", "Delegation revoked"
        ROLE_CHANGED = "role_changed", "Role changed"
        PROFILE_CHANGED = "profile_changed", "Profile changed"
        DUAL_CONTROL_REQUESTED = "dual_control_requested", "Dual control requested"
        DUAL_CONTROL_DECIDED = "dual_control_decided", "Dual control decided"
        DUAL_CONTROL_USED = "dual_control_used", "Dual control used"
        PERMISSION_GRANTED = "permission_granted", "Approval or posting right used"
        PERMISSION_DENIED = "permission_denied", "Permission denied"
        SUPERUSER_FLAG_CHANGED = "superuser_flag_changed", "Superuser flag changed"
        STAFF_FLAG_CHANGED = "staff_flag_changed", "Staff flag changed"
        AGENT_TOKEN_ISSUED = "agent_token_issued", "Agent token issued"
        AGENT_TOKEN_REVOKED = "agent_token_revoked", "Agent token revoked"
        BOOTSTRAP = "bootstrap", "Bootstrap"
        ENTITY_SETTINGS_CHANGED = "entity_settings_changed", "Entity settings changed"
        USER_ACTIVE_CHANGED = "user_active_changed", "User activated or deactivated"
        PERSONAL_DATA_VIEWED = "personal_data_viewed", "Personal data viewed"  # hr R10, access R29
        PERSONAL_DATA_EXPORTED = "personal_data_exported", "Personal data exported"  # hr R12
        # access §10 (R78, R83 to R87): authority matrix, approvals, self-approval, governance report.
        MATRIX_DRAFTED = "authority_matrix_drafted", "Authority matrix version drafted"
        MATRIX_ROW_ADDED = "authority_matrix_row_added", "Authority matrix row added"
        MATRIX_ROW_REMOVED = "authority_matrix_row_removed", "Authority matrix row removed"
        MATRIX_SUBMITTED = "authority_matrix_submitted", "Authority matrix version submitted"
        MATRIX_APPROVED = "authority_matrix_approved", "Authority matrix version approved"
        MATRIX_REJECTED = "authority_matrix_rejected", "Authority matrix version rejected"
        MATRIX_WITHDRAWN = "authority_matrix_withdrawn", "Authority matrix version withdrawn"
        MATRIX_EFFECTIVE = "authority_matrix_effective", "Authority matrix version took effect"
        GOVERNANCE_CHANGE_REQUESTED = "governance_change_requested", "Top authority change requested"
        APPROVAL_SUBMITTED = "approval_submitted", "Approval request opened"
        APPROVAL_DECIDED = "approval_decided", "Approval step decided"
        SELF_APPROVAL = "self_approval", "Top-authority self-approval"
        ASSISTANT_ASSIGNED = "assistant_assigned", "Personal assistant assigned"
        ASSISTANT_REVOKED = "assistant_revoked", "Personal assistant assignment ended"
        REPORT_GENERATED = "governance_report_generated", "Governance report generated"
        REPORT_VIEWED = "governance_report_viewed", "Governance report viewed"
        REPORT_EXPORTED = "governance_report_exported", "Governance report exported"
        REPORT_REVIEWED = "governance_report_reviewed", "Governance report reviewed"
        ALERT_SENT = "access_alert_sent", "Access alert sent"
        # payroll R49, migration R26: an employee's written salary-deduction consent recorded for a migrated contract
        DEDUCTION_CONSENT_RECORDED = "deduction_consent_recorded", "Salary-deduction consent recorded"
        LOGIN_SUCCEEDED = "login_succeeded", "Signed in to the web UI"  # web.md 3.4, R26
        LOGOUT = "logout", "Signed out of the web UI"  # web.md 3.4, R26
        # web.md 3.4, R16, R26 (slice 2): MFA events. Never the code, the secret or a recovery code.
        MFA_ENROLLED = "mfa_enrolled", "MFA device enrolled"
        MFA_VERIFIED = "mfa_verified", "MFA code verified"
        MFA_VERIFY_FAILED = "mfa_verify_failed", "MFA code refused"
        MFA_RECOVERY_USED = "mfa_recovery_code_used", "MFA recovery code used"
        MFA_RECOVERY_RENEWED = "mfa_recovery_codes_renewed", "MFA recovery codes renewed"
        MFA_RESET = "mfa_reset", "MFA device reset by another person"
        MFA_REKEYED = "mfa_rekeyed", "MFA secrets re-encrypted under the current key"

    at = models.DateTimeField(default=timezone.now)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    actor_kind = models.CharField(max_length=6, choices=Kind.choices, default=Kind.HUMAN)
    event = models.CharField(max_length=30, choices=Event.choices)
    entity = models.ForeignKey(Entity, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    subject_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    membership = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    role = models.ForeignKey(Role, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    permission = models.CharField(max_length=100, blank=True)
    details = models.JSONField(default=dict)
    request_id = models.CharField(max_length=64, blank=True)
    ip = models.CharField(max_length=45, blank=True)
    source = models.CharField(max_length=60, blank=True)

    objects = AccessLogQuerySet.as_manager()

    class Meta:
        ordering = ["at", "id"]

    def save(self, *args, **kwargs):
        if self.pk is not None or kwargs.get("force_update"):
            raise AppendOnlyError("The access log is append-only. / سجل الوصول للإضافة فقط.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError("The access log is append-only. / سجل الوصول للإضافة فقط.")

    def __str__(self):
        return f"{self.at:%Y-%m-%d %H:%M} {self.event} {self.permission}"


class GrantUse(models.Model):
    """The single use of a grant (R30): one row per ``permission_granted`` AccessLog row.

    The one-to-one key makes a second use fail in the database, also when two
    requests present the same grant at the same moment. Append-only.
    """

    log = models.OneToOneField(AccessLog, on_delete=models.PROTECT, related_name="grant_use")
    used_at = models.DateTimeField(default=timezone.now)
    used_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    target_type = models.CharField(max_length=100, blank=True, help_text="app_label.model the grant was used on")
    target_id = models.PositiveBigIntegerField(null=True, blank=True)

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        ordering = ["used_at", "id"]

    def save(self, *args, **kwargs):
        if self.pk is not None or kwargs.get("force_update"):
            raise AppendOnlyError("A grant use is final. / استخدام التفويض نهائي.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError("A grant use is final. / استخدام التفويض نهائي.")

    def __str__(self):
        return f"grant {self.log_id} used {self.used_at:%Y-%m-%d %H:%M}"


# access §10: authority matrix, approval engine, delegation, assistants and the governance report (same app).
from .authority_models import (  # noqa: E402,F401
    AccessAlert, ApprovalActivity, ApprovalDecision, ApprovalDelegation, ApprovalRequest, AssistantAssignment,
    AuthorityMatrixVersion, AuthorityRule, EntityGovernance, GovernanceReport,
)
