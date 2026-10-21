"""Delegation of approval authority and personal-assistant assignments (access.md 3.9, §10; R62, R81, R87, R88).

- An ``ApprovalDelegation`` lets a delegate decide what the delegator may decide in one entity, for optional
  activities and up to ``amount_max``, between ``valid_from`` and a required ``valid_to`` at most 90 days later
  (OQ-45). It ends at ``valid_to`` without any job (eligibility is checked at decision time), when revoked, or when
  the delegator loses the right. A renewal is a new, logged delegation. Never for dual-control, S! or G rights,
  never to a personal assistant, analyst or auditor only, never to a system or agent user (R88).
- An ``AssistantAssignment`` names the principal a ``personal_assistant`` drafts for; the principal then counts as
  a creator of those drafts (R81).
"""
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from django.utils import timezone

from core.models import Entity

from . import authority_defaults, catalogue, engine
from . import services as access
from .authority_models import AccessAlert, ApprovalActivity, ApprovalDelegation, AssistantAssignment, active_window_q
from .models import AccessLog, Kind, Membership

DELEGATE = "core.delegate_approval"
ASSIGN = "core.manage_assistant_assignment"
MAX_DAYS = engine.DELEGATION_MAX_DAYS
REMINDER_DAYS = engine.DELEGATION_REMINDER_DAYS

MESSAGES = {
    "delegation_too_long": (
        "A delegation lasts at most {days} days; renew it as a new delegation (OQ-45).",
        "لا يتجاوز التفويض {days} يومًا؛ يُجدَّد بتفويض جديد.",
    ),
    "delegate_not_eligible": (
        "{who} cannot be a delegate: a delegate is a person with their own working membership in {entity}, not only "
        "an assistant, analyst or auditor, and never a system or agent user (R88).",
        "لا يمكن أن يكون {who} مفوَّضًا: المفوَّض شخص له عضوية عمل في {entity}، وليس مساعدًا أو محللًا أو مدققًا فقط، "
        "ولا مستخدم نظام أو وكيلًا.",
    ),
    "activity_not_delegable": (
        "{activity} is a dual-control, segregated or group-level right and is never delegated (R88).",
        "{activity} صلاحية رقابة مزدوجة أو فصل مهام أو على مستوى المجموعة ولا تُفوَّض أبدًا.",
    ),
    "delegator_or_top_authority": (
        "A delegation is created by the delegator or by the entity's top authority (R62).",
        "ينشئ التفويضَ المفوِّضُ نفسه أو السلطة العليا للكيان.",
    ),
    "not_an_assistant": (
        "{who} holds no active personal assistant membership in {entity}.",
        "لا يملك {who} عضوية مساعد شخصي نشطة في {entity}.",
    ),
    "principal_or_top_authority": (
        "An assistant is assigned by the principal or by the entity's top authority.",
        "يعيّن المساعدَ صاحبُ الصلاحية نفسه أو السلطة العليا للكيان.",
    ),
}


def _refuse(code, user, entity, permission, **values):
    en, ar = MESSAGES[code]
    access.log_denial(actor=user, entity=entity, permission=permission, details={"reason": code})
    raise access.AccessDenied(code, en.format(entity=entity.code, **values), ar.format(entity=entity.code, **values),
                              access._language(user))


def _save(obj):
    obj._service_write = True
    obj.save()
    return obj


def _direct_roles(user, entity, permission, at):
    return {m.role.code for m in access._memberships_for(user, entity, permission, at) if m.entity_id == entity.pk}


def _is_top_authority(user, entity, at):
    return Membership.objects.active(at).filter(user=user, entity=entity,
                                                role__code=engine.governance_of(entity).top_authority_role).exists()


def _check_window(actor, entity, valid_from, valid_to, at):
    if valid_to is None:
        raise ValidationError("A delegation needs an end date. / يجب تحديد تاريخ انتهاء التفويض.",
                              code="valid_to_required")
    valid_from = valid_from or at
    if valid_from < at - access.DELEGATION_START_TOLERANCE:
        access.deny(actor, entity, DELEGATE, "delegation_dates")
    valid_from = max(valid_from, at)
    if valid_to <= valid_from:
        raise ValidationError("A delegation ends after it starts. / ينتهي التفويض بعد بدايته.")
    if valid_to - valid_from > timedelta(days=MAX_DAYS):
        _refuse("delegation_too_long", actor, entity, DELEGATE, days=MAX_DAYS)
    return valid_from


