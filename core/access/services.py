"""Access services (access.md 3.7): the one way every module checks rights.

Nothing here is cached: every call reads memberships again (R4).
"""
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.utils import timezone

from core.models import Entity

from . import catalogue
from .models import AccessLog, DualControlRequest, Kind, Membership, Role, UserProfile

DUAL_CONTROL_TTL = timedelta(hours=72)  # OQ-11 default

# OQ-2 default: an access_admin grants every entity role except these.
ACCESS_ADMIN_CANNOT_GRANT = frozenset({"access_admin", "approver", "zatca_admin"})


def now():
    """Current time; a function so tests can move the clock."""
    return timezone.now()


class AccessUsageError(RuntimeError):
    """A programming error in how a module calls the access layer (e.g. A permission without a document)."""


# Bilingual reasons for refusals (R28).
MESSAGES = {
    "no_user": ("You are not signed in.", "لم تقم بتسجيل الدخول."),
    "inactive_user": ("This user account is inactive.", "حساب المستخدم هذا غير نشط."),
    "no_membership": (
        "You do not have the right '{perm}' in {entity}.",
        "ليست لديك صلاحية '{perm_ar}' في {entity}.",
    ),
    "agent_forbidden": (
        "AI agents may not use '{perm}'; a person must do this.",
        "لا يجوز لوكلاء الذكاء الاصطناعي استخدام صلاحية '{perm_ar}'؛ يجب أن يقوم بذلك شخص.",
    ),
    "system_forbidden": (
        "This system user may not use '{perm}'.",
        "لا يجوز لمستخدم النظام هذا استخدام صلاحية '{perm_ar}'.",
    ),
    "system_only": (
        "'{perm}' is reserved for a system user.",
        "صلاحية '{perm_ar}' مخصصة لمستخدم النظام.",
    ),
    "inactive_entity": (
        "{entity} is inactive; only viewing is allowed.",
        "الكيان {entity} غير نشط؛ يُسمح بالعرض فقط.",
    ),
    "self_approval": (
        "You created or submitted this document, so you cannot approve it.",
        "لا يمكنك اعتماد مستند أنشأته أو قدمته بنفسك.",
    ),
    "self_grant": (
        "Nobody can grant, revoke or delegate rights to themselves.",
        "لا يجوز لأحد منح الصلاحيات أو إلغاؤها أو تفويضها لنفسه.",
    ),
    "role_not_grantable": (
        "You may not grant or revoke the role '{role}'.",
        "لا يجوز لك منح الدور '{role_ar}' أو إلغاؤه.",
    ),
    "delegate_cannot_delegate": (
        "Only the holder of an approver membership can delegate it; delegates cannot delegate further.",
        "لا يفوِّض إلا صاحب عضوية المعتمد؛ ولا يجوز للمفوَّض التفويض مرة أخرى.",
    ),
    "grant_other_entity": (
        "This authorisation is for another entity.",
        "هذا التفويض لكيان آخر.",
    ),
    "grant_wrong_permission": (
        "This authorisation does not allow this action.",
        "هذا التفويض لا يسمح بهذا الإجراء.",
    ),
    "grant_missing": (
        "This action needs an authorisation (grant) from the access layer.",
        "يتطلب هذا الإجراء تفويضًا من نظام الصلاحيات.",
    ),
    "grant_wrong_document": (
        "This authorisation was given for another document.",
        "هذا التفويض صادر لمستند آخر.",
    ),
    "dual_control": (
        "This action needs an approved, unused four-eyes request for exactly this change.",
        "يتطلب هذا الإجراء طلب رقابة مزدوجة معتمدًا وغير مستخدم لهذا التغيير بعينه.",
    ),
    "dual_control_expired": ("The four-eyes request has expired.", "انتهت صلاحية طلب الرقابة المزدوجة."),
}


class AccessDenied(PermissionDenied):
    """Refusal from the access layer, with English and Arabic messages (R28)."""

    def __init__(self, code, message_en, message_ar, language="en"):
        self.code = code
        self.message_en = message_en
        self.message_ar = message_ar
        self.language = language
        super().__init__(self.message)

    @property
    def message(self):
        return self.message_ar if self.language == "ar" else self.message_en

    def __str__(self):
        return self.message


