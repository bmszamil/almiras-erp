"""Users, roles, per-entity access and the append-only audit log.

Rules (spec docs/specs/approvals-and-web-ui.md):
- Access is per entity (D12): a role in entity A gives nothing in entity B.
- Roles are data; module scoping comes from module-prefixed permission codes (D13).
- Users are deactivated, never deleted, once they have audit rows (R3).
- Audit rows are append-only (R19).
"""
import uuid

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from core.models import Entity

from .errors import BilingualError


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

    REQUIRED_FIELDS = ["email", "full_name_en", "full_name_ar"]

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

    def get_full_name(self):
        return self.full_name_ar if self.language == self.Language.AR else self.full_name_en

    def get_short_name(self):
        return self.username

    def clean(self):
        super().clean()
        if self.user_type == self.UserType.SERVICE_AGENT and not self.agent_name:
            raise ValidationError({"agent_name": "Service agents need an agent name."})

    def delete(self, *args, **kwargs):
        if AuditLog.objects.filter(models.Q(actor=self) | models.Q(on_behalf_of=self)).exists():
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
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    @property
    def is_valid(self):
        now = timezone.now()
        return self.revoked_at is None and (self.expires_at is None or self.expires_at > now)

    def __str__(self):
        return f"token {self.label or self.pk} for {self.user}"