@transaction.atomic
def delegate_authority(actor, *, entity, delegate, valid_to, reason_en, reason_ar, delegator=None, valid_from=None,
                       activities=(), amount_max=None):
    """Create an ``ApprovalDelegation`` (R62, R87, R88). ``delegator`` defaults to ``actor``; the entity's top
    authority may create one for another delegator."""
    entity = Entity.objects.get(pk=entity.pk)
    delegator = delegator or actor
    at = access.now()
    access.require(actor, entity, DELEGATE)
    if not _direct_roles(actor, entity, DELEGATE, at):  # R90: never through the group parent
        access.deny(actor, entity, DELEGATE, "no_membership")
    if delegator.pk != actor.pk and not _is_top_authority(actor, entity, at):
        _refuse("delegator_or_top_authority", actor, entity, DELEGATE)
    if delegator.pk != actor.pk and not _direct_roles(delegator, entity, DELEGATE, at):
        access.deny(actor, entity, DELEGATE, "delegate_cannot_delegate")
    if delegate.pk in (delegator.pk, actor.pk):
        access.deny(actor, entity, DELEGATE, "self_grant")
    if not (reason_en or "").strip() or not (reason_ar or "").strip():
        raise ValidationError("A reason in English and Arabic is required. / السبب بالإنجليزية والعربية مطلوب.")
    valid_from = _check_window(actor, entity, valid_from, valid_to, at)
    own = list(Membership.objects.active(at).filter(user=delegate, entity=entity).select_related("role"))
    working = {m.role.code for m in own if m.role.kind == Kind.HUMAN} - engine.NON_DELEGATE_ROLES
    if access.kind_of(delegate) != Kind.HUMAN or not working:
        _refuse("delegate_not_eligible", actor, entity, DELEGATE, who=delegate.get_username())
    acts = [a if isinstance(a, ApprovalActivity) else engine.activity_row(a) for a in activities]
    for act in acts:
        perms = [p for p in authority_defaults.get(act.code).permissions if p in catalogue.CATALOGUE]
        if any(catalogue.get(p).is_dual_control or catalogue.get(p).is_segregated or catalogue.get(p).is_group_only
               for p in perms):
            _refuse("activity_not_delegable", actor, entity, DELEGATE, activity=act.code)
    d = _save(ApprovalDelegation(
        entity=entity, delegator=delegator, delegate=delegate, amount_max=amount_max, valid_from=valid_from,
        valid_to=valid_to, reason_en=reason_en, reason_ar=reason_ar, created_by=actor, created_at=at))
    if acts:
        d.activities.set(acts)
    access.log(AccessLog.Event.DELEGATION_GRANTED, actor=actor, entity=entity, subject_user=delegate,
               permission=DELEGATE,
               details={"approval_delegation": d.pk, "delegator": delegator.pk, "delegate": delegate.pk,
                        "activities": [a.code for a in acts], "amount_max": None if amount_max is None else str(amount_max),
                        "valid_from": valid_from.isoformat(), "valid_to": valid_to.isoformat(),
                        "reason_en": reason_en, "reason_ar": reason_ar})
    return d


@transaction.atomic
def revoke_delegation(actor, delegation, reason):
    """End a delegation now: by its delegator or the entity's top authority (one write, logged)."""
    d = ApprovalDelegation.objects.select_for_update().get(pk=delegation.pk)
    entity, at = d.entity, access.now()
    access.require(actor, entity, DELEGATE)
    if actor.pk != d.delegator_id and not _is_top_authority(actor, entity, at):
        _refuse("delegator_or_top_authority", actor, entity, DELEGATE)
    if not (reason or "").strip():
        raise ValidationError("A reason is required. / السبب مطلوب.")
    if d.revoked_at is not None:
        raise ValidationError("This delegation is already revoked. / هذا التفويض ملغى بالفعل.")
    d.revoked_at, d.revoked_by, d.revoke_reason = at, actor, reason[:500]
    _save(d)
    access.log(AccessLog.Event.DELEGATION_REVOKED, actor=actor, entity=entity, subject_user=d.delegate,
               permission=DELEGATE, details={"approval_delegation": d.pk, "reason": reason})
    return d


def send_delegation_reminders(at=None):
    """Job (daily): a bilingual reminder to the delegator and the delegate 3 days before a delegation ends (R87);
    written once per delegation (``notified_expiry_at``)."""
    at = at or access.now()
    due = ApprovalDelegation.objects.filter(active_window_q(at), notified_expiry_at__isnull=True,
                                            valid_to__lte=at + timedelta(days=REMINDER_DAYS))
    sent = []
    for d in due.select_related("entity"):
        end = timezone.localtime(d.valid_to).date()
        for uid in (d.delegator_id, d.delegate_id):
            engine.alert(d.entity, uid, AccessAlert.Kind.DELEGATION_EXPIRING,
                         f"{d.entity.code}: the approval delegation {d.pk} from {d.delegator} to {d.delegate} ends on "
                         f"{end}. Renew it as a new delegation if it is still needed (R87).",
                         f"{d.entity.code}: ينتهي تفويض الاعتماد رقم {d.pk} من {d.delegator} إلى {d.delegate} في {end}. "
                         "جدّده بتفويض جديد إن كان لا يزال لازمًا.",
                         key=f"delegation_expiring:{d.pk}:{uid}", details={"approval_delegation": d.pk})
        d.notified_expiry_at = at
        _save(d)
        sent.append(d)
    return sent