@dataclass(frozen=True)
class Grant:
    """Proof that ``user`` held ``permission`` in ``entity`` at ``checked_at``.

    Services that receive a Grant (e.g. ``ledger.services.post_entry``) still
    call :func:`verify_grant`, which checks the right again (R4), so a forged
    or stale Grant gives nothing.
    """

    user: object
    entity: Entity
    permission: str
    membership_ids: tuple
    checked_at: datetime
    log_id: int | None = None
    document_ref: tuple | None = None  # (app_label.model, pk) for A / S! checks
    self_approval: bool = False


# --- helpers ---------------------------------------------------------------

def profile_of(user):
    if user is None or not getattr(user, "pk", None):
        return None
    try:
        return user.access_profile
    except UserProfile.DoesNotExist:
        # Signal creates profiles for every user; users created by raw SQL fall back to human.
        return UserProfile(user=user, kind=Kind.HUMAN)


def kind_of(user):
    profile = profile_of(user)
    return profile.kind if profile else Kind.HUMAN


def source_of(user):
    profile = profile_of(user)
    return profile.source if profile else ""


def _language(user):
    profile = profile_of(user)
    return profile.preferred_language if profile and profile.preferred_language else "en"


def _document_ref(document):
    if document is None or getattr(document, "pk", None) is None:
        return None
    return (document._meta.label_lower, document.pk)


def _user_id(value):
    if value is None:
        return None
    return getattr(value, "pk", value)


def _document_people(document):
    people = set()
    for attr in ("created_by", "submitted_by"):
        uid = getattr(document, f"{attr}_id", None)
        if uid is None:
            uid = _user_id(getattr(document, attr, None))
        if uid is not None:
            people.add(uid)
    return people


def log(event, *, actor=None, entity=None, subject_user=None, membership=None, role=None, permission="", details=None, source=None):
    """Write one AccessLog row (R27). Never pass passwords, tokens, keys or OTPs in ``details``."""
    actor = actor if getattr(actor, "pk", None) else None
    return AccessLog.objects.create(
        at=now(),
        actor=actor,
        actor_kind=kind_of(actor),
        event=event,
        entity=entity,
        subject_user=subject_user,
        membership=membership,
        role=role,
        permission=permission,
        details=details or {},
        source=source if source is not None else (source_of(actor) if actor else ""),
    )


def _deny(user, entity, permission, code, **fmt):
    perm = catalogue.CATALOGUE.get(permission)
    values = dict(
        perm=perm.label_en if perm else permission,
        perm_ar=perm.label_ar if perm else permission,
        entity=entity.code if entity is not None else "",
        role="",
        role_ar="",
    )
    values.update(fmt)
    en, ar = MESSAGES[code]
    log(AccessLog.Event.PERMISSION_DENIED, actor=user, entity=entity, permission=permission, details={"reason": code})
    raise AccessDenied(code, en.format(**values), ar.format(**values), _language(user))


def _kind_refusal(user, perm):
    """Reason code if the user's kind may never use ``perm`` (R12, R13), else None."""
    profile = profile_of(user)
    kind = profile.kind if profile else Kind.HUMAN
    if kind == Kind.AGENT:
        return None if perm.agent_ok else "agent_forbidden"
    if kind == Kind.SYSTEM:
        return None if perm.codename in catalogue.allowed_for_system(profile.system_code) else "system_forbidden"
    if perm.system is not None:
        return "system_only"
    return None


def covering_entities(entity):
    """Entities whose *group* memberships cover ``entity`` (R6).

    ``entity`` itself, then each ancestor reached through a chain of
    ``consolidation=full`` links. Equity and non-consolidated entities stop the chain.
    """
    chain = [entity]
    current = entity
    seen = {entity.pk}
    while current.consolidation == Entity.Consolidation.FULL and current.parent_id and current.parent_id not in seen:
        current = current.parent
        seen.add(current.pk)
        chain.append(current)
    return chain


def _covered_by_group(entity):
    """Entities a group membership on ``entity`` covers: itself plus full descendants (R6)."""
    result = [entity]
    frontier = [entity.pk]
    while frontier:
        children = list(
            Entity.objects.filter(parent_id__in=frontier, consolidation=Entity.Consolidation.FULL).exclude(
                pk__in=[e.pk for e in result]
            )
        )
        result += children
        frontier = [c.pk for c in children]
    return result


