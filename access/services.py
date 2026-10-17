"""Access checks, entity scoping and the audit log.

R1  scope(queryset, user, perm) filters every view/API queryset by entity access.
R2  has_perm(user, entity, perm): union of the user's roles in that entity only (D12).
R3  deactivate_user instead of delete.
R18 service agents never hold act/post/manage permissions, whatever their roles.
R19 audit() writes append-only AuditLog rows with before/after.
"""
import datetime
import threading
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
def grant_access(granter, user, entity, role, valid_from=None, valid_to=None, ctx=None, setup=False,
                 _approved_change=None):
    """Grant a role. granter=None is the operator bootstrap (`grant_access` command).
    D19: roles of a Managing Partner change only through a Chairman (or setup mode).
    D21: roles of a Chairman change only through an applied dual-control change (`_approved_change`)."""
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
    if _approved_change is not None:
        _require_applying(_approved_change, user)
    elif granter is not None:
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
    audit(granter or system_user(), "access.grant", grant, entity=entity, before=before,
          after=_with_change(snapshot(grant), _approved_change), ctx=ctx)
    return grant


@transaction.atomic
def revoke_access(revoker, grant, ctx=None, setup=False, _approved_change=None):
    if revoker is None:
        raise AccessDenied("permission_denied", "A revoke needs an acting user.", "يتطلب السحب مستخدماً منفذاً.")
    if _approved_change is not None:
        _require_applying(_approved_change, grant.user)
    else:
        _require_grant_rights(revoker, grant.user, grant.entity, grant.role, setup)
    before = snapshot(grant)
    grant.revoked_at = timezone.now()
    grant.save(update_fields=["revoked_at"])
    audit(revoker, "access.revoke", grant, entity=grant.entity, before=before,
          after=_with_change(snapshot(grant), _approved_change), ctx=ctx)
    return grant


_APPLYING = threading.local()


def _applying_ids():
    if not hasattr(_APPLYING, "ids"):
        _APPLYING.ids = set()
    return _APPLYING.ids


def _require_applying(change, target):
    """D21: only a dual-control change that _apply_chairman_change is applying right now may change a
    Chairman's account without the normal checks. Checked against the database (row applied, both
    approvals complete) and the in-progress marker, so an in-memory object cannot be forged."""
    from .models import ChairmanChange

    stored = None
    if isinstance(change, ChairmanChange) and change.pk in _applying_ids():
        stored = ChairmanChange.objects.filter(pk=change.pk, status=ChairmanChange.Status.APPLIED,
                                               target_id=target.pk).select_related("entity", "role").first()
    if stored is None or not _dual_complete(stored):
        raise AccessDenied("chairman_dual_control", "This change needs an approved dual-control request (D21).",
                           "يتطلب هذا التغيير طلب رقابة مزدوجة معتمداً (D21).")


def _with_change(after, change):
    if change is None:
        return after
    return {**(after or {}), "chairman_change": change.pk}


def _require_grant_rights(actor, user, entity, role, setup):
    """Who may grant or revoke `role` for `user` in `entity` (an acting user; operator commands pass none).

    - Nobody grants or revokes their own roles, in any mode (closes self-promotion to Chairman).
    - D19: only a holder of the Chairman role in that entity grants or revokes the Chairman role, so
      nobody can create a Chairman to get around the Managing Partner protection. Setup mode excepted.
    - D19: roles of a Managing Partner change only through a Chairman of all of their MP entities.
    - D21: roles of a Chairman change only through a dual-control change (CFO + MP of the group root),
      so here they are refused for everyone except setup mode (can_manage_user).
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
    if is_protected(user):
        require_manage_user(actor, user, setup=setup)  # D19, D21
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


def chairman_entities(user):
    """Entities where `user` holds the Chairman role (any non-revoked grant, even future or expired)."""
    return set(EntityAccess.objects.filter(user=user, role__code=CHAIRMAN_ROLE_CODE, revoked_at__isnull=True)
               .values_list("entity_id", flat=True))


def is_protected_chairman(user):
    return bool(chairman_entities(user))


def is_protected(user):
    """D19/D21: Managing Partner and Chairman accounts are managed only by a Chairman."""
    return is_protected_mp(user) or is_protected_chairman(user)


def can_manage_user(actor, target, setup=False):
    """Who may change `target`'s privileges, active status, password, roles, tokens, TOTP or lockout.

    - Nobody manages themselves; the ERP system user is never managed (2a).
    - D19: a Managing Partner is managed only by a user holding the Chairman role (an active grant)
      in every entity where the target is MP. Other MPs and user managers cannot. Setup mode excepted.
    - D21: nobody alone manages a user holding the Chairman role (any non-revoked grant) — not another
      Chairman, not the MP, not the CFO. Changes go through access.dual_control (CFO + MP of the group
      root entity). Setup mode and operator commands (no acting user) excepted.
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
    if is_protected_chairman(target):
        return False  # D21: dual control only
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