def delegations(user, entity=None):
    """Delegations of the entities where ``user`` holds a §10 right through their own membership (R90)."""
    from .matrix import entities_reading

    ids = set(entities_reading(user).values_list("pk", flat=True))
    ids |= {e.pk for e in access.entities_for(user, DELEGATE) if _direct_roles(user, e, DELEGATE, access.now())}
    qs = ApprovalDelegation.objects.filter(entity_id__in=ids)
    return qs.filter(entity=entity) if entity is not None else qs


def get_delegation(user, pk):
    try:
        return delegations(user).get(pk=pk)
    except ApprovalDelegation.DoesNotExist:
        raise Http404("Not found / غير موجود") from None


# --- personal assistants (R81) -------------------------------------------------------------------------------------

@transaction.atomic
def assign_assistant(actor, *, entity, assistant, principal, valid_to, reason, valid_from=None):
    """Assign a personal assistant to a principal in ``entity``: by the principal, or by the entity's top authority
    (10.3, S!: never to oneself)."""
    entity = Entity.objects.get(pk=entity.pk)
    at = access.now()
    access.require(actor, entity, ASSIGN)
    if not _direct_roles(actor, entity, ASSIGN, at):
        access.deny(actor, entity, ASSIGN, "no_membership")
    if actor.pk != principal.pk and not _is_top_authority(actor, entity, at):
        _refuse("principal_or_top_authority", actor, entity, ASSIGN)
    if assistant.pk in (actor.pk, principal.pk):
        access.deny(actor, entity, ASSIGN, "self_grant")
    if not Membership.objects.active(at).filter(user=assistant, entity=entity, role__code="personal_assistant").exists():
        _refuse("not_an_assistant", actor, entity, ASSIGN, who=assistant.get_username())
    if not Membership.objects.active(at).filter(user=principal, entity=entity, role__kind=Kind.HUMAN).exists():
        access.deny(actor, entity, ASSIGN, "no_membership")
    if not (reason or "").strip():
        raise ValidationError("A reason is required. / السبب مطلوب.")
    valid_from = max(valid_from or at, at)
    if valid_to is None or valid_to <= valid_from:
        raise ValidationError("An assignment needs an end date after its start. / يحتاج التعيين تاريخ انتهاء بعد بدايته.")
    a = _save(AssistantAssignment(entity=entity, assistant=assistant, principal=principal, valid_from=valid_from,
                                  valid_to=valid_to, reason=reason, created_by=actor, created_at=at))
    access.log(AccessLog.Event.ASSISTANT_ASSIGNED, actor=actor, entity=entity, subject_user=assistant, permission=ASSIGN,
               details={"assignment": a.pk, "assistant": assistant.pk, "principal": principal.pk,
                        "valid_from": valid_from.isoformat(), "valid_to": valid_to.isoformat(), "reason": reason})
    return a


@transaction.atomic
def end_assistant_assignment(actor, assignment, reason):
    a = AssistantAssignment.objects.select_for_update().get(pk=assignment.pk)
    entity, at = a.entity, access.now()
    access.require(actor, entity, ASSIGN)
    if actor.pk != a.principal_id and not _is_top_authority(actor, entity, at):
        _refuse("principal_or_top_authority", actor, entity, ASSIGN)
    if not (reason or "").strip():
        raise ValidationError("A reason is required. / السبب مطلوب.")
    if a.revoked_at is not None:
        raise ValidationError("This assignment has already ended. / انتهى هذا التعيين بالفعل.")
    a.revoked_at, a.revoked_by, a.revoke_reason = at, actor, reason[:500]
    _save(a)
    access.log(AccessLog.Event.ASSISTANT_REVOKED, actor=actor, entity=entity, subject_user=a.assistant,
               permission=ASSIGN, details={"assignment": a.pk, "reason": reason})
    return a


def assistant_assignments(user, entity=None):
    from .matrix import entities_reading

    ids = set(entities_reading(user).values_list("pk", flat=True))
    ids |= set(access.entities_for(user, ASSIGN).values_list("pk", flat=True))
    qs = AssistantAssignment.objects.filter(entity_id__in=ids)
    return qs.filter(entity=entity) if entity is not None else qs