def _memberships_for(user, entity, permission, at):
    """Active memberships that give ``permission`` in ``entity`` (R3, R5, R6).

    Re-applies the catalogue limits per role (R12, R13), so a permission
    forced into the database on the wrong role grants nothing.
    """
    kind = kind_of(user)
    chain = covering_entities(entity)
    qs = (
        Membership.objects.active(at)
        .filter(user=user, role__permission_rows__codename=permission, role__kind=kind)
        .filter(
            Q(entity=entity, role__scope=Role.Scope.ENTITY)
            | Q(entity__in=chain, role__scope=Role.Scope.GROUP, entity__consolidation=Entity.Consolidation.PARENT)
        )
        .select_related("role")
        .distinct()
    )
    perm = catalogue.get(permission)
    return [m for m in qs if catalogue.role_may_hold(m.role.kind, m.role.scope, m.role.system_code, perm) is None]


def _evaluate(user, entity, permission, at):
    """Return (memberships, None) when allowed, or ([], reason_code)."""
    perm = catalogue.get(permission)
    if user is None or not getattr(user, "pk", None) or not getattr(user, "is_authenticated", False):
        return [], "no_user"
    if not user.is_active:
        return [], "inactive_user"
    refusal = _kind_refusal(user, perm)
    if refusal:
        return [], refusal
    if not entity.is_active and not perm.is_view:
        return [], "inactive_entity"
    memberships = _memberships_for(user, entity, permission, at)
    if not memberships:
        return [], "no_membership"
    return memberships, None


# --- checking rights --------------------------------------------------------

def require(user, entity, permission, *, document=None, allow_self_approval=False):
    """Return a Grant if ``user`` may use ``permission`` in ``entity`` now; else log and raise AccessDenied.

    Superuser, staff, Django groups and user_permissions give nothing (R3, R24).
    """
    perm = catalogue.get(permission)
    if perm.is_approval and document is None:
        raise AccessUsageError(f"{permission} is an approval permission; pass the document (R14).")
    if perm.is_dual_control:
        raise AccessUsageError(f"{permission} needs dual control; use require_dual_control (R19).")
    at = now()
    memberships, reason = _evaluate(user, entity, permission, at)
    if reason:
        _deny(user, entity, permission, reason)
    self_approval = False
    if document is not None and (perm.is_approval or perm.is_segregated) and user.pk in _document_people(document):
        if perm.is_segregated or not allow_self_approval:  # R15: S! ignores allow_self_approval
            _deny(user, entity, permission, "self_approval")
        self_approval = True
    log_id = None
    if perm.is_approval or perm.posts or self_approval:
        entry = log(
            AccessLog.Event.PERMISSION_GRANTED,
            actor=user,
            entity=entity,
            permission=permission,
            details={
                "self_approval": self_approval,
                "document": list(_document_ref(document)) if _document_ref(document) else None,
                "membership_ids": [m.pk for m in memberships],
            },
        )
        log_id = entry.pk
    return Grant(
        user=user,
        entity=entity,
        permission=permission,
        membership_ids=tuple(m.pk for m in memberships),
        checked_at=at,
        log_id=log_id,
        document_ref=_document_ref(document),
        self_approval=self_approval,
    )


def has(user, entity, permission):
    """True if ``user`` currently holds ``permission`` in ``entity``. No logging, no segregation check."""
    memberships, reason = _evaluate(user, entity, permission, now())
    return reason is None


def entities_for(user, permission):
    """Entities in which ``user`` currently holds ``permission`` (R2, R6)."""
    perm = catalogue.get(permission)
    if user is None or not getattr(user, "pk", None) or not user.is_active or _kind_refusal(user, perm):
        return Entity.objects.none()
    at = now()
    kind = kind_of(user)
    ids = set()
    qs = Membership.objects.active(at).filter(user=user, role__permission_rows__codename=permission, role__kind=kind)
    for m in qs.select_related("role", "entity").distinct():
        if catalogue.role_may_hold(m.role.kind, m.role.scope, m.role.system_code, perm):
            continue
        if m.role.scope == Role.Scope.GROUP:
            if m.entity.consolidation == Entity.Consolidation.PARENT:
                ids.update(e.pk for e in _covered_by_group(m.entity))
        else:
            ids.add(m.entity_id)
    result = Entity.objects.filter(pk__in=ids)
    if not perm.is_view:
        result = result.filter(is_active=True)
    return result


