"""Access services (access.md 3.7): the one way every module checks rights.

Nothing here is cached: every call reads memberships again (R4).
"""
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied, ValidationError
from django.conf import settings
from django.db import DatabaseError, IntegrityError, connections, router, transaction
from django.db.models import Q
from django.http import Http404
from django.utils import timezone

from core.models import Entity

from . import catalogue
from .models import AccessLog, DualControlRequest, GrantUse, Kind, Membership, Role, UserProfile

DUAL_CONTROL_TTL = timedelta(hours=72)  # OQ-11 default
# How long a grant may be used after require() issued it (R31). Setting ACCESS_GRANT_MAX_AGE (timedelta or seconds).
GRANT_MAX_AGE = timedelta(minutes=15)
GRANT_EVENTS = ("permission_granted", "dual_control_used")
DELEGATION_START_TOLERANCE = timedelta(minutes=1)

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
    "grant_needs_draft": (
        "A manual journal is posted with post_entry after it is drafted, by someone other than its creator.",
        "يُرحَّل القيد اليدوي بعد إنشائه كمسودة، ومن شخص غير منشئه.",
    ),
    "dual_control": (
        "This action needs an approved, unused four-eyes request for exactly this change.",
        "يتطلب هذا الإجراء طلب رقابة مزدوجة معتمدًا وغير مستخدم لهذا التغيير بعينه.",
    ),
    "dual_control_expired": ("The four-eyes request has expired.", "انتهت صلاحية طلب الرقابة المزدوجة."),
    "grant_forged": (
        "This authorisation does not match the access log; ask for a new one.",
        "هذا التفويض لا يطابق سجل الوصول؛ اطلب تفويضًا جديدًا.",
    ),
    "grant_expired": (
        "This authorisation has expired; ask for a new one.",
        "انتهت صلاحية هذا التفويض؛ اطلب تفويضًا جديدًا.",
    ),
    "grant_used": (
        "This authorisation has already been used; each one is used once.",
        "استُخدم هذا التفويض من قبل؛ يُستخدم كل تفويض مرة واحدة.",
    ),
    "grant_needs_document": (
        "A posting authorisation is given for one document; this one names none or another.",
        "يُمنح تفويض الترحيل لمستند واحد؛ وهذا التفويض لا يحدد مستندًا أو يحدد مستندًا آخر.",
    ),
    "grant_document_changed": (
        "The document changed after this authorisation was given; it must be approved again.",
        "تغيّر المستند بعد صدور هذا التفويض؛ يجب اعتماده من جديد.",
    ),
    "posted_by_not_approver": (
        "The person recorded as poster must be the approver recorded for this document.",
        "يجب أن يكون المرحِّل المسجَّل هو المعتمد المسجَّل لهذا المستند.",
    ),
    "document_not_registered": (
        "This kind of document cannot post to the ledger yet; its module has not registered it.",
        "لا يمكن لهذا النوع من المستندات الترحيل إلى دفتر الأستاذ بعد؛ لم تسجله الوحدة المختصة.",
    ),
    "document_not_found": (
        "The document this authorisation names does not exist.",
        "المستند الذي يحدده هذا التفويض غير موجود.",
    ),
    "document_other_entity": (
        "The document belongs to another entity.",
        "المستند يخص كيانًا آخر.",
    ),
    "delegation_dates": (
        "A delegation starts now or later, and not before the delegator's own membership.",
        "يبدأ التفويض الآن أو لاحقًا، ولا يبدأ قبل عضوية المفوِّض نفسه.",
    ),
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
    """``(app_label.model, pk)`` of a document object; None for no document or an unsaved one.

    A bare reference (tuple or list) is a programming error: a grant is issued
    for a document the caller holds, never for a name it made up (R30).
    """
    if isinstance(document, (tuple, list, str)):
        raise AccessUsageError("Pass the document itself, not a reference to it (R30).")
    if document is None or getattr(document, "pk", None) is None:
        return None
    return (document._meta.label_lower, document.pk)


def _user_id(value):
    if value is None:
        return None
    return getattr(value, "pk", value)


