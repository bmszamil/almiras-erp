"""Access checks, entity scoping and the audit log.

R1  scope(queryset, user, perm) filters every view/API queryset by entity access.
R2  has_perm(user, entity, perm): union of the user's roles in that entity only (D12).
R3  deactivate_user instead of delete.
R18 service agents never hold act/post/manage permissions, whatever their roles.
R19 audit() writes append-only AuditLog rows with before/after.
"""
import datetime
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.models import Entity

from .catalogue import AGENT_FORBIDDEN
from .errors import AccessDenied, RuleError
from .models import AuditLog, EntityAccess, Role, User


@dataclass
class Context:
    """Where an action came from. Passed from views/API/jobs down to services."""

    channel: str = AuditLog.Channel.SYSTEM
    ip: str = ""
    user_agent: str = ""
    request_id: uuid.UUID = field(default_factory=uuid.uuid4)

    @classmethod
    def from_request(cls, request, channel=AuditLog.Channel.API):
        meta = getattr(request, "META", {})
        return cls(
            channel=channel,
            ip=meta.get("REMOTE_ADDR", "") or "",
            user_agent=(meta.get("HTTP_USER_AGENT", "") or "")[:300],
        )


SYSTEM_USERNAME = "system"


def system_user():
    """Technical actor for scheduled jobs (escalation, expiry). Cannot sign in."""
    user, created = User.objects.get_or_create(
        username=SYSTEM_USERNAME,
        defaults={
            "full_name_en": "ERP system",
            "full_name_ar": "نظام تخطيط الموارد",
            "user_type": User.UserType.SERVICE_INTEGRATION,
        },
    )
    if created:
        user.set_unusable_password()
        user.save(update_fields=["password"])
    return user


# ---------------------------------------------------------------- access checks


def active_grants(user, entity=None, on=None):
    on = on or timezone.localdate()
    qs = EntityAccess.objects.filter(
        user=user, user__is_active=True, entity__is_active=True, revoked_at__isnull=True, valid_from__lte=on
    ).filter(Q(valid_to__isnull=True) | Q(valid_to__gte=on))
    if entity is not None:
        qs = qs.filter(entity=entity)
    return qs


def active_grants_for_entity(entity, on=None):
    on = on or timezone.localdate()
    return EntityAccess.objects.filter(
        entity=entity, user__is_active=True, revoked_at__isnull=True, valid_from__lte=on
    ).filter(Q(valid_to__isnull=True) | Q(valid_to__gte=on))


def permissions_for(user, entity):
    if user is None or not user.is_active:
        return set()
    codes = set(
        active_grants(user, entity).values_list("role__permissions__code", flat=True)
    ) - {None}
    if user.is_service_agent:
        codes -= AGENT_FORBIDDEN
    return codes


def has_perm(user, entity, perm):
    return perm in permissions_for(user, entity)


def require_perm(user, entity, perm):
    if not has_perm(user, entity, perm):
        raise AccessDenied(
            "permission_denied",
            f"You do not have permission '{perm}' in {entity.code}.",
            f"ليس لديك صلاحية '{perm}' في {entity.code}.",
        )


def has_role(user, entity, role_code):
    return active_grants(user, entity).filter(role__code=role_code).exists()


def entities_with_perm(user, perm):
    if user is None or not user.is_authenticated or not user.is_active:
        return Entity.objects.none()
    if user.is_service_agent and perm in AGENT_FORBIDDEN:
        return Entity.objects.none()
    ids = active_grants(user).filter(role__permissions__code=perm).values_list("entity_id", flat=True)
    return Entity.objects.filter(pk__in=ids)


def scope(queryset, user, perm, entity_field="entity"):
    """R1: limit a queryset to entities where the user holds `perm`."""
    return queryset.filter(**{f"{entity_field}__in": entities_with_perm(user, perm)})


def users_with_perm(entity, perm, role=None):
    """Active users holding `perm` in `entity` (optionally through a given role)."""
    on = timezone.localdate()
    grants = EntityAccess.objects.filter(
        entity=entity, revoked_at__isnull=True, valid_from__lte=on, user__is_active=True
    ).filter(Q(valid_to__isnull=True) | Q(valid_to__gte=on))
    if role is not None:
        grants = grants.filter(role=role)
    candidates = User.objects.filter(pk__in=grants.values("user_id")).distinct()
    return [u for u in candidates if has_perm(u, entity, perm)]


def is_root_entity(entity):
    return entity.parent_id is None


# ---------------------------------------------------------------- audit