def scope(queryset, user, permission, entity_field="entity"):
    """Filter ``queryset`` to records in entities where ``user`` holds ``permission`` (R2)."""
    return queryset.filter(**{f"{entity_field}__in": entities_for(user, permission)})


def get_scoped_or_404(queryset, user, permission, entity_field="entity", **lookup):
    """Fetch one record through :func:`scope`; records outside the user's entities are 404, never 403 (R2)."""
    try:
        return scope(queryset, user, permission, entity_field).get(**lookup)
    except queryset.model.DoesNotExist:
        raise Http404("Not found / غير موجود") from None


def verify_grant(grant, entity, allowed_permissions, *, document=None):
    """Check a Grant passed to a service: right type, same entity, and still valid now (R4, R23).

    Returns the user the grant belongs to. Refusals are logged and raise AccessDenied.
    """
    if not isinstance(grant, Grant):
        _deny(None, entity, "", "grant_missing")
    user = grant.user
    if grant.entity.pk != entity.pk:
        _deny(user, entity, grant.permission, "grant_other_entity")
    if grant.permission not in allowed_permissions:
        _deny(user, entity, grant.permission, "grant_wrong_permission")
    perm = catalogue.get(grant.permission)
    _, reason = _evaluate(user, entity, grant.permission, now())
    if reason:
        _deny(user, entity, grant.permission, reason)
    if document is not None:
        if grant.document_ref != _document_ref(document):
            _deny(user, entity, grant.permission, "grant_wrong_document")
        if (perm.is_approval or perm.is_segregated) and user.pk in _document_people(document) and not (
            grant.self_approval and not perm.is_segregated
        ):
            _deny(user, entity, grant.permission, "self_approval")
    return user


# --- memberships -------------------------------------------------------------

def _grantor_is_restricted(grant):
    """True when the grant to manage access comes only from access_admin, not managing_partner (OQ-2)."""
    codes = set(Membership.objects.filter(pk__in=grant.membership_ids).values_list("role__code", flat=True))
    return "managing_partner" not in codes


def _check_grantable(actor, grant, entity, role):
    if _grantor_is_restricted(grant) and (role.code in ACCESS_ADMIN_CANNOT_GRANT or role.scope == Role.Scope.GROUP):
        _deny(actor, entity, "core.grant_membership", "role_not_grantable", role=role.code, role_ar=role.name_ar)


def _membership_details(m):
    return {
        "role": m.role.code,
        "valid_from": m.valid_from.isoformat(),
        "valid_to": m.valid_to.isoformat() if m.valid_to else None,
        "reason": m.reason,
        "delegated_from": m.delegated_from_id,
    }


def grant_membership(actor, user, entity, role, valid_from, valid_to=None, reason="", delegated_from=None):
    """Grant ``role`` to ``user`` in ``entity`` (R8, R9, R11, R27)."""
    if delegated_from is not None:
        raise AccessUsageError("CFO delegations are created with delegate_approval (R16).")
    grant = require(actor, entity, "core.grant_membership")
    if _user_id(user) == actor.pk:
        _deny(actor, entity, "core.grant_membership", "self_grant")
    _check_grantable(actor, grant, entity, role)
    with transaction.atomic():
        m = Membership(
            user=user, entity=entity, role=role, valid_from=valid_from, valid_to=valid_to,
            granted_by=actor, granted_at=now(), reason=reason,
        )
        m.save()
        log(
            AccessLog.Event.MEMBERSHIP_GRANTED, actor=actor, entity=entity, subject_user=user, membership=m,
            role=role, permission="core.grant_membership", details=_membership_details(m),
        )
    return m


