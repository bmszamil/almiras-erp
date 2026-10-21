"""Authority matrix versions under dual control (access.md §10; R69 to R78, R90; OQ-42, OQ-43, OQ-46 defaults).

An entity's matrix rows change only through a new ``AuthorityMatrixVersion``:

1. a holder of ``core.propose_authority_matrix`` in the entity, through the entity's top-authority role or
   ``access_admin`` (OQ-43: not the CFO), drafts it (:func:`draft_version`) and adds or removes rows;
2. submitting it (:func:`submit_version`) freezes the rows (``rows_hash``) and opens a ``DualControlRequest`` for
   ``core.approve_authority_matrix`` whose ``payload_hash`` is the rows hash;
3. a **different** person decides it (:func:`decide_version`): the entity's top authority when someone else
   proposed it; when the top authority proposed it, the group's ``chairman`` in the group parent or a
   ``board_representative`` of the entity elsewhere (R76, OQ-42). Self-decision is refused (S!, R15);
4. an approved version takes effect on its ``effective_from``, never before the approval date (R77); the previous
   version is then ``superseded``. Requests already open keep the rules they were opened with (OQ-46).

Every step writes an ``authority_matrix_*`` AccessLog row with the rows (diff) and the request id (R78). The same
four-eyes rule applies to changes of the entity's top authority and self-approval switch (R76; applied through
``core.services.change_entity_settings``).
"""
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from django.utils import timezone

from core.models import Currency, Entity

from . import catalogue, engine
from . import services as access
from .authority_models import (
    ApprovalActivity, AuthorityMatrixVersion, AuthorityRule, EntityGovernance, canonical_hash,
)
from .models import AccessLog, DualControlRequest, Kind, Membership, Role

PROPOSE = "core.propose_authority_matrix"
APPROVE = "core.approve_authority_matrix"
# A matrix or top-authority change may wait for a board representative or the chairman: its four-eyes request stays
# open 30 days (the 72 h of OQ-11 is for operational switches such as ZATCA production). Build decision, as built.
MATRIX_DECISION_TTL = timedelta(days=30)
Status = AuthorityMatrixVersion.Status

MESSAGES = {
    "not_member_of_entity": (
        "{who} has no active membership in {entity}; a matrix row names only people of that entity (R69).",
        "لا يملك {who} عضوية نشطة في {entity}؛ لا يسمي سطر المصفوفة إلا أشخاصًا من هذا الكيان.",
    ),
    "role_not_for_entity": (
        "The role {role} cannot be held in {entity}, so it cannot approve there (R69).",
        "لا يمكن شغل الدور {role} في {entity}، فلا يعتمد فيه.",
    ),
    "one_approver_kind": (
        "A matrix row names either approver roles or one named user, not both and not neither (R72).",
        "يسمي سطر المصفوفة إما أدوار المعتمدين أو مستخدمًا واحدًا بعينه، لا الاثنين معًا ولا أيًّا منهما.",
    ),
    "band_overlap": (
        "This band overlaps another band of {activity} in {currency} in this version (R71).",
        "يتداخل هذا النطاق مع نطاق آخر لنشاط {activity} بعملة {currency} في هذا الإصدار.",
    ),
    "amount_decimals": (
        "{amount} has more decimals than {currency} allows ({places}).",
        "المبلغ {amount} يتجاوز عدد الخانات العشرية المسموح بها للعملة {currency} ({places}).",
    ),
    "not_draft": (
        "Only a draft matrix version changes; draft a new version (R77).",
        "لا يتغير إلا إصدار المصفوفة المسودة؛ أعدّ إصدارًا جديدًا.",
    ),
    "effective_from_backdated": (
        "A matrix version takes effect on or after the day it is approved, never earlier ({date}) (R77).",
        "يسري إصدار المصفوفة في يوم اعتماده أو بعده، لا قبله ({date}).",
    ),
    "proposer_role": (
        "Matrix versions are drafted by the entity's top authority or its access administrator (OQ-43).",
        "يُعدّ إصدارات المصفوفة السلطة العليا للكيان أو مسؤول الصلاحيات فيه.",
    ),
    "decider_role": (
        "This change is decided by {role} of {entity} (R76).",
        "يقرر هذا التغيير {role_ar} في {entity}.",
    ),
    "rows_changed": (
        "The rows of this version no longer match what was submitted.", "أسطر هذا الإصدار لم تعد تطابق ما قُدّم.",
    ),
}


