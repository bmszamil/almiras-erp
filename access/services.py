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

from .catalogue import SERVICE_FORBIDDEN
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
            ip=client_ip(request),
            user_agent=(meta.get("HTTP_USER_AGENT", "") or "")[:300],
        )


def client_ip(request):
    """Client address. X-Forwarded-For is trusted only when the direct peer is a configured proxy
    (settings.ERP_TRUSTED_PROXIES); then the right-most address that is not a trusted proxy wins."""
    from django.conf import settings

    meta = getattr(request, "META", {}) or {}
    remote = (meta.get("REMOTE_ADDR") or "").strip()
    trusted = set(getattr(settings, "ERP_TRUSTED_PROXIES", []) or [])
    forwarded = meta.get("HTTP_X_FORWARDED_FOR", "")
    if not forwarded or remote not in trusted:
        return remote[:45]
    for hop in reversed([h.strip() for h in forwarded.split(",") if h.strip()]):
        if hop not in trusted:
            return hop[:45]
    return remote[:45]


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
        user._allow_system_save = True
        user.save(update_fields=["password"])
        user._allow_system_save = False
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
    if not user.is_human:
        codes -= SERVICE_FORBIDDEN  # R18, D17: service users are drafts-only
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
    if not user.is_human and perm in SERVICE_FORBIDDEN:
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


def can_view_security(user, row):
    """IP address and user agent are shown only with access.audit.view_security for the row's entity
    (global rows: in a root entity)."""
    entities = entities_with_perm(user, "access.audit.view_security")
    if row.entity_id is None:
        return entities.filter(parent__isnull=True).exists()
    return entities.filter(pk=row.entity_id).exists()


# ---------------------------------------------------------------- user and access management


@transaction.atomic
def grant_access(granter, user, entity, role, valid_from=None, valid_to=None, ctx=None, setup=False):
    """Grant a role. granter=None is the operator bootstrap (`grant_access` command).
    D19: roles of a Managing Partner change only through a Chairman (or setup mode)."""
    if is_system_user(user):
        raise RuleError("system_user", "The ERP system user cannot be given roles.",
                        "لا يمكن منح أدوار لمستخدم النظام.")
    if isinstance(role, str):
        role = Role.objects.get(code=role)
    if not user.is_human and role.permissions.filter(code__in=SERVICE_FORBIDDEN).exists():
        # The permissions would be stripped anyway (R18); refuse to make intent explicit.
        raise RuleError(
            "agent_role_not_allowed",
            f"Service agents cannot hold role {role.code}.",
            f"لا يمكن لوكلاء الخدمة حمل الدور {role.code}.",
        )
    if granter is not None:
        _require_grant_rights(granter, user, entity, role, setup)
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
def revoke_access(revoker, grant, ctx=None, setup=False):
    if revoker is None:
        raise AccessDenied("permission_denied", "A revoke needs an acting user.", "يتطلب السحب مستخدماً منفذاً.")
    _require_grant_rights(revoker, grant.user, grant.entity, grant.role, setup)
    before = snapshot(grant)
    grant.revoked_at = timezone.now()
    grant.save(update_fields=["revoked_at"])
    audit(revoker, "access.revoke", grant, entity=grant.entity, before=before, after=snapshot(grant), ctx=ctx)
    return grant


def _require_grant_rights(actor, user, entity, role, setup):
    """Who may grant or revoke `role` for `user` in `entity` (an acting user; operator commands pass none).

    - Nobody grants or revokes their own roles, in any mode (closes self-promotion to Chairman).
    - D19: only a holder of the Chairman role in that entity grants or revokes the Chairman role, so
      nobody can create a Chairman to get around the Managing Partner protection. Setup mode excepted.
    - D19: roles of a Managing Partner change only through a Chairman in all of their MP entities.
    - Otherwise access.entity_access.manage in the entity.
    """
    if actor.pk == user.pk:
        raise AccessDenied("self_grant", "You cannot grant or revoke your own roles.",
                           "لا يمكنك منح أدوارك أو سحبها بنفسك.")
    if role.code == CHAIRMAN_ROLE_CODE and not setup:
        if not (actor.is_active and actor.is_human and has_role(actor, entity, CHAIRMAN_ROLE_CODE)):
            raise AccessDenied(
                "chairman_role_protected",
                f"Only a Chairman of {entity.code} can grant or revoke the Chairman role (D19).",
                f"لا يحق إلا لرئيس مجلس الإدارة في {entity.code} منح دور رئيس مجلس الإدارة أو سحبه (D19).",
            )
    if is_protected_mp(user):
        require_manage_user(actor, user, setup=setup)  # D19
    elif role.code != CHAIRMAN_ROLE_CODE or setup:
        require_perm(actor, entity, "access.entity_access.manage")


def _can_manage_users(actor):
    return entities_with_perm(actor, "access.user.manage").exists()


MP_ROLE_CODE = "managing_partner"
CHAIRMAN_ROLE_CODE = "chairman"