def role_holders(role):
    """Users holding `role` (any non-revoked grant)."""
    return list(User.objects.filter(entity_access__role=role, entity_access__revoked_at__isnull=True).distinct())


def role_affects_chairman(role):
    """D21: editing the chairman role, or a role any Chairman holds, changes a Chairman's privileges."""
    return role.code == CHAIRMAN_ROLE_CODE or any(is_protected_chairman(u) for u in role_holders(role))


def can_edit_role(actor, role, setup=False):
    """Role edits change the privileges of everyone holding the role.

    - D21: a role affecting a Chairman is changed only through a dual-control change (refused here).
    - D19: a role held by a Managing Partner (and managing_partner itself) needs a Chairman who can
      manage every MP holding it (see can_manage_user).
    The normal role-edit right is checked separately (admin). Setup mode excepted."""
    if setup:
        return True
    if role_affects_chairman(role):
        return False
    mps = [u for u in role_holders(role) if is_protected_mp(u)]
    if role.code != MP_ROLE_CODE and not mps:
        return True
    if actor is None or not active_grants(actor).filter(role__code=CHAIRMAN_ROLE_CODE).exists():
        return False
    return all(can_manage_user(actor, mp) for mp in mps)


def require_manage_user(actor, target, setup=False):
    if not can_manage_user(actor, target, setup=setup):
        if actor is not None and actor.pk != target.pk and not is_system_user(target):
            if is_protected_chairman(target):
                raise AccessDenied(
                    "chairman_dual_control",
                    "A Chairman's account changes only through a dual-control request approved by a CFO and a "
                    "Managing Partner of the group (D21).",
                    "لا يتغير حساب رئيس مجلس الإدارة إلا بطلب رقابة مزدوجة يعتمده المدير المالي والشريك المدير "
                    "للمجموعة (D21).",
                )
            if is_protected_mp(target):
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
def deactivate_user(actor, user, ctx=None, setup=False, _approved_change=None):
    """R3: users are deactivated, never deleted. D19 applies to Managing Partners, D21 to Chairmen."""
    if actor is None or is_system_user(user):
        raise AccessDenied("cannot_manage_user", "This user cannot be deactivated here.", "لا يمكن تعطيل هذا المستخدم هنا.")
    if _approved_change is not None:
        _require_applying(_approved_change, user)
    else:
        require_manage_user(actor, user, setup=setup)
    before = snapshot(user, ["is_active"])
    user.is_active = False
    user.save(update_fields=["is_active"])
    for token in user.service_tokens.filter(revoked_at__isnull=True):
        revoke_service_token(actor, token, ctx=ctx, setup=setup)
    audit(actor, "access.user.deactivate", user, before=before,
          after=_with_change(snapshot(user, ["is_active"]), _approved_change), ctx=ctx)
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
    if actor is not None and actor.pk == user.pk and user.password_set_by_others:
        raise RuleError("password_set_by_others", "Change the password you were given before enrolling a device.",
                        "غيّر كلمة المرور التي أُعطيت لك قبل تسجيل جهاز.")
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
def unlock_user(actor, user, ctx=None, setup=False, _approved_change=None):
    """Clear a sign-in lockout for `user` (failures before this point stop counting). Audited.
    actor=None is the `unlock_user` management command (server operator)."""
    if is_system_user(user):
        raise RuleError("system_user", "The ERP system user cannot sign in.", "لا يمكن لمستخدم النظام تسجيل الدخول.")
    if _approved_change is not None:
        _require_applying(_approved_change, user)
    elif actor is not None:
        require_manage_user(actor, user, setup=setup)
    return audit(actor or system_user(), "access.unlock", user,
                 after=_with_change({"username": user.username}, _approved_change), ctx=ctx)


# ---------------------------------------------------------------- D21: dual control for Chairman accounts

DUAL_CONTROL_ROLES = ("cfo", MP_ROLE_CODE)
CHAIRMAN_UPDATE_FIELDS = {"is_active", "is_staff", "full_name_en", "full_name_ar", "email", "language"}