def _refuse(code, user, entity, permission, **values):
    en, ar = MESSAGES[code]
    access.log_denial(actor=user if getattr(user, "pk", None) else None, entity=entity, permission=permission,
                      details={"reason": code})
    raise access.AccessDenied(code, en.format(entity=entity.code, **values), ar.format(entity=entity.code, **values),
                              access._language(user))


def _direct(user, entity, permission):
    """Memberships of ``user`` giving ``permission`` in ``entity`` itself (never through the group parent, R90)."""
    return [m for m in access._memberships_for(user, entity, permission, access.now()) if m.entity_id == entity.pk]


def _require_direct(user, entity, permission):
    access.require(user, entity, permission)
    ms = _direct(user, entity, permission)
    if not ms:
        access.deny(user, entity, permission, "no_membership")
    return ms


def _require_proposer(user, entity):
    ms = _require_direct(user, entity, PROPOSE)
    top = engine.governance_of(entity).top_authority_role
    if not {m.role.code for m in ms} & {top, "access_admin"}:
        _refuse("proposer_role", user, entity, PROPOSE)
    return ms


def _save(obj):
    obj._service_write = True
    obj.save()
    return obj


def _snapshot(version):
    return [r.snapshot() for r in version.rules.select_related("activity").prefetch_related("approver_roles")
            .order_by("activity__code", "band_currency", "amount_from", "step_no", "id")]


def rows_hash(version):
    """SHA-256 of the version's rows, without row ids (the ``DualControlRequest.payload_hash``, R76)."""
    rows = [{k: v for k, v in r.items() if k != "id"} for r in _snapshot(version)]
    return canonical_hash({"entity": version.entity.code, "version_no": version.version_no,
                           "effective_from": version.effective_from.isoformat(), "rows": rows})


def _diff(before, after):
    key = lambda r: (r["activity"], r["band_currency"], r["amount_from"], r["amount_to"], r["step_no"],  # noqa: E731
                     tuple(r["approver_roles"]), r["approver_user"])
    b = {key(r): r for r in before}
    a = {key(r): r for r in after}
    return {"added": [a[k] for k in a if k not in b], "removed": [b[k] for k in b if k not in a]}


# --- drafting ------------------------------------------------------------------------------------------------------

@transaction.atomic
def draft_version(entity, *, user, effective_from, summary_en, summary_ar, copy_current=True):
    """Draft version n+1 of ``entity``'s matrix (R76). By default it starts as a copy of the version in force."""
    entity = Entity.objects.get(pk=entity.pk)
    _require_proposer(user, entity)
    if not (summary_en or "").strip() or not (summary_ar or "").strip():
        raise ValidationError("A summary in English and Arabic is required. / الملخص بالإنجليزية والعربية مطلوب.")
    today = timezone.localdate(access.now())
    if effective_from < today:
        _refuse("effective_from_backdated", user, entity, PROPOSE, date=effective_from)
    last = AuthorityMatrixVersion.objects.filter(entity=entity).order_by("-version_no").first()
    version = _save(AuthorityMatrixVersion(
        entity=entity, version_no=(last.version_no + 1) if last else 1, effective_from=effective_from,
        summary_en=summary_en, summary_ar=summary_ar, proposed_by=user, proposed_at=access.now()))
    current = engine.effective_version(entity, today)
    if copy_current and current is not None:
        for rule in current.rules.prefetch_related("approver_roles"):
            copy = AuthorityRule(
                entity=entity, version=version, activity=rule.activity, band_currency=rule.band_currency,
                amount_from=rule.amount_from, amount_to=rule.amount_to, step_no=rule.step_no,
                min_approvals=rule.min_approvals, approver_user=rule.approver_user,
                delegation_allowed=rule.delegation_allowed, note_en=rule.note_en, note_ar=rule.note_ar,
                created_by=user)
            _save(copy)
            copy.approver_roles.set(rule.approver_roles.all())
    access.log(AccessLog.Event.MATRIX_DRAFTED, actor=user, entity=entity, permission=PROPOSE,
               details={"version": version.pk, "version_no": version.version_no,
                        "effective_from": effective_from.isoformat(), "copied_from": current.pk if current else None,
                        "summary_en": summary_en, "summary_ar": summary_ar})
    return version