def _document_people(document):
    """Users who may not approve ``document``: its creator, submitter and last modifier (R14)."""
    people = set()
    if document is None:
        return people
    for attr in ("created_by", "submitted_by", "modified_by"):
        uid = getattr(document, f"{attr}_id", None)
        if uid is None:
            uid = _user_id(getattr(document, attr, None))
        if uid is not None:
            people.add(uid)
    return people


def log(event, *, actor=None, entity=None, subject_user=None, membership=None, role=None, permission="", details=None, source=None, using=None, at=None):
    """Write one AccessLog row (R27). Never pass passwords, tokens, keys or OTPs in ``details``.

    The row is written in the caller's transaction (``using`` picks another
    connection; only :func:`log_denial` does that).
    """
    actor = actor if getattr(actor, "pk", None) else None
    return AccessLog.objects.using(using or router.db_for_write(AccessLog)).create(
        at=at or now(),
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


def log_denial(*, actor, entity, permission, details):
    """Write a ``permission_denied`` row that survives the caller's rollback (R27, AT-1).

    A refusal raises AccessDenied, which usually rolls back the caller's
    transaction (ATOMIC_REQUESTS, service ``atomic`` blocks), and a row
    written there would be lost. When the main connection is inside a
    transaction, the row is written through the separate ``ACCESS_LOG_DB``
    connection in its own short transaction instead. If that connection
    cannot write it (for example the actor or entity were created in the
    still-open transaction and are not visible to it, or a lock times out),
    the row is written in the caller's transaction as before.
    """
    fields = dict(actor=actor, entity=entity, permission=permission, details=details)
    main = router.db_for_write(AccessLog)
    alias = getattr(settings, "ACCESS_LOG_DB", None)
    if alias and alias in settings.DATABASES and alias != main and connections[main].in_atomic_block:
        try:
            with transaction.atomic(using=alias):
                return log(AccessLog.Event.PERMISSION_DENIED, using=alias, **fields)
        except DatabaseError:
            pass
    return log(AccessLog.Event.PERMISSION_DENIED, **fields)


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
    log_denial(actor=user, entity=entity, permission=permission, details={"reason": code})
    raise AccessDenied(code, en.format(**values), ar.format(**values), _language(user))


def deny(user, entity, permission, code, **fmt):
    """Log a ``permission_denied`` row and raise AccessDenied with a catalogue message (R27, R28)."""
    _deny(user, entity, permission, code, **fmt)


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
    if perm.posts and _document_ref(document) is None:
        raise AccessUsageError(f"{permission} authorises a posting for one document; pass the document (R30).")
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
            at=at,
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


def _grant_max_age():
    value = getattr(settings, "ACCESS_GRANT_MAX_AGE", None)
    if value is None:
        return GRANT_MAX_AGE
    return value if isinstance(value, timedelta) else timedelta(seconds=value)


def _grant_row_matches(grant, row):
    """R31: the AccessLog row written when the grant was issued says exactly what the Grant says."""
    if row is None or row.event not in GRANT_EVENTS:
        return False
    if (row.actor_id, row.entity_id, row.permission) != (_user_id(grant.user), getattr(grant.entity, "pk", None), grant.permission):
        return False
    if row.at != grant.checked_at:
        return False
    if row.event == AccessLog.Event.PERMISSION_GRANTED:
        details = row.details or {}
        expected = list(grant.document_ref) if grant.document_ref else None
        if details.get("document") != expected or bool(details.get("self_approval")) != bool(grant.self_approval):
            return False
        if sorted(details.get("membership_ids") or []) != sorted(grant.membership_ids or ()):
            return False
    elif grant.document_ref is not None:
        return False
    return True


def verify_grant(grant, entity, allowed_permissions, *, document=None, allow_self_approval=False, use=True, target=None):
    """Check a Grant passed to a service before honouring it (R4, R23, R30, R31).

    The Grant must match the ``permission_granted`` AccessLog row it was
    issued with (user, entity, permission, document, time), be younger than
    ``ACCESS_GRANT_MAX_AGE`` (default 15 minutes), not be used yet, and the
    user must still hold the right now. With ``document`` (required for A and
    P permissions), the grant must have been issued for that document and
    segregation (R14) is checked again; a self-approval grant is accepted only
    when the calling service passes ``allow_self_approval=True``.

    With ``use=True`` (default) the grant is marked used here (single use,
    R30); a service that wants the use to roll back with its own writes
    passes ``use=False`` and calls :func:`use_grant` inside its transaction.
    Returns the user the grant belongs to. Refusals are logged and raise AccessDenied.
    """
    if not isinstance(grant, Grant):
        _deny(None, entity, "", "grant_missing")
    user = grant.user
    actor = user if getattr(user, "pk", None) else None
    if getattr(grant.entity, "pk", None) != entity.pk:
        _deny(actor, entity, grant.permission, "grant_other_entity")
    if grant.permission not in allowed_permissions:
        _deny(actor, entity, grant.permission, "grant_wrong_permission")
    perm = catalogue.get(grant.permission)
    if (perm.posts or perm.is_approval) and document is None:
        raise AccessUsageError(f"{grant.permission}: pass the document the grant is used for (R30).")
    row = AccessLog.objects.filter(pk=grant.log_id).first() if grant.log_id else None
    if not _grant_row_matches(grant, row):
        _deny(actor, entity, grant.permission, "grant_forged")
    if now() - row.at > _grant_max_age():
        _deny(actor, entity, grant.permission, "grant_expired")
    if GrantUse.objects.filter(log_id=row.pk).exists():
        _deny(actor, entity, grant.permission, "grant_used")
    _, reason = _evaluate(user, entity, grant.permission, now())
    if reason:
        _deny(actor, entity, grant.permission, reason)
    if document is not None:
        if grant.document_ref is None or grant.document_ref != _document_ref(document):
            _deny(actor, entity, grant.permission, "grant_wrong_document")
        if (perm.is_approval or perm.is_segregated) and user.pk in _document_people(document) and not (
            allow_self_approval and grant.self_approval and not perm.is_segregated
        ):
            _deny(actor, entity, grant.permission, "self_approval")
    if use:
        use_grant(grant, target=target if target is not None else document)
    return user


def use_grant(grant, *, target=None):
    """Record the single use of ``grant`` (R30). A second use is refused, also under a race.

    Call after :func:`verify_grant` (``use=False``), inside the transaction
    that does the work, so the use rolls back with it.
    """
    ref = _document_ref(target)
    try:
        with transaction.atomic():
            return GrantUse.objects.create(
                log_id=grant.log_id, used_at=now(), used_by=grant.user, entity=grant.entity,
                target_type=ref[0] if ref else "", target_id=ref[1] if ref else None,
            )
    except IntegrityError:
        _deny(grant.user, grant.entity, grant.permission, "grant_used")


def recorded_approvers(entity, permission, document, *, used=False, since=None):
    """Users recorded in the access log as having been granted ``permission`` for ``document`` in ``entity``.

    With ``used`` only grants that were actually used (a GrantUse row) count;
    with ``since`` only grants issued at or after that moment (e.g. the
    document's last change, so a stale approval does not count).
    """
    expected = list(_document_ref(document) or ())
    if not expected:
        return set()
    rows = AccessLog.objects.filter(event=AccessLog.Event.PERMISSION_GRANTED, entity=entity, permission=permission)
    if used:
        rows = rows.filter(grant_use__isnull=False)
    if since is not None:
        rows = rows.filter(at__gte=since)
    return {r.actor_id for r in rows.only("actor_id", "details") if (r.details or {}).get("document") == expected}


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
    ``valid_from`` (None = now) is never in the past and never before the
    source membership starts: a delegation cannot cover approvals already made.
    """
    if valid_to is None:
        raise ValidationError("A delegation needs an end date. / يجب تحديد تاريخ انتهاء التفويض.")
    grant = require(actor, entity, "core.delegate_approval")
    at = now()
    valid_from = valid_from or at
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
    # A caller's "now" computed a moment earlier is accepted (one minute) and moved to now.
    if valid_from < at - DELEGATION_START_TOLERANCE or valid_from < source.valid_from:
        _deny(actor, entity, "core.delegate_approval", "delegation_dates")
    valid_from = max(valid_from, at)
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


def _check_target(target):
    if target is None or getattr(target, "pk", None) is None:
        raise AccessUsageError("A dual-control action names the object it changes (target) (R19).")


def request_dual_control(user, entity, permission, target, payload):
    """Open a four-eyes request for a DC action on ``target`` with exactly ``payload`` (R19)."""
    _check_target(target)
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
        DualControlRequest.objects.filter(pk=request.pk, status=DualControlRequest.Status.PENDING)._transition(
            status=DualControlRequest.Status.EXPIRED
        )
        _deny(user, entity, permission, "dual_control_expired")
    status = DualControlRequest.Status.APPROVED if approve else DualControlRequest.Status.REJECTED
    with transaction.atomic():
        updated = DualControlRequest.objects.filter(pk=request.pk, status=DualControlRequest.Status.PENDING)._transition(
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


def require_dual_control(user, entity, permission, request, payload, target):
    """Use an approved four-eyes request once, for the same ``target`` it was requested for (R19).

    Returns a Grant; the request becomes ``used``.
    """
    _check_target(target)
    memberships = _require_dc_holder(user, entity, permission)
    request = DualControlRequest.objects.get(pk=request.pk)
    if (
        request.entity_id != entity.pk
        or request.permission != permission
        or request.payload_hash != payload_hash(payload)
        or request.decided_by_id in (None, request.requested_by_id)
        or request.target_type_id != ContentType.objects.get_for_model(target).pk
        or request.target_id != target.pk
    ):
        _deny(user, entity, permission, "dual_control")
    at = now()
    if request.expires_at <= at:
        DualControlRequest.objects.filter(pk=request.pk, status=DualControlRequest.Status.APPROVED)._transition(
            status=DualControlRequest.Status.EXPIRED
        )
        _deny(user, entity, permission, "dual_control_expired")
    with transaction.atomic():
        used = DualControlRequest.objects.filter(pk=request.pk, status=DualControlRequest.Status.APPROVED)._transition(
            status=DualControlRequest.Status.USED
        )
        if not used:
            _deny(user, entity, permission, "dual_control")
        entry = log(
            AccessLog.Event.DUAL_CONTROL_USED, actor=user, entity=entity, permission=permission, at=at,
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
    """Grant the first managing_partner membership (R26). Refused once any active one exists.

    The role row is locked first, so two bootstraps at the same moment run one
    after the other and the second sees the first's membership.
    """
    role = Role.objects.select_for_update().get(code="managing_partner")
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


# --- access log visibility and user activation ---------------------------------------

def visible_access_log(user):
    """AccessLog rows ``user`` may read (OQ-10 default): rows of entities where they hold ``core.view_access``,
    plus rows without an entity (flag changes, user activation) when they hold it on a group parent entity."""
    entities = entities_for(user, "core.view_access")
    q = Q(entity__in=entities)
    if entities.filter(consolidation=Entity.Consolidation.PARENT, parent__isnull=True).exists():
        q |= Q(entity__isnull=True)
    return AccessLog.objects.filter(q)


def group_parent_of(entity):
    """The top of ``entity``'s parent chain (the group parent entity, R6)."""
    seen = {entity.pk}
    current = entity
    while current.parent_id and current.parent_id not in seen:
        current = current.parent
        seen.add(current.pk)
    return current


def set_user_active(actor, user, active, reason):
    """Activate or deactivate a user account (R32 for users): ``core.manage_users`` on a group parent, logged.

    Deactivating ends every right of the user at once (3.3). Nobody changes their own account.
    """
    if not reason:
        raise ValidationError("A reason is required. / السبب مطلوب.")
    parents = Entity.objects.filter(consolidation=Entity.Consolidation.PARENT, parent__isnull=True).order_by("pk")
    parent = next((p for p in parents if has(actor, p, "core.manage_users")), None) or parents.first()
    if parent is None:
        raise ValidationError("No group parent entity exists. / لا يوجد كيان أم للمجموعة.")
    require(actor, parent, "core.manage_users")
    if _user_id(user) == _user_id(actor):
        _deny(actor, parent, "core.manage_users", "self_grant")
    User = type(user)
    with transaction.atomic():
        stored = User.objects.select_for_update().get(pk=user.pk)
        before = stored.is_active
        if before != bool(active):
            stored.is_active = bool(active)
            stored._access_actor = actor
            stored.save(update_fields=["is_active"])
            log(
                AccessLog.Event.USER_ACTIVE_CHANGED, actor=actor, subject_user=stored, permission="core.manage_users",
                details={"field": "is_active", "before": before, "after": bool(active), "reason": reason},
            )
    user.is_active = stored.is_active
    return stored