def group_root_entity():
    """The group root entity (no parent). D21 approvals come from its CFO and MP."""
    roots = list(Entity.objects.filter(parent__isnull=True, is_active=True)[:2])
    if len(roots) != 1:
        raise RuleError("no_group_root", "Exactly one active group root entity is needed for dual control.",
                        "يلزم وجود منشأة أم واحدة نشطة للمجموعة للرقابة المزدوجة.")
    return roots[0]


def _affected_chairmen(change):
    if change.target_id:
        return {change.target_id}
    return {u.pk for u in role_holders(change.role) if is_protected_chairman(u)}


def _dual_roles(user, entity):
    return set(active_grants(user, entity).filter(role__code__in=DUAL_CONTROL_ROLES)
               .values_list("role__code", flat=True))


def _require_dual_approver(actor, change):
    """A CFO or MP of the root entity; active human; never an affected Chairman (D21)."""
    if actor is None or not actor.is_active or not actor.is_human:
        raise AccessDenied("dual_control_approver", "Only active people can request or approve this change.",
                           "يقدّم هذا التغيير أو يعتمده الأشخاص النشطون فقط.")
    if actor.pk in _affected_chairmen(change):
        raise AccessDenied("dual_control_own_account", "A Chairman cannot request or approve changes to his own "
                           "account or roles (D21).", "لا يحق لرئيس مجلس الإدارة طلب تغيير حسابه أو أدواره أو اعتماده (D21).")
    roles = _dual_roles(actor, change.entity)
    if not roles:
        raise AccessDenied(
            "dual_control_approver",
            f"Only a CFO or Managing Partner of {change.entity.code} can request or approve changes to a "
            "Chairman's account (D21).",
            f"لا يحق إلا للمدير المالي أو الشريك المدير في {change.entity.code} طلب تغيير حساب رئيس مجلس الإدارة "
            "أو اعتماده (D21).",
        )
    return roles


def _clean_payload(action, target, role, payload, entity):
    from django.contrib.auth.hashers import make_password
    from django.contrib.auth.password_validation import validate_password
    from django.core.exceptions import ValidationError

    from .models import ChairmanChange as CC

    payload = dict(payload or {})
    if action == CC.Action.PASSWORD:
        raw = payload.pop("password", "")
        try:
            validate_password(raw, target)
        except ValidationError as exc:
            raise RuleError("invalid_password", " ".join(exc.messages), "كلمة المرور غير مقبولة.")
        return {"password_hash": make_password(raw)}  # never stored in plain text
    if action == CC.Action.TOTP_RESET:
        return {}
    if action in (CC.Action.DEACTIVATE, CC.Action.UNLOCK):
        return {}
    if action == CC.Action.UPDATE:
        fields = payload.get("fields") or {}
        bad = set(fields) - CHAIRMAN_UPDATE_FIELDS
        if not fields or bad:
            raise RuleError("invalid_fields", f"Fields that can change here: {sorted(CHAIRMAN_UPDATE_FIELDS)}.",
                            "الحقول المسموح بتغييرها هنا محدودة.")
        return {"fields": fields}
    if action == CC.Action.GRANT:
        grant_role = Role.objects.get(code=payload["role"])
        grant_entity = Entity.objects.get(code=payload["entity"])
        return {"role": grant_role.code, "entity": grant_entity.code,
                "valid_from": to_json(payload.get("valid_from")), "valid_to": to_json(payload.get("valid_to"))}
    if action == CC.Action.REVOKE:
        grant = EntityAccess.objects.get(pk=payload["grant"], user=target, revoked_at__isnull=True)
        return {"grant": grant.pk, "role": grant.role.code, "entity": grant.entity.code}
    if action == CC.Action.ROLE_PERMISSIONS:
        codes = sorted(set(payload.get("permissions") or []))
        from .models import Permission

        found = set(Permission.objects.filter(code__in=codes).values_list("code", flat=True))
        if found != set(codes):
            raise RuleError("unknown_permission", f"Unknown permissions: {sorted(set(codes) - found)}.",
                            "صلاحيات غير معروفة.")
        return {"permissions": codes}
    raise RuleError("invalid_action", "Unknown change.", "تغيير غير معروف.")


def _public_payload(change):
    return {k: v for k, v in change.payload.items() if k != "password_hash"}