def is_system_user(user):
    """The reserved ERP system user (created by migration). It can never be managed (follow-up 2a)."""
    return user is not None and user.username == SYSTEM_USERNAME


def mp_entities(user):
    """Entities where `user` holds the Managing Partner role (any non-revoked grant, even future or expired)."""
    return set(EntityAccess.objects.filter(user=user, role__code=MP_ROLE_CODE, revoked_at__isnull=True)
               .values_list("entity_id", flat=True))


def is_protected_mp(user):
    return bool(mp_entities(user))


def can_manage_user(actor, target, setup=False):
    """Who may change `target`'s privileges, active status, password, roles, tokens, TOTP or lockout.

    - Nobody manages themselves; the ERP system user is never managed (2a).
    - D19: a Managing Partner is managed only by a user holding the Chairman role (an active grant)
      in every entity where the target is MP. Other MPs and user managers cannot. Setup mode excepted.
    - Everyone else (review B): access.user.manage in every entity where the target holds a grant.
    - Superusers are managed only in setup mode.
    """
    if actor is None or not actor.is_active or not actor.is_human or actor.pk == target.pk:
        return False
    if is_system_user(target):
        return False
    if setup:
        return True
    if target.is_superuser:
        return False
    protected = mp_entities(target)
    if protected:
        chair = set(active_grants(actor).filter(role__code=CHAIRMAN_ROLE_CODE).values_list("entity_id", flat=True))
        return protected <= chair
    managed = set(entities_with_perm(actor, "access.user.manage").values_list("pk", flat=True))
    if not managed:
        return False
    target_entities = set(
        EntityAccess.objects.filter(user=target, revoked_at__isnull=True).values_list("entity_id", flat=True)
    )
    return target_entities <= managed


def mp_holders(role):
    """Managing Partners holding `role` (any non-revoked grant)."""
    holders = User.objects.filter(entity_access__role=role, entity_access__revoked_at__isnull=True).distinct()
    return [u for u in holders if is_protected_mp(u)]


def can_edit_role(actor, role, setup=False):
    """D19 for role edits: changing a role held by a Managing Partner changes that MP's privileges, so
    the editor must be able to manage every MP holding it (Chairman in each of their MP entities,
    see can_manage_user). The managing_partner role itself always needs a Chairman. The normal
    role-edit right is checked separately (admin). Setup mode excepted."""
    if setup:
        return True
    holders = mp_holders(role)
    if role.code != MP_ROLE_CODE and not holders:
        return True
    if actor is None or not active_grants(actor).filter(role__code=CHAIRMAN_ROLE_CODE).exists():
        return False
    return all(can_manage_user(actor, mp) for mp in holders)


def require_manage_user(actor, target, setup=False):
    if not can_manage_user(actor, target, setup=setup):
        if is_protected_mp(target) and actor is not None and actor.pk != target.pk:
            raise AccessDenied(
                "mp_protected",
                "Only the Chairman can change, lock or disable a Managing Partner (D19).",
                "لا يحق إلا لرئيس مجلس الإدارة تعديل الشريك المدير أو قفله أو تعطيله (D19).",
            )
        raise AccessDenied(
            "cannot_manage_user",
            "You cannot change this user: you need user-management rights in every entity they belong to, "
            "and you cannot change your own privileges.",
            "لا يمكنك تعديل هذا المستخدم: تحتاج صلاحية إدارة المستخدمين في كل منشآته، ولا يمكنك تعديل صلاحياتك.",
        )


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
def deactivate_user(actor, user, ctx=None, setup=False):
    """R3: users are deactivated, never deleted. D19 applies to Managing Partners."""
    if actor is None or is_system_user(user):
        raise AccessDenied("cannot_manage_user", "This user cannot be deactivated here.", "لا يمكن تعطيل هذا المستخدم هنا.")
    require_manage_user(actor, user, setup=setup)
    before = snapshot(user, ["is_active"])
    user.is_active = False
    user.save(update_fields=["is_active"])
    for token in user.service_tokens.filter(revoked_at__isnull=True):
        revoke_service_token(actor, token, ctx=ctx, setup=setup)
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


# ---------------------------------------------------------------- service tokens and TOTP (audited)


def _token_hash(key):
    import hashlib

    return hashlib.sha256(key.encode()).hexdigest()


