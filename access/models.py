"""Users, roles, per-entity access and the append-only audit log.

Rules (spec docs/specs/approvals-and-web-ui.md):
- Access is per entity (D12): a role in entity A gives nothing in entity B.
- Roles are data; module scoping comes from module-prefixed permission codes (D13).
- Users are deactivated, never deleted, once they have audit rows (R3).
- Audit rows are append-only (R19).
"""
import uuid

from django.contrib.auth.models import AbstractUser, UserManager as BaseUserManager
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from core.models import Entity

from .errors import BilingualError


SYSTEM_USERNAME = "system"


def _refuse_system(message_en, message_ar):
    raise BilingualError("system_user", message_en, message_ar)


class UserQuerySet(models.QuerySet):
    """Bulk changes skip User.save/delete, so they are refused when the rows include the reserved
    ERP system user (follow-up 2a). bulk_update goes through update()."""

    def update(self, **kwargs):
        if self.filter(username=SYSTEM_USERNAME).exists():
            _refuse_system("The ERP system user cannot be changed.", "لا يمكن تعديل مستخدم النظام.")
        if kwargs.get("username") == SYSTEM_USERNAME:
            _refuse_system("The username 'system' is reserved.", "اسم المستخدم 'system' محجوز.")
        return super().update(**kwargs)

    update.alters_data = True

    def delete(self):
        if self.filter(username=SYSTEM_USERNAME).exists():
            _refuse_system("The ERP system user cannot be deleted.", "لا يمكن حذف مستخدم النظام.")
        return super().delete()

    delete.alters_data = True

    def bulk_update(self, objs, fields, batch_size=None):
        # Checked before Django opens its atomic block, so a refusal does not break the caller's transaction.
        objs = list(objs)
        if "username" in fields and any(o.username == SYSTEM_USERNAME for o in objs):
            _refuse_system("The username 'system' is reserved.", "اسم المستخدم 'system' محجوز.")
        if self.model._default_manager.filter(pk__in=[o.pk for o in objs], username=SYSTEM_USERNAME).exists():
            _refuse_system("The ERP system user cannot be changed.", "لا يمكن تعديل مستخدم النظام.")
        return super().bulk_update(objs, fields, batch_size=batch_size)

    bulk_update.alters_data = True


class UserManager(BaseUserManager.from_queryset(UserQuerySet)):
    pass