@transaction.atomic
def request_chairman_change(actor, action, *, target=None, role=None, payload=None, entity=None, ctx=None):
    """D21: a CFO or MP of the group root proposes a change to a Chairman's account (or to a role a
    Chairman holds). Their request counts as their approval; a second person in the other role must
    approve before it applies."""
    import datetime as _dt

    from django.conf import settings

    from .models import ChairmanChange as CC, ChairmanChangeApproval

    entity = entity or group_root_entity()
    if entity.parent_id is not None:
        raise RuleError("not_root_entity", "Dual control runs in the group root entity.",
                        "تتم الرقابة المزدوجة في المنشأة الأم للمجموعة.")
    action = CC.Action(action)
    if action == CC.Action.ROLE_PERMISSIONS:
        if role is None or target is not None or not role_affects_chairman(role):
            raise RuleError("not_chairman_change", "Use the normal role edit: no Chairman holds this role.",
                            "استخدم تعديل الأدوار المعتاد: لا يحمل أي رئيس مجلس إدارة هذا الدور.")
    elif target is None or role is not None or not is_protected_chairman(target):
        raise RuleError("not_chairman_change", "Dual control is only for users holding the Chairman role.",
                        "الرقابة المزدوجة لحسابات رؤساء مجلس الإدارة فقط.")
    if target is not None and is_system_user(target):
        raise RuleError("system_user", "The ERP system user cannot be changed.", "لا يمكن تعديل مستخدم النظام.")
    _require_factors_apart(action, target)
    days = getattr(settings, "ERP_CHAIRMAN_CHANGE_EXPIRY_DAYS", 7)
    at = timezone.now()
    change = CC(entity=entity, target=target, role=role, action=action, requested_by=actor, requested_at=at,
                expires_at=at + _dt.timedelta(days=days))
    roles = _require_dual_approver(actor, change)
    change.payload = _clean_payload(action, target, role, payload, entity)
    change.save()
    ChairmanChangeApproval.objects.create(change=change, approver=actor, roles=sorted(roles), at=at)
    audit(actor, "access.chairman_change.request", change, entity=entity,
          after={"action": action, "target": target.pk if target else None, "role": role.code if role else None,
                 "payload": _public_payload(change), "expires_at": change.expires_at, "approver_roles": sorted(roles)},
          ctx=ctx)
    return change


def _require_factors_apart(action, target):
    """D21 (b): the people who set a Chairman's password must never also control his second factor.
    No 2FA reset while the password was set by others; no password set while a 2FA re-enrolment is pending."""
    from .models import ChairmanChange as CC

    if target is None:
        return
    if action == CC.Action.TOTP_RESET and target.password_set_by_others:
        raise RuleError("factors_apart", "The Chairman must first change the password he was given; only then can "
                        "his authenticator app be reset.", "يجب أن يغيّر رئيس مجلس الإدارة كلمة المرور المعطاة له "
                        "أولاً، ثم يمكن إعادة تعيين تطبيق المصادقة.")
    if action == CC.Action.PASSWORD and target.totp_enrolment_required:
        raise RuleError("factors_apart", "The Chairman must first enrol his new authenticator app; only then can his "
                        "password be set.", "يجب أن يسجل رئيس مجلس الإدارة تطبيق المصادقة الجديد أولاً، ثم يمكن تعيين "
                        "كلمة المرور.")


def _expire_if_due(change):
    """Commit the expiry of a due request in its own transaction (so the caller's refusal keeps it)."""
    from .models import ChairmanChange as CC

    with transaction.atomic():
        locked = CC.objects.select_for_update().get(pk=change.pk)
        if locked.status != CC.Status.PENDING or locked.expires_at > timezone.now():
            return False
        locked.status = CC.Status.EXPIRED
        locked.decided_at = timezone.now()
        locked._allow_save = True
        locked.save(update_fields=["status", "decided_at"])
        audit(system_user(), "access.chairman_change.expire", locked, entity=locked.entity,
              before={"status": CC.Status.PENDING}, after={"status": CC.Status.EXPIRED})
    return True


def expire_chairman_changes():
    """Cron helper: expire all due pending requests. Returns how many expired."""
    from .models import ChairmanChange as CC

    due = CC.objects.filter(status=CC.Status.PENDING, expires_at__lte=timezone.now())
    return sum(1 for c in due if _expire_if_due(c))