def revoke_membership(actor, membership, reason):
    """Revoke a membership: one final write, logged (R8, R9, R27).

    Needs ``core.grant_membership`` in the entity, or, for a CFO delegation,
    the delegator's own ``core.delegate_approval``.
    """
    entity = membership.entity
    is_delegation = membership.delegated_from_id is not None
    if is_delegation and membership.delegated_from.user_id == getattr(actor, "pk", None):
        require(actor, entity, "core.delegate_approval")
        permission = "core.delegate_approval"
    else:
        grant = require(actor, entity, "core.grant_membership")
        permission = "core.grant_membership"
        _check_grantable(actor, grant, entity, membership.role)
    if membership.user_id == actor.pk:
        _deny(actor, entity, permission, "self_grant")
    if not reason:
        raise ValidationError("A reason is required. / السبب مطلوب.")
    with transaction.atomic():
        m = Membership.objects.select_for_update().get(pk=membership.pk)
        m.revoked_at = now()
        m.revoked_by = actor
        m.revoke_reason = reason
        m.save()
        log(
            AccessLog.Event.DELEGATION_REVOKED if is_delegation else AccessLog.Event.MEMBERSHIP_REVOKED,
            actor=actor, entity=entity, subject_user=m.user, membership=m, role=m.role, permission=permission,
            details={"reason": reason, "revoked_at": m.revoked_at.isoformat()},
        )
    return m


def delegate_approval(actor, delegate, entity, valid_from, valid_to, reason, source=None):
    """Create a CFO delegate: an approver membership with ``delegated_from`` set (R16).

    The delegator delegates their own approver membership; a managing_partner
    may pass ``source`` (or rely on there being exactly one approver membership).
    """
    if valid_to is None:
        raise ValidationError("A delegation needs an end date. / يجب تحديد تاريخ انتهاء التفويض.")
    grant = require(actor, entity, "core.delegate_approval")
    at = now()
    by_mp = not _grantor_is_restricted(grant)
    own = list(
        Membership.objects.active(at).filter(
            user=actor, entity=entity, role__code="approver", role__scope=Role.Scope.ENTITY, delegated_from__isnull=True
        )
    )
    if source is None:
        if own:
            source = own[0]
        elif by_mp:
            candidates = list(
                Membership.objects.active(at).filter(entity=entity, role__code="approver", delegated_from__isnull=True)
            )
            if len(candidates) != 1:
                raise ValidationError("Choose which approver membership is delegated. / حدّد عضوية المعتمد المراد تفويضها.")
            source = candidates[0]
        else:
            _deny(actor, entity, "core.delegate_approval", "delegate_cannot_delegate")
    elif not by_mp and source.pk not in {m.pk for m in own}:
        _deny(actor, entity, "core.delegate_approval", "delegate_cannot_delegate")
    if not source.is_active_at(at) or source.delegated_from_id or source.entity_id != entity.pk:
        _deny(actor, entity, "core.delegate_approval", "delegate_cannot_delegate")
    if _user_id(delegate) in (actor.pk, source.user_id):
        _deny(actor, entity, "core.delegate_approval", "self_grant")
    with transaction.atomic():
        m = Membership(
            user=delegate, entity=entity, role=source.role, valid_from=valid_from, valid_to=valid_to,
            granted_by=actor, granted_at=at, reason=reason, delegated_from=source,
        )
        m.save()
        log(
            AccessLog.Event.DELEGATION_GRANTED, actor=actor, entity=entity, subject_user=delegate, membership=m,
            role=m.role, permission="core.delegate_approval", details=_membership_details(m),
        )
    return m


# --- dual control ------------------------------------------------------------

def payload_hash(payload):
    data = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _require_dc_holder(user, entity, permission):
    perm = catalogue.get(permission)
    if not perm.is_dual_control:
        raise AccessUsageError(f"{permission} is not a dual-control permission.")
    memberships, reason = _evaluate(user, entity, permission, now())
    if reason:
        _deny(user, entity, permission, reason)
    return memberships


def request_dual_control(user, entity, permission, target, payload):
    """Open a four-eyes request for a DC action on ``target`` with exactly ``payload`` (R19)."""
    _require_dc_holder(user, entity, permission)
    at = now()
    with transaction.atomic():
        req = DualControlRequest.objects.create(
            entity=entity, permission=permission, target_type=ContentType.objects.get_for_model(target),
            target_id=target.pk, payload_hash=payload_hash(payload), requested_by=user, requested_at=at,
            expires_at=at + DUAL_CONTROL_TTL,
        )
        log(
            AccessLog.Event.DUAL_CONTROL_REQUESTED, actor=user, entity=entity, permission=permission,
            details={"request": req.pk, "target": [req.target_type.model, req.target_id], "payload_hash": req.payload_hash},
        )
    return req