def _decimal(value, name):
    try:
        return None if value is None else Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValidationError(f"{name} must be a number. / يجب أن يكون {name} رقمًا.") from None


def _check_places(user, entity, amount, currency):
    places = engine.check_amount_places(amount, currency)
    if places is not None:
        _refuse("amount_decimals", user, entity, PROPOSE, amount=amount, currency=currency, places=places)


def _role_holdable(role, entity):
    if not role.is_active or role.kind != Kind.HUMAN:
        return False
    if role.scope == Role.Scope.GROUP:
        return entity.consolidation == Entity.Consolidation.PARENT and entity.parent_id is None
    return True


@transaction.atomic
def add_rule(version, *, user, activity, approver_roles=(), approver_user=None, step_no=1, min_approvals=1,
             amount_from=Decimal("0"), amount_to=None, band_currency=None, delegation_allowed=True, note_en="",
             note_ar=""):
    """Add a row to a draft version (R69 to R74)."""
    version = AuthorityMatrixVersion.objects.select_for_update().get(pk=version.pk)
    entity = version.entity
    _require_proposer(user, entity)
    if not version.is_draft:
        _refuse("not_draft", user, entity, PROPOSE)
    act = activity if isinstance(activity, ApprovalActivity) else engine.activity_row(activity)
    roles = [r if isinstance(r, Role) else Role.objects.get(code=r) for r in approver_roles]
    if bool(roles) == (approver_user is not None):
        _refuse("one_approver_kind", user, entity, PROPOSE)
    for role in roles:
        if not _role_holdable(role, entity):
            _refuse("role_not_for_entity", user, entity, PROPOSE, role=role.code)
    if approver_user is not None:  # R69, R72: a named user is a person of this entity
        if access.kind_of(approver_user) != Kind.HUMAN or not Membership.objects.active(access.now()).filter(
                user=approver_user, entity=entity, role__kind=Kind.HUMAN).exists():
            _refuse("not_member_of_entity", user, entity, PROPOSE, who=approver_user.get_username())
    currency = band_currency.code if isinstance(band_currency, Currency) else band_currency
    if currency == entity.base_currency_id:
        currency = None
    amount_from = _decimal(amount_from, "amount_from") or Decimal("0")
    amount_to = _decimal(amount_to, "amount_to")
    for amount in (amount_from, amount_to):
        if amount is not None:
            _check_places(user, entity, amount, currency or entity.base_currency_id)
    if amount_to is not None and amount_to < amount_from:
        raise ValidationError("The band ends before it starts. / ينتهي النطاق قبل أن يبدأ.")
    for other in AuthorityRule.objects.filter(version=version, activity=act, band_currency_id=currency):
        same = other.amount_from == amount_from and other.amount_to == amount_to
        overlaps = other.amount_from <= (amount_to if amount_to is not None else Decimal("1e30")) and \
            amount_from <= (other.amount_to if other.amount_to is not None else Decimal("1e30"))
        if overlaps and not same:
            _refuse("band_overlap", user, entity, PROPOSE, activity=act.code,
                    currency=currency or entity.base_currency_id)
    rule = _save(AuthorityRule(
        entity=entity, version=version, activity=act, band_currency_id=currency, amount_from=amount_from,
        amount_to=amount_to, step_no=step_no, min_approvals=min_approvals, approver_user=approver_user,
        delegation_allowed=delegation_allowed, note_en=note_en, note_ar=note_ar, created_by=user))
    if roles:
        rule.approver_roles.set(roles)
    access.log(AccessLog.Event.MATRIX_ROW_ADDED, actor=user, entity=entity, permission=PROPOSE,
               details={"version": version.pk, "row": rule.snapshot()})
    return rule