class User(AbstractUser):
    class Language(models.TextChoices):
        EN = "en", "English"
        AR = "ar", "العربية"

    class UserType(models.TextChoices):
        HUMAN = "human", "Human"
        SERVICE_AGENT = "service_agent", "Service agent (AI)"
        SERVICE_INTEGRATION = "service_integration", "Service integration"

    first_name = None
    last_name = None

    keycloak_id = models.UUIDField(
        null=True, blank=True, unique=True,
        help_text="Keycloak 'sub'. Empty until Keycloak SSO is connected (see Build notes).",
    )
    full_name_en = models.CharField(max_length=200)
    full_name_ar = models.CharField(max_length=200)
    email = models.EmailField(blank=True)
    language = models.CharField(max_length=2, choices=Language.choices, default=Language.EN)
    user_type = models.CharField(max_length=20, choices=UserType.choices, default=UserType.HUMAN)
    agent_name = models.SlugField(max_length=60, blank=True, help_text="Required for service agents")
    default_entity = models.ForeignKey(Entity, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    totp_enrolment_required = models.BooleanField(
        default=False, help_text="Set by a dual-controlled 2FA reset (D21): the user enrols a new device himself "
                                 "at next sign-in (password + one-time enrolment).")
    enrolment_code_required = models.BooleanField(
        default=False, help_text="D21 split knowledge: enrolment needs the initial password (known to the requester) "
                                 "and a one-time code (shown only to the approver who applied it).")
    enrolment_code_hash = models.CharField(max_length=128, blank=True, help_text="Hash only; never the code")
    enrolment_code_expires_at = models.DateTimeField(null=True, blank=True)
    password_set_by_others = models.BooleanField(
        default=True, help_text="The current password was set by someone other than the user (account creation, "
                                "admin, commands, D21 dual control). Cleared only when the user changes it himself. "
                                "While set, no 2FA reset or self-enrolment is allowed.")

    REQUIRED_FIELDS = ["email", "full_name_en", "full_name_ar"]

    objects = UserManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["agent_name"], condition=~models.Q(agent_name=""), name="unique_agent_name"
            ),
            models.CheckConstraint(
                condition=~models.Q(user_type="service_agent") | ~models.Q(agent_name=""),
                name="service_agent_has_agent_name",
            ),
        ]

    @property
    def is_human(self):
        return self.user_type == self.UserType.HUMAN

    @property
    def is_service_agent(self):
        return self.user_type == self.UserType.SERVICE_AGENT

    @property
    def source(self):
        """Value stored in `source` on records this user creates (D8)."""
        if self.user_type == self.UserType.SERVICE_AGENT:
            return f"agent:{self.agent_name}"
        if self.user_type == self.UserType.SERVICE_INTEGRATION:
            return "import"
        return "human"

    def set_password(self, raw_password):
        """Every password set through here counts as set by others (D21); the self-service paths clear the
        flag afterwards (access.services.password_changed_by_self)."""
        super().set_password(raw_password)
        self.password_set_by_others = True

    def check_password(self, raw_password):
        from django.contrib.auth.base_user import AbstractBaseUser
        from django.contrib.auth.hashers import check_password

        def setter(raw):
            AbstractBaseUser.set_password(self, raw)  # hash upgrade only: not a new password
            self._password = None
            self.save(update_fields=["password"])

        return check_password(raw_password, self.password, setter)

    def get_full_name(self):
        return self.full_name_ar if self.language == self.Language.AR else self.full_name_en

    def get_short_name(self):
        return self.username

    def clean(self):
        super().clean()
        if self.user_type == self.UserType.SERVICE_AGENT and not self.agent_name:
            raise ValidationError({"agent_name": "Service agents need an agent name."})

    def save(self, *args, **kwargs):
        # Follow-up 2a: the reserved ERP system user is never renamed or otherwise changed, and no
        # other account can take its name.
        if not self._state.adding and self.pk:
            stored = User.objects.filter(pk=self.pk).values_list("username", flat=True).first()
            if stored == SYSTEM_USERNAME and getattr(self, "_allow_system_save", False) is not True:
                raise BilingualError("system_user", "The ERP system user cannot be changed.",
                                     "لا يمكن تعديل مستخدم النظام.")
            if stored != SYSTEM_USERNAME and self.username == SYSTEM_USERNAME:
                raise BilingualError("system_user", "The username 'system' is reserved.",
                                     "اسم المستخدم 'system' محجوز.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.pk and User.objects.filter(pk=self.pk, username=SYSTEM_USERNAME).exists():
            _refuse_system("The ERP system user cannot be deleted.", "لا يمكن حذف مستخدم النظام.")
        about_me = models.Q(object_type="access.user", object_id=str(self.pk))
        if AuditLog.objects.filter(models.Q(actor=self) | models.Q(on_behalf_of=self) | about_me).exists():
            raise BilingualError(
                "user_has_audit",
                "Users with audit history cannot be deleted. Deactivate the user instead.",
                "لا يمكن حذف مستخدم له سجل تدقيق. قم بتعطيل المستخدم بدلاً من ذلك.",
            )
        return super().delete(*args, **kwargs)


class Permission(models.Model):
    """ERP permission, e.g. `ledger.entry.create`. The first segment is the module (D13)."""

    code = models.CharField(max_length=100, unique=True)
    name_en = models.CharField(max_length=200)
    name_ar = models.CharField(max_length=200)

    class Meta:
        ordering = ["code"]

    @property
    def module(self):
        return self.code.split(".", 1)[0]

    def __str__(self):
        return self.code


class Role(models.Model):
    code = models.SlugField(max_length=60, unique=True)
    name_en = models.CharField(max_length=100)
    name_ar = models.CharField(max_length=100)
    permissions = models.ManyToManyField(Permission, related_name="roles", blank=True)
    is_system = models.BooleanField(default=False)

    class Meta:
        ordering = ["code"]

    def delete(self, *args, **kwargs):
        if self.is_system:
            raise BilingualError("system_role", "System roles cannot be deleted.", "لا يمكن حذف أدوار النظام.")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return self.code


class EntityAccess(models.Model):
    """One row per user x entity x role. Access is never inherited across entities (D12)."""

    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="entity_access")
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="user_access")
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="grants")
    valid_from = models.DateField(default=timezone.localdate)
    valid_to = models.DateField(null=True, blank=True, help_text="Inclusive; empty = open-ended")
    granted_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
        help_text="Empty only for the bootstrap grant made by the setup command",
    )
    revoked_at = models.DateTimeField(null=True, blank=True, help_text="Set on revoke; rows are kept for history")

    class Meta:
        verbose_name_plural = "entity access"
        constraints = [models.UniqueConstraint(fields=["user", "entity", "role"], name="unique_user_entity_role")]

    def clean(self):
        if self.valid_to and self.valid_to < self.valid_from:
            raise ValidationError("Access ends before it starts.")

    def __str__(self):
        return f"{self.user} @ {self.entity.code} as {self.role.code}"


class AppendOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise BilingualError("append_only", "Audit records cannot be changed.", "لا يمكن تعديل سجلات التدقيق.")

    def delete(self):
        raise BilingualError("append_only", "Audit records cannot be deleted.", "لا يمكن حذف سجلات التدقيق.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise BilingualError("append_only", "Audit records cannot be changed.", "لا يمكن تعديل سجلات التدقيق.")


class AppendOnlyModel(models.Model):
    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise BilingualError("append_only", "Audit records cannot be changed.", "لا يمكن تعديل سجلات التدقيق.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise BilingualError("append_only", "Audit records cannot be deleted.", "لا يمكن حذف سجلات التدقيق.")


class AuditLog(AppendOnlyModel):
    class Channel(models.TextChoices):
        WEB = "web", "Web"
        API = "api", "API"
        MOBILE = "mobile", "Mobile"
        AGENT_GATEWAY = "agent_gateway", "Agent gateway"
        SYSTEM = "system", "System (scheduled job / command)"

    at = models.DateTimeField(default=timezone.now, db_index=True)
    actor = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    on_behalf_of = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    entity = models.ForeignKey(Entity, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    action = models.CharField(max_length=60, db_index=True)
    object_type = models.CharField(max_length=100)
    object_id = models.CharField(max_length=64)
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    source = models.CharField(max_length=60)
    ip = models.CharField(max_length=45, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    channel = models.CharField(max_length=20, choices=Channel.choices, blank=True)
    request_id = models.UUIDField(default=uuid.uuid4)

    class Meta:
        ordering = ["-at", "-id"]
        indexes = [models.Index(fields=["object_type", "object_id"])]

    def __str__(self):
        return f"{self.at:%Y-%m-%d %H:%M} {self.actor} {self.action} {self.object_type}#{self.object_id}"


class ServiceToken(models.Model):
    """Revocable API token for service users (agents, integrations). Humans sign in with password + TOTP."""

    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="service_tokens")
    key_hash = models.CharField(max_length=64, unique=True)
    label = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(help_text="Mandatory; tokens never live forever")
    revoked_at = models.DateTimeField(null=True, blank=True)

    @property
    def is_valid(self):
        return self.revoked_at is None and self.expires_at > timezone.now()

    def __str__(self):
        return f"token {self.label or self.pk} for {self.user}"


class ChairmanChangeQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise BilingualError("dual_control", "Dual-control changes move only through their service.",
                             "لا تتغير طلبات الرقابة المزدوجة إلا عبر خدمتها.")

    def delete(self):
        raise BilingualError("dual_control", "Dual-control changes cannot be deleted.",
                             "لا يمكن حذف طلبات الرقابة المزدوجة.")

    def bulk_update(self, objs, fields, batch_size=None):
        self.update()


class ChairmanChange(models.Model):
    """D21 dual control: a proposed change to the account of a user holding the Chairman role (or to a
    role a Chairman holds). Applies only after a CFO and a Managing Partner of the group root entity,
    two distinct people and neither of them an affected Chairman, have both approved. The requester's
    request counts as their approval. Created and moved only through access.dual_control."""

    class Action(models.TextChoices):
        PASSWORD = "password", "Set password"
        TOTP_RESET = "totp_reset", "Reset authenticator app (Chairman re-enrols himself)"
        DEACTIVATE = "deactivate", "Deactivate"
        UNLOCK = "unlock", "Clear sign-in lockout"
        UPDATE = "update", "Change account fields"
        GRANT = "grant", "Grant role"
        REVOKE = "revoke", "Revoke role"
        ROLE_PERMISSIONS = "role_permissions", "Change role permissions"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPLIED = "applied", "Applied"
        REJECTED = "rejected", "Rejected"
        EXPIRED = "expired", "Expired"

    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+",
                               help_text="The group root entity whose CFO and MP approve")
    target = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                               help_text="The Chairman whose account changes (empty for role edits)")
    role = models.ForeignKey(Role, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                             help_text="The role whose permissions change (role edits only)")
    action = models.CharField(max_length=20, choices=Action.choices)
    payload = models.JSONField(default=dict, blank=True, help_text="Proposed change; passwords only as a hash")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    requested_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    requested_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    reason = models.TextField(blank=True)

    objects = ChairmanChangeQuerySet.as_manager()

    class Meta:
        ordering = ["-requested_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(target__isnull=False) | models.Q(role__isnull=False),
                name="chairman_change_has_subject",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding and getattr(self, "_allow_save", False) is not True:
            raise BilingualError("dual_control", "Dual-control changes move only through their service.",
                                 "لا تتغير طلبات الرقابة المزدوجة إلا عبر خدمتها.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise BilingualError("dual_control", "Dual-control changes cannot be deleted.",
                             "لا يمكن حذف طلبات الرقابة المزدوجة.")

    def __str__(self):
        subject = self.target or self.role
        return f"{self.get_action_display()} for {subject} ({self.status})"


class ChairmanChangeApproval(AppendOnlyModel):
    """One approval of a ChairmanChange (append-only). The request itself is the requester's approval."""

    change = models.ForeignKey(ChairmanChange, on_delete=models.PROTECT, related_name="approvals")
    approver = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    roles = models.JSONField(default=list, help_text="Dual-control roles the approver held when approving")
    at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["change", "approver"], name="unique_chairman_change_approver")]