def _dual_complete(change):
    """Two distinct approvers, one currently a CFO and the other currently an MP of the root entity."""
    people = [(a.approver, _dual_roles(a.approver, change.entity)) for a in change.approvals.select_related("approver")
              if a.approver.is_active and a.approver.pk not in _affected_chairmen(change)]
    for i, (u1, r1) in enumerate(people):
        for u2, r2 in people[i + 1:]:
            if u1.pk != u2.pk and (("cfo" in r1 and MP_ROLE_CODE in r2) or (MP_ROLE_CODE in r1 and "cfo" in r2)):
                return True
    return False


def _lock_pending(change):
    from .errors import Conflict
    from .models import ChairmanChange as CC

    locked = CC.objects.select_for_update(of=("self",)).select_related("entity", "target", "role").get(pk=change.pk)
    if locked.status != CC.Status.PENDING:
        raise Conflict("dual_control_decided", f"This request is already {locked.status}.",
                       "تم البت في هذا الطلب.")
    if locked.expires_at <= timezone.now():  # expired between the pre-check and the lock
        raise Conflict("dual_control_expired", "This request has expired.", "انتهت صلاحية هذا الطلب.")
    return locked


def _refuse_if_expired(change):
    """Run before the caller's transaction so a committed expiry survives the refusal."""
    from .errors import Conflict

    if _expire_if_due(change):
        raise Conflict("dual_control_expired", "This request has expired.", "انتهت صلاحية هذا الطلب.")


def approve_chairman_change(actor, change, ctx=None):
    """Second approval. Applies the change when a CFO and an MP (two people) have approved. Returns
    (change, result); result is the applied object (e.g. a new grant) or None. Never returns a secret."""
    from .errors import Conflict
    from .models import ChairmanChangeApproval

    _refuse_if_expired(change)
    with transaction.atomic():
        change = _lock_pending(change)
        roles = _require_dual_approver(actor, change)
        if change.approvals.filter(approver=actor).exists():
            raise Conflict("dual_control_same_person", "You have already approved this request; a second person "
                           "must approve.", "سبق أن اعتمدت هذا الطلب؛ يجب أن يعتمده شخص آخر.")
        ChairmanChangeApproval.objects.create(change=change, approver=actor, roles=sorted(roles))
        audit(actor, "access.chairman_change.approve", change, entity=change.entity,
              after={"approver_roles": sorted(roles)}, ctx=ctx)
        result = None
        if _dual_complete(change):
            result = _apply_chairman_change(actor, change, ctx)
        return change, result


def reject_chairman_change(actor, change, reason="", ctx=None):
    _refuse_if_expired(change)
    with transaction.atomic():
        return _reject(actor, change, reason, ctx)


def _reject(actor, change, reason, ctx):
    from .models import ChairmanChange as CC

    change = _lock_pending(change)
    _require_dual_approver(actor, change)
    change.status = CC.Status.REJECTED
    change.decided_at, change.decided_by, change.reason = timezone.now(), actor, reason or ""
    change._allow_save = True
    change.save(update_fields=["status", "decided_at", "decided_by", "reason"])
    audit(actor, "access.chairman_change.reject", change, entity=change.entity,
          before={"status": CC.Status.PENDING}, after={"status": change.status, "reason": change.reason}, ctx=ctx)
    return change