@transaction.atomic
def remove_rule(rule, *, user):
    rule = AuthorityRule.objects.select_related("version", "entity").get(pk=rule.pk)
    _require_proposer(user, rule.entity)
    if not rule.version.is_draft:
        _refuse("not_draft", user, rule.entity, PROPOSE)
    snap = rule.snapshot()
    rule.approver_roles.clear()
    rule._service_write = True
    rule.delete()
    access.log(AccessLog.Event.MATRIX_ROW_REMOVED, actor=user, entity=rule.entity, permission=PROPOSE,
               details={"version": rule.version_id, "row": snap})


# --- submission and decision -----------------------------------------------------------------------------------------

@transaction.atomic
def submit_version(version, *, user):
    """Freeze the draft and open the four-eyes request (R76): ``payload_hash`` = the rows hash."""
    version = AuthorityMatrixVersion.objects.select_for_update().get(pk=version.pk)
    entity = version.entity
    _require_proposer(user, entity)
    if not version.is_draft:
        _refuse("not_draft", user, entity, PROPOSE)
    at = access.now()
    version.rows_hash = rows_hash(version)
    req = DualControlRequest.objects.create(
        entity=entity, permission=APPROVE, target_type=ContentType.objects.get_for_model(AuthorityMatrixVersion),
        target_id=version.pk, payload_hash=version.rows_hash, requested_by=user, requested_at=at,
        expires_at=at + MATRIX_DECISION_TTL)
    version.dual_control_request = req
    version.status, version.submitted_at = Status.SUBMITTED, at
    _save(version)
    current = engine.effective_version(entity, timezone.localdate(at))
    access.log(AccessLog.Event.MATRIX_SUBMITTED, actor=user, entity=entity, permission=APPROVE,
               details={"version": version.pk, "version_no": version.version_no, "dual_control_request": req.pk,
                        "rows_hash": version.rows_hash, "effective_from": version.effective_from.isoformat(),
                        "diff": _diff(_snapshot(current) if current else [], _snapshot(version))})
    return version


def decider_roles(entity, proposer, at=None):
    """R76 / OQ-42: who decides a change ``proposer`` asked for in ``entity``."""
    at = at or access.now()
    top = engine.governance_of(entity).top_authority_role
    proposer_is_top = Membership.objects.active(at).filter(user=proposer, entity=entity, role__code=top).exists()
    if not proposer_is_top:
        return frozenset({top})
    return frozenset({"chairman"}) if engine.is_group_parent(entity) else frozenset({"board_representative"})


def _require_decider(user, entity, proposer_id, at):
    if proposer_id == getattr(user, "pk", None):  # S!: nobody decides their own change (R15, R19)
        access.deny(user, entity, APPROVE, "self_approval")
    ms = [m for m in access._evaluate(user, entity, APPROVE, at)[0] if m.entity_id == entity.pk]
    if not ms:
        access.deny(user, entity, APPROVE, "no_membership")
    from django.contrib.auth import get_user_model

    proposer = get_user_model().objects.get(pk=proposer_id)
    roles = decider_roles(entity, proposer, at)
    if not {m.role.code for m in ms} & roles:
        names = list(Role.objects.filter(code__in=roles).values_list("name_en", "name_ar"))
        _refuse("decider_role", user, entity, APPROVE, role=", ".join(n for n, _ in names) or ", ".join(roles),
                role_ar="، ".join(a for _, a in names) or ", ".join(roles))
    return ms


def _pending(user, entity, req, at):
    if req is None or req.status != DualControlRequest.Status.PENDING:
        access.deny(user, entity, APPROVE, "dual_control")
    if req.expires_at <= at:
        DualControlRequest.objects.filter(pk=req.pk, status=DualControlRequest.Status.PENDING)._transition(
            status=DualControlRequest.Status.EXPIRED)
        access.deny(user, entity, APPROVE, "dual_control_expired")