def to_json(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {k: to_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_json(v) for v in value]
    return value


def snapshot(obj, fields=None):
    """Field-level snapshot of a model instance for before/after (D7)."""
    if obj is None:
        return None
    data = {}
    for f in obj._meta.concrete_fields:
        if fields is not None and f.name not in fields:
            continue
        if f.name == "password":
            continue
        data[f.attname] = to_json(getattr(obj, f.attname))
    return data


def audit(actor, action, obj=None, *, entity=None, before=None, after=None, on_behalf_of=None,
          ctx=None, object_type=None, object_id=None, source=None):
    ctx = ctx or Context()
    if obj is not None:
        object_type = object_type or obj._meta.label_lower
        object_id = object_id if object_id is not None else str(obj.pk)
        if entity is None and hasattr(obj, "entity_id"):
            entity = obj.entity
    return AuditLog.objects.create(
        actor=actor,
        on_behalf_of=on_behalf_of,
        entity=entity,
        action=action,
        object_type=object_type or "",
        object_id=str(object_id or ""),
        before=to_json(before),
        after=to_json(after),
        source=source or ("system" if actor.username == SYSTEM_USERNAME else actor.source),
        ip=ctx.ip,
        user_agent=ctx.user_agent,
        channel=ctx.channel,
        request_id=ctx.request_id,
    )


def visible_audit(user):
    """Audit rows the user may read: entities where they hold access.audit.view.

    Global rows (entity empty, e.g. role edits) only for holders of the permission in a
    root (parent) entity — conservative build choice, see Build notes.
    """
    entities = entities_with_perm(user, "access.audit.view")
    q = Q(entity__in=entities)
    if entities.filter(parent__isnull=True).exists():
        q |= Q(entity__isnull=True)
    return AuditLog.objects.filter(q)


# ---------------------------------------------------------------- user and access management


@transaction.atomic
def grant_access(granter, user, entity, role, valid_from=None, valid_to=None, ctx=None):
    if granter is not None:
        require_perm(granter, entity, "access.entity_access.manage")
    if isinstance(role, str):
        role = Role.objects.get(code=role)
    if user.is_service_agent and role.permissions.filter(code__in=AGENT_FORBIDDEN).exists():
        # The permissions would be stripped anyway (R18); refuse to make intent explicit.
        raise RuleError(
            "agent_role_not_allowed",
            f"Service agents cannot hold role {role.code}.",
            f"لا يمكن لوكلاء الخدمة حمل الدور {role.code}.",
        )
    grant, created = EntityAccess.objects.get_or_create(
        user=user, entity=entity, role=role,
        defaults={"valid_from": valid_from or timezone.localdate(), "valid_to": valid_to, "granted_by": granter},
    )
    before = None
    if not created:
        before = snapshot(grant)
        grant.valid_from = valid_from or grant.valid_from
        grant.valid_to = valid_to
        grant.revoked_at = None
        grant.granted_by = granter
        grant.save()
    audit(granter or system_user(), "access.grant", grant, entity=entity, before=before, after=snapshot(grant), ctx=ctx)
    return grant


@transaction.atomic
def revoke_access(revoker, grant, ctx=None):
    require_perm(revoker, grant.entity, "access.entity_access.manage")
    before = snapshot(grant)
    grant.revoked_at = timezone.now()
    grant.save(update_fields=["revoked_at"])
    audit(revoker, "access.revoke", grant, entity=grant.entity, before=before, after=snapshot(grant), ctx=ctx)
    return grant


def _can_manage_users(actor):
    return entities_with_perm(actor, "access.user.manage").exists()


@transaction.atomic
def create_user(actor, *, username, full_name_en, full_name_ar, email="", language="en",
                user_type=User.UserType.HUMAN, agent_name="", password=None, ctx=None):
    if actor is not None and not _can_manage_users(actor):
        raise AccessDenied("permission_denied", "You cannot manage users.", "لا يمكنك إدارة المستخدمين.")
    user = User(
        username=username, full_name_en=full_name_en, full_name_ar=full_name_ar, email=email,
        language=language, user_type=user_type, agent_name=agent_name,
    )
    user.full_clean(exclude=["password"])
    if password and user_type == User.UserType.HUMAN:
        user.set_password(password)
    else:
        user.set_unusable_password()
    user.save()
    audit(actor or system_user(), "access.user.create", user, after=snapshot(user), ctx=ctx)
    return user


@transaction.atomic
def deactivate_user(actor, user, ctx=None):
    """R3: users are deactivated, never deleted."""
    if not _can_manage_users(actor):
        raise AccessDenied("permission_denied", "You cannot manage users.", "لا يمكنك إدارة المستخدمين.")
    before = snapshot(user, ["is_active"])
    user.is_active = False
    user.save(update_fields=["is_active"])
    user.service_tokens.filter(revoked_at__isnull=True).update(revoked_at=timezone.now())
    audit(actor, "access.user.deactivate", user, before=before, after=snapshot(user, ["is_active"]), ctx=ctx)
    return user


def update_profile(user, *, language=None, default_entity=None, ctx=None):
    """PATCH /me: language and default entity."""
    before = snapshot(user, ["language", "default_entity"])
    if language is not None:
        if language not in User.Language.values:
            raise RuleError("invalid_language", "Language must be en or ar.", "يجب أن تكون اللغة en أو ar.")
        user.language = language
    if default_entity is not None:
        require_perm(user, default_entity, "core.entity.view")
        user.default_entity = default_entity
    user.save(update_fields=["language", "default_entity"])
    audit(user, "access.user.update_profile", user, before=before,
          after=snapshot(user, ["language", "default_entity"]), ctx=ctx)
    return user