def _apply_chairman_change(actor, change, ctx):
    from .models import ChairmanChange as CC

    change.status = CC.Status.APPLIED
    change.decided_at, change.decided_by = timezone.now(), actor
    change._allow_save = True
    change.save(update_fields=["status", "decided_at", "decided_by"])
    _applying_ids().add(change.pk)
    target, p, result = change.target, change.payload, None
    if target is not None:
        target.refresh_from_db()
        _require_factors_apart(change.action, target)  # checked again at the moment it applies
    try:
        if change.action == CC.Action.PASSWORD:
            before = {"has_usable_password": target.has_usable_password()}
            target.password = p["password_hash"]
            target.password_set_by_others = True
            target.save(update_fields=["password", "password_set_by_others"])
            audit(actor, "access.user.password_change", target, before=before,
                  after={"chairman_change": change.pk, "password_set_by_others": True}, ctx=ctx)
        elif change.action == CC.Action.TOTP_RESET:
            from django_otp.plugins.otp_totp.models import TOTPDevice

            removed = list(TOTPDevice.objects.filter(user=target).values_list("pk", flat=True))
            TOTPDevice.objects.filter(pk__in=removed).delete()
            target.totp_enrolment_required = True
            target.save(update_fields=["totp_enrolment_required"])
            audit(actor, "access.totp.reset", target, after={"removed_devices": removed,
                  "totp_enrolment_required": True, "chairman_change": change.pk}, ctx=ctx)
        elif change.action == CC.Action.DEACTIVATE:
            result = deactivate_user(actor, target, ctx=ctx, _approved_change=change)
        elif change.action == CC.Action.UNLOCK:
            result = unlock_user(actor, target, ctx=ctx, _approved_change=change)
        elif change.action == CC.Action.UPDATE:
            fields = sorted(p["fields"])
            before = snapshot(target, fields)
            for f in fields:
                setattr(target, f, p["fields"][f])
            target.full_clean(exclude=["password"])
            target.save(update_fields=fields)
            privileged = set(fields) & {"is_active", "is_staff"}
            audit(actor, "access.user.privilege_change" if privileged else "access.user.update", target,
                  before=before, after=_with_change(snapshot(target, fields), change), ctx=ctx)
        elif change.action == CC.Action.GRANT:
            vf, vt = p.get("valid_from"), p.get("valid_to")
            result = grant_access(actor, target, Entity.objects.get(code=p["entity"]), p["role"],
                                  valid_from=datetime.date.fromisoformat(vf) if vf else None,
                                  valid_to=datetime.date.fromisoformat(vt) if vt else None,
                                  ctx=ctx, _approved_change=change)
        elif change.action == CC.Action.REVOKE:
            result = revoke_access(actor, EntityAccess.objects.get(pk=p["grant"]), ctx=ctx, _approved_change=change)
        elif change.action == CC.Action.ROLE_PERMISSIONS:
            from .models import Permission

            role = change.role
            before = sorted(role.permissions.values_list("code", flat=True))
            role.permissions.set(Permission.objects.filter(code__in=p["permissions"]))
            audit(actor, "access.role.change", role, entity=None, before={"permissions": before},
                  after={"permissions": p["permissions"], "chairman_change": change.pk}, ctx=ctx)
    finally:
        _applying_ids().discard(change.pk)
    audit(actor, "access.chairman_change.apply", change, entity=change.entity,
          after={"action": change.action, "payload": _public_payload(change)}, ctx=ctx)
    return result


# ---------------------------------------------------------------- self-service (D21 a) and 2FA self-enrolment (D21 b)

#: What a user may change on his own account himself (D21 a). Never roles, privileges or active status.
SELF_SERVICE_FIELDS = {"email", "full_name_en", "full_name_ar", "language", "default_entity"}


def password_changed_by_self(user, ctx=None):
    """Called after a user changed his own password: the password is no longer one others set."""
    if user.password_set_by_others:
        user.password_set_by_others = False
        user.save(update_fields=["password_set_by_others"])
    audit(user, "access.user.password_change", user, after={"self_service": True}, ctx=ctx)


def start_self_enrolment(user, ctx=None):
    """D21 (b): after a 2FA reset the user, signed in with his password only, enrols a new device himself.
    Returns an unconfirmed device whose secret is shown only to him; any earlier unconfirmed device is
    replaced (its secret stops working)."""
    from django_otp.plugins.otp_totp.models import TOTPDevice

    if not (user.is_active and user.is_human and user.totp_enrolment_required) or user.password_set_by_others:
        raise AccessDenied("no_enrolment_pending", "No authenticator enrolment is pending for this account.",
                           "لا يوجد تسجيل معلق لتطبيق المصادقة لهذا الحساب.")
    with transaction.atomic():
        TOTPDevice.objects.filter(user=user, confirmed=False).delete()
        device = TOTPDevice.objects.create(user=user, name="Authenticator app", confirmed=False)
        audit(user, "access.totp.self_enrol_start", user, after={"device_id": device.pk}, ctx=ctx)
    return device


@transaction.atomic
def finish_self_enrolment(user, device, token, ctx=None):
    """Confirm the device with a code from the app; clears the enrolment flag. Returns True on success."""
    if device.user_id != user.pk or device.confirmed or not user.totp_enrolment_required:
        raise AccessDenied("no_enrolment_pending", "No authenticator enrolment is pending for this account.",
                           "لا يوجد تسجيل معلق لتطبيق المصادقة لهذا الحساب.")
    if not device.verify_token(token):
        audit(user, "access.totp.self_enrol_failed", user, after={"device_id": device.pk}, ctx=ctx)
        return False
    device.confirmed = True
    device.save(update_fields=["confirmed"])
    user.totp_enrolment_required = False
    user.save(update_fields=["totp_enrolment_required"])
    audit(user, "access.totp.enroll", user, after={"device_id": device.pk, "self_service": True}, ctx=ctx)
    return True