@transaction.atomic
def decide_version(version, *, user, approve, note=""):
    """The second person approves or rejects a submitted version (R76, R77). Approval is used at once: the version
    takes effect on ``effective_from`` (today or later)."""
    version = AuthorityMatrixVersion.objects.select_for_update().get(pk=version.pk)
    entity, at = version.entity, access.now()
    if version.status != Status.SUBMITTED:
        _refuse("not_draft", user, entity, APPROVE)
    ms = _require_decider(user, entity, version.proposed_by_id, at)
    req = version.dual_control_request
    _pending(user, entity, req, at)
    today = timezone.localdate(at)
    if approve and version.effective_from < today:
        _refuse("effective_from_backdated", user, entity, APPROVE, date=version.effective_from)
    if approve and (rows_hash(version) != version.rows_hash or req.payload_hash != version.rows_hash):
        _refuse("rows_changed", user, entity, APPROVE)
    status = DualControlRequest.Status.APPROVED if approve else DualControlRequest.Status.REJECTED
    DualControlRequest.objects.filter(pk=req.pk, status=DualControlRequest.Status.PENDING)._transition(
        status=status, decided_by=user, decided_at=at, decision_note=note[:500])
    access.log(AccessLog.Event.DUAL_CONTROL_DECIDED, actor=user, entity=entity, permission=APPROVE,
               details={"request": req.pk, "status": status, "note": note[:500],
                        "roles": sorted({m.role.code for m in ms})})
    version.decided_by, version.decided_at, version.decision_note = user, at, note[:500]
    version.status = Status.APPROVED if approve else Status.REJECTED
    _save(version)
    if approve:
        DualControlRequest.objects.filter(pk=req.pk, status=DualControlRequest.Status.APPROVED)._transition(
            status=DualControlRequest.Status.USED)
        access.log(AccessLog.Event.DUAL_CONTROL_USED, actor=user, entity=entity, permission=APPROVE,
                   details={"request": req.pk, "payload_hash": req.payload_hash})
    access.log(AccessLog.Event.MATRIX_APPROVED if approve else AccessLog.Event.MATRIX_REJECTED, actor=user,
               entity=entity, permission=APPROVE,
               details={"version": version.pk, "version_no": version.version_no, "dual_control_request": req.pk,
                        "rows_hash": version.rows_hash, "effective_from": version.effective_from.isoformat(),
                        "note": note[:500], "rows": _snapshot(version)})
    if approve:
        activate_due_versions(entity=entity, today=today)
    return version


@transaction.atomic
def withdraw_version(version, *, user, reason):
    """The proposer (or the top authority) withdraws a draft or submitted version; the version in force stays."""
    version = AuthorityMatrixVersion.objects.select_for_update().get(pk=version.pk)
    entity = version.entity
    _require_proposer(user, entity)
    if not (reason or "").strip():
        raise ValidationError("A reason is required. / السبب مطلوب.")
    if version.status not in (Status.DRAFT, Status.SUBMITTED):
        _refuse("not_draft", user, entity, PROPOSE)
    if version.dual_control_request_id:
        DualControlRequest.objects.filter(pk=version.dual_control_request_id,
                                          status=DualControlRequest.Status.PENDING)._transition(
            status=DualControlRequest.Status.EXPIRED)
    version.status = Status.WITHDRAWN
    version.decision_note = reason[:500]
    _save(version)
    access.log(AccessLog.Event.MATRIX_WITHDRAWN, actor=user, entity=entity, permission=PROPOSE,
               details={"version": version.pk, "version_no": version.version_no, "reason": reason})
    return version