@transaction.atomic
def issue_service_token(actor, user, *, expires_at, label="", ctx=None, setup=False):
    """Return the plain token once; only its hash is stored. Expiry is mandatory."""
    import secrets

    from .models import ServiceToken

    if is_system_user(user):
        raise RuleError("system_user", "Tokens cannot be issued for the ERP system user.",
                        "لا يمكن إصدار رموز لمستخدم النظام.")
    if actor is not None:
        require_manage_user(actor, user, setup=setup)
    if user.is_human:
        raise RuleError("token_for_human", "Tokens are for service users only.", "الرموز لمستخدمي الخدمة فقط.")
    if not user.is_active:
        raise RuleError("inactive", "The user is inactive.", "المستخدم غير نشط.")
    if expires_at is None or expires_at <= timezone.now():
        raise RuleError("token_expiry_required", "A service token needs a future expiry date.",
                        "يحتاج رمز الخدمة إلى تاريخ انتهاء مستقبلي.")
    key = secrets.token_urlsafe(32)
    token = ServiceToken.objects.create(user=user, key_hash=_token_hash(key), label=label, expires_at=expires_at)
    audit(actor or system_user(), "access.token.issue", token, object_type="access.servicetoken",
          after={"user": user.username, "label": label, "expires_at": expires_at}, ctx=ctx)
    return key


@transaction.atomic
def revoke_service_token(actor, token, ctx=None, setup=False):
    if actor is not None and actor.pk != token.user_id:
        require_manage_user(actor, token.user, setup=setup)
    if token.revoked_at is None:
        token.revoked_at = timezone.now()
        token.save(update_fields=["revoked_at"])
        audit(actor or system_user(), "access.token.revoke", token, object_type="access.servicetoken",
              after={"user": token.user.username, "label": token.label, "revoked_at": token.revoked_at}, ctx=ctx)
    return token


@transaction.atomic
def enroll_totp(actor, user, name="Authenticator app", ctx=None, setup=False):
    """Create a confirmed TOTP device (D14). Replaces a device of the same name. Returns the device."""
    from django_otp.plugins.otp_totp.models import TOTPDevice

    if is_system_user(user):
        raise RuleError("system_user", "The ERP system user cannot sign in.", "لا يمكن لمستخدم النظام تسجيل الدخول.")
    if actor is not None and actor.pk != user.pk:
        require_manage_user(actor, user, setup=setup)
    if not user.is_human or not user.is_active:
        raise RuleError("totp_for_human", "Authenticator apps are for active human users.",
                        "تطبيقات المصادقة للمستخدمين البشريين النشطين فقط.")
    replaced = list(TOTPDevice.objects.filter(user=user, name=name).values_list("pk", flat=True))
    TOTPDevice.objects.filter(pk__in=replaced).delete()
    device = TOTPDevice.objects.create(user=user, name=name, confirmed=True)
    audit(actor or system_user(), "access.totp.enroll", user,
          after={"device_id": device.pk, "name": name, "replaced_devices": replaced}, ctx=ctx)
    return device


# ---------------------------------------------------------------- quarterly access review


def access_review_report(viewer, on=None, ctx=None):
    """Who holds which role in which entity, with last login and 2FA status, for the quarterly review.

    Covers entities where the viewer holds access.audit.view or access.entity_access.manage.
    Generating the report is itself audited.
    """
    from django_otp import user_has_device

    on = on or timezone.localdate()
    entities = entities_with_perm(viewer, "access.audit.view") | entities_with_perm(viewer, "access.entity_access.manage")
    grants = (
        EntityAccess.objects.filter(entity__in=entities)
        .select_related("user", "entity", "role", "granted_by")
        .order_by("entity__code", "user__username", "role__code")
    )
    rows = []
    for g in grants:
        active = (g.revoked_at is None and g.valid_from <= on and (g.valid_to is None or g.valid_to >= on)
                  and g.user.is_active)
        rows.append({
            "entity": g.entity.code,
            "username": g.user.username,
            "name": {"en": g.user.full_name_en, "ar": g.user.full_name_ar},
            "user_type": g.user.user_type,
            "role": g.role.code,
            "valid_from": g.valid_from.isoformat(),
            "valid_to": g.valid_to.isoformat() if g.valid_to else None,
            "revoked_at": g.revoked_at.isoformat() if g.revoked_at else None,
            "granted_by": g.granted_by.username if g.granted_by_id else None,
            "active": active,
            "user_active": g.user.is_active,
            "last_login": g.user.last_login.isoformat() if g.user.last_login else None,
            "two_factor": user_has_device(g.user) if g.user.is_human else None,
        })
    audit(viewer, "access.review_report", viewer, object_type="access.review", object_id=on.isoformat(),
          after={"as_of": on, "rows": len(rows), "entities": sorted(e.code for e in entities)}, ctx=ctx)
    return rows


# ---------------------------------------------------------------- sign-in lockout reset (review E)


@transaction.atomic
def unlock_user(actor, user, ctx=None, setup=False):
    """Clear a sign-in lockout for `user` (failures before this point stop counting). Audited.
    actor=None is the `unlock_user` management command (server operator)."""
    if is_system_user(user):
        raise RuleError("system_user", "The ERP system user cannot sign in.", "لا يمكن لمستخدم النظام تسجيل الدخول.")
    if actor is not None:
        require_manage_user(actor, user, setup=setup)
    return audit(actor or system_user(), "access.unlock", user, after={"username": user.username}, ctx=ctx)