def decide_dual_control(user, request, approve, note=""):
    """Approve or reject a pending request; the decider must differ from the requester (R15, R19)."""
    entity, permission = request.entity, request.permission
    _require_dc_holder(user, entity, permission)
    if request.requested_by_id == user.pk:
        _deny(user, entity, permission, "self_approval")
    at = now()
    if request.expires_at <= at:
        DualControlRequest.objects.filter(pk=request.pk, status=DualControlRequest.Status.PENDING).update(
            status=DualControlRequest.Status.EXPIRED
        )
        _deny(user, entity, permission, "dual_control_expired")
    status = DualControlRequest.Status.APPROVED if approve else DualControlRequest.Status.REJECTED
    with transaction.atomic():
        updated = DualControlRequest.objects.filter(pk=request.pk, status=DualControlRequest.Status.PENDING).update(
            status=status, decided_by=user, decided_at=at, decision_note=note
        )
        if not updated:
            _deny(user, entity, permission, "dual_control")
        log(
            AccessLog.Event.DUAL_CONTROL_DECIDED, actor=user, entity=entity, permission=permission,
            details={"request": request.pk, "status": status, "note": note},
        )
    request.refresh_from_db()
    return request


def require_dual_control(user, entity, permission, request, payload, target=None):
    """Use an approved four-eyes request once (R19). Returns a Grant; the request becomes ``used``."""
    memberships = _require_dc_holder(user, entity, permission)
    request = DualControlRequest.objects.get(pk=request.pk)
    if (
        request.entity_id != entity.pk
        or request.permission != permission
        or request.payload_hash != payload_hash(payload)
        or request.decided_by_id in (None, request.requested_by_id)
        or (target is not None and (
            request.target_type_id != ContentType.objects.get_for_model(target).pk or request.target_id != target.pk
        ))
    ):
        _deny(user, entity, permission, "dual_control")
    at = now()
    if request.expires_at <= at:
        DualControlRequest.objects.filter(pk=request.pk, status=DualControlRequest.Status.APPROVED).update(
            status=DualControlRequest.Status.EXPIRED
        )
        _deny(user, entity, permission, "dual_control_expired")
    with transaction.atomic():
        used = DualControlRequest.objects.filter(pk=request.pk, status=DualControlRequest.Status.APPROVED).update(
            status=DualControlRequest.Status.USED
        )
        if not used:
            _deny(user, entity, permission, "dual_control")
        entry = log(
            AccessLog.Event.DUAL_CONTROL_USED, actor=user, entity=entity, permission=permission,
            details={"request": request.pk, "payload_hash": request.payload_hash},
        )
    return Grant(
        user=user, entity=entity, permission=permission, membership_ids=tuple(m.pk for m in memberships),
        checked_at=at, log_id=entry.pk,
    )


# --- service users and bootstrap --------------------------------------------------

def system_user(code):
    """The system user for ``code`` (R20). Raises if it does not exist."""
    profile = UserProfile.objects.select_related("user").get(kind=Kind.SYSTEM, system_code=code)
    return profile.user


@transaction.atomic
def bootstrap_managing_partner(user, entity, reason, os_user):
    """Grant the first managing_partner membership (R26). Refused once any active one exists."""
    role = Role.objects.get(code="managing_partner")
    if Membership.objects.active(now()).filter(role=role).exists():
        raise ValidationError(
            "A managing partner membership already exists; use grant_membership. / "
            "توجد عضوية للشريك المدير بالفعل؛ استخدم منح العضوية."
        )
    m = Membership(user=user, entity=entity, role=role, valid_from=now(), reason=reason, granted_at=now())
    m._bootstrap = True
    m.save()
    log(
        AccessLog.Event.BOOTSTRAP, actor=None, entity=entity, subject_user=user, membership=m, role=role,
        details={"os_user": os_user, **_membership_details(m)}, source="bootstrap",
    )
    return m