def activate_due_versions(*, entity=None, today=None):
    """Job (and called at approval): mark the version in force for each entity, superseding its predecessor, and log
    ``authority_matrix_effective`` once per version (R77, R78). Effective dates decide which rules apply whether or
    not this ran; it records the change of status."""
    today = today or timezone.localdate(access.now())
    entities = [entity] if entity is not None else Entity.objects.filter(authority_versions__isnull=False).distinct()
    changed = []
    for ent in entities:
        current = engine.effective_version(ent, today)
        if current is None or current.status != Status.APPROVED:
            continue
        older = AuthorityMatrixVersion.objects.filter(entity=ent, status=Status.APPROVED,
                                                      effective_from__lte=current.effective_from).exclude(pk=current.pk)
        for old in older:
            old.status, old.superseded_by = Status.SUPERSEDED, current
            _save(old)
        if not AccessLog.objects.filter(event=AccessLog.Event.MATRIX_EFFECTIVE, entity=ent,
                                        details__version=current.pk).exists():
            access.log(AccessLog.Event.MATRIX_EFFECTIVE, entity=ent, permission=APPROVE, source="system:access",
                       details={"version": current.pk, "version_no": current.version_no,
                                "effective_from": current.effective_from.isoformat(),
                                "superseded": [o.pk for o in older]})
            changed.append(current)
    return changed


# --- reading (R2, R90: per entity, never through the group parent) ---------------------------------------------------

READ_PERMISSIONS = (PROPOSE, APPROVE, "core.view_governance_report")


def entities_reading(user):
    ids = set()
    for perm in READ_PERMISSIONS:
        ids |= set(access.entities_for(user, perm).values_list("pk", flat=True))
    return Entity.objects.filter(pk__in=ids)


def versions(user, entity=None):
    """Matrix versions ``user`` may read: entities where they hold a §10 right through their own membership."""
    qs = AuthorityMatrixVersion.objects.filter(entity__in=entities_reading(user))
    return qs.filter(entity=entity) if entity is not None else qs


def get_version(user, pk):
    try:
        return versions(user).get(pk=pk)
    except AuthorityMatrixVersion.DoesNotExist:
        raise Http404("Not found / غير موجود") from None


def rules(user, version):
    if not versions(user).filter(pk=version.pk).exists():
        raise Http404("Not found / غير موجود")
    return AuthorityRule.objects.filter(version=version)


# --- top authority and self-approval switch (R67, R76) ---------------------------------------------------------------

GOVERNANCE_FIELDS = ("top_authority_role", "self_approval_enabled")


def governance_payload(entity, changes, reason):
    return {"entity": entity.code, "governance": {k: changes[k] for k in sorted(changes)}, "reason": str(reason)}


@transaction.atomic
def request_governance_change(entity, *, user, reason, **changes):
    """Step 1 of R76 for ``EntityGovernance``: a holder of ``core.change_entity_settings`` on the group parent asks
    to change the entity's top-authority role or self-approval switch. Decided by :func:`decide_governance_change`
    (the R76 decider), applied by ``core.services.change_entity_settings(..., dual_control_request=...)``."""
    _check_governance_changes(changes)
    if not (reason or "").strip():
        raise ValidationError("A reason is required. / السبب مطلوب.")
    entity = Entity.objects.get(pk=entity.pk)
    group = access.group_parent_of(entity)
    access.require(user, group, "core.change_entity_settings")  # G: on the group parent (R32, G8)
    row = engine.ensure_governance(entity)
    at = access.now()
    req = DualControlRequest.objects.create(
        entity=entity, permission=APPROVE, target_type=ContentType.objects.get_for_model(EntityGovernance),
        target_id=row.pk, payload_hash=access.payload_hash(governance_payload(entity, changes, reason)),
        requested_by=user, requested_at=at, expires_at=at + MATRIX_DECISION_TTL)
    access.log(AccessLog.Event.GOVERNANCE_CHANGE_REQUESTED, actor=user, entity=entity, permission=APPROVE,
               details={"request": req.pk, "changes": changes, "reason": reason, "payload_hash": req.payload_hash})
    return req


def _check_governance_changes(changes):
    unknown = set(changes) - set(GOVERNANCE_FIELDS)
    if unknown or not changes:
        raise ValidationError(f"Not a governance setting: {sorted(unknown)}. / ليس من إعدادات الحوكمة.")
    if "self_approval_enabled" in changes and type(changes["self_approval_enabled"]) is not bool:
        raise ValidationError("self_approval_enabled must be True or False. / يجب أن تكون القيمة صحيحة أو خاطئة.")
    if "top_authority_role" in changes:
        role = Role.objects.filter(code=changes["top_authority_role"], kind=Kind.HUMAN, is_active=True).first()
        if role is None:
            raise ValidationError("The top authority is an active human role. / السلطة العليا دور بشري نشط.")


@transaction.atomic
def decide_governance_change(request, *, user, approve, note=""):
    """Step 2 of R76: the second person (top authority, or chairman / board representative when the top authority
    asked) approves or rejects the change; never its requester."""
    req = DualControlRequest.objects.select_for_update().get(pk=request.pk)
    if req.permission != APPROVE or req.target_type != ContentType.objects.get_for_model(EntityGovernance):
        raise ValidationError("Not a governance change request. / ليس طلب تغيير للحوكمة.")
    entity, at = req.entity, access.now()
    ms = _require_decider(user, entity, req.requested_by_id, at)
    _pending(user, entity, req, at)
    status = DualControlRequest.Status.APPROVED if approve else DualControlRequest.Status.REJECTED
    DualControlRequest.objects.filter(pk=req.pk, status=DualControlRequest.Status.PENDING)._transition(
        status=status, decided_by=user, decided_at=at, decision_note=note[:500])
    access.log(AccessLog.Event.DUAL_CONTROL_DECIDED, actor=user, entity=entity, permission=APPROVE,
               details={"request": req.pk, "status": status, "note": note[:500],
                        "roles": sorted({m.role.code for m in ms}), "governance": True})
    req.refresh_from_db()
    return req


def use_governance_request(user, entity, request, changes, reason):
    """Used by ``change_entity_settings``: the approved, unused request for exactly these changes (R76, R19)."""
    at = access.now()
    req = DualControlRequest.objects.select_for_update().get(pk=request.pk)
    row = engine.ensure_governance(entity)
    if (req.entity_id != entity.pk or req.permission != APPROVE
            or req.target_type_id != ContentType.objects.get_for_model(EntityGovernance).pk or req.target_id != row.pk
            or req.payload_hash != access.payload_hash(governance_payload(entity, changes, reason))
            or req.decided_by_id in (None, req.requested_by_id)):
        access.deny(user, entity, APPROVE, "dual_control")
    if req.expires_at <= at:
        access.deny(user, entity, APPROVE, "dual_control_expired")
    used = DualControlRequest.objects.filter(pk=req.pk, status=DualControlRequest.Status.APPROVED)._transition(
        status=DualControlRequest.Status.USED)
    if not used:
        access.deny(user, entity, APPROVE, "dual_control")
    access.log(AccessLog.Event.DUAL_CONTROL_USED, actor=user, entity=entity, permission=APPROVE,
               details={"request": req.pk, "payload_hash": req.payload_hash})
    return row


def _apply_governance(row, changes):
    before = {"top_authority_role": row.top_authority_role.code, "self_approval_enabled": row.self_approval_enabled}
    if "top_authority_role" in changes:
        row.top_authority_role = Role.objects.get(code=changes["top_authority_role"])
    if "self_approval_enabled" in changes:
        row.self_approval_enabled = changes["self_approval_enabled"]
    _save(row)
    if "top_authority_role" in changes:
        recipients = set(row.governance_report_recipients.values_list("code", flat=True))
        recipients.discard(before["top_authority_role"])
        recipients |= {"internal_auditor", row.top_authority_role.code}
        row.governance_report_recipients.set(Role.objects.filter(code__in=recipients))
    after = {"top_authority_role": row.top_authority_role.code, "self_approval_enabled": row.self_approval_enabled}
    return {k: before[k] for k in changes}, {k: after[k] for k in changes}


assert all(p in catalogue.CATALOGUE for p in (PROPOSE, APPROVE)), "matrix permissions missing from the catalogue"
