"""Generic approval engine. Spec: docs/specs/approvals-and-web-ui.md.

Entry points (the only way to create or change approval requests):
  submit(doc, user)                        R5, R18, D8
  act(request, actor, action, ...)         R8, R9/D10, R12, R14, R16, R17, R18, D11/R20
  cancel(request, user)                    R13
  run_escalations(now)                     R15
  resolve_policy(...)                      R5
  ensure_spending_policies()               R6
  self_approval_report(viewer, year, month) D10
"""
import calendar
import datetime
from dataclasses import dataclass
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from access import services as access
from access.errors import AccessDenied, BilingualError, Conflict, NotFound, RuleError
from access.models import Role, User
from access.services import Context, audit, has_perm, has_role, snapshot

from . import notify as notifications
from .models import (
    ApprovableType, ApprovalAction, ApprovalPolicy, ApprovalRequest, ApprovalStep, Delegation, Notification,
)
from .registry import adapter_for_code, code_for_model

MP_ROLE = "managing_partner"
INF = Decimal("Infinity")

A = ApprovalAction.Action
S = ApprovalRequest.Status


def now():
    """Single clock for the engine; tests replace it to freeze time."""
    return timezone.now()


class PostingFailed(RuleError):
    """Final approval was rolled back because the owning module refused to post (D11).

    keep_changes: the alert and audit row written after the rollback must be committed.
    """

    keep_changes = True


class DocumentChanged(Conflict):
    """R12: the request was set to returned; that state change must be committed."""

    keep_changes = True


def _link(request):
    return f"/approvals/{request.pk}"


def _ctx(ctx):
    return ctx or Context()


# ---------------------------------------------------------------- policy resolution (R5)


def _range_key(policy):
    lo = policy.amount_min if policy.amount_min is not None else -INF
    hi = policy.amount_max if policy.amount_max is not None else INF
    bounds = (policy.amount_min is not None) + (policy.amount_max is not None)
    width = hi - lo if bounds == 2 else INF
    # Narrowest first: more bounds, smaller width, higher minimum, lower maximum; then priority.
    return (-bounds, width, -lo, hi, -policy.priority)


def resolve_policy(entity, document_type, amount, currency, on=None):
    on = on or timezone.localdate()
    qs = (
        ApprovalPolicy.objects.filter(document_type=document_type, is_active=True, valid_from__lte=on, currency=currency)
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=on))
        .filter(Q(entity=entity) | Q(entity__isnull=True))
        .prefetch_related("steps")
    )
    matches = []
    for p in qs:
        if amount is None:
            if p.amount_min is None and p.amount_max is None:
                matches.append(p)
            continue
        if p.amount_min is not None and amount < p.amount_min:
            continue
        if p.amount_max is not None and amount >= p.amount_max:
            continue
        matches.append(p)
    specific = [p for p in matches if p.entity_id == entity.pk]
    matches = specific or matches
    if not matches:
        raise RuleError(
            "no_policy",
            f"No approval policy covers {document_type.name_en} for {entity.code} ({amount or '-'} {currency}).",
            f"لا توجد سياسة موافقة تغطي {document_type.name_ar} للمنشأة {entity.code} ({amount or '-'} {currency}).",
        )
    matches.sort(key=_range_key)
    if len(matches) > 1 and _range_key(matches[0]) == _range_key(matches[1]):
        raise RuleError("ambiguous_policy", "Two approval policies match equally. Fix the approval matrix.",
                        "سياستا موافقة تنطبقان بالتساوي. يرجى تصحيح مصفوفة الموافقات.")
    policy = matches[0]
    if not policy.steps.exists():
        raise RuleError("policy_without_steps", "The matching approval policy has no steps.",
                        "سياسة الموافقة المطابقة لا تحتوي على خطوات.")
    return policy


@transaction.atomic
def ensure_spending_policies(currency="SAR"):
    """R6: one group-wide policy per spending type, any amount, one step: managing_partner (D2)."""
    mp = Role.objects.get(code=MP_ROLE)
    created = 0
    for t in ApprovableType.objects.filter(is_spending=True):
        if ApprovalPolicy.objects.filter(document_type=t, entity__isnull=True, currency=currency).exists():
            continue
        p = ApprovalPolicy.objects.create(
            document_type=t, currency=currency, priority=0,
            name_en=f"{t.name_en}: Managing Partner approves all",
            name_ar=f"{t.name_ar}: يعتمد الشريك المدير الجميع",
            valid_from=datetime.date(2026, 1, 1),
        )
        ApprovalStep.objects.create(policy=p, sequence=1, approver_role=mp)
        created += 1
    return created


def register_type(code, model, name_en, name_ar, is_spending=False):
    ct = ContentType.objects.get_for_model(model)
    t, _ = ApprovableType.objects.update_or_create(
        code=code, defaults={"content_type": ct, "name_en": name_en, "name_ar": name_ar, "is_spending": is_spending}
    )
    return t


# ---------------------------------------------------------------- eligibility


def _steps_at(request, sequence=None):
    return list(request.policy.steps.filter(sequence=sequence or request.current_sequence))


def _escalation_targets(request, step):
    roles, users = set(), set()
    for a in request.actions.filter(action=A.ESCALATE, step=step):
        if a.escalated_to_role_id:
            roles.add(a.escalated_to_role_id)
        if a.escalated_to_user_id:
            users.add(a.escalated_to_user_id)
    return roles, users


def step_candidates(request, step):
    """Users designated for the step (role in the document's entity, or named user), plus escalation
    targets. Everyone must hold approvals.request.act in that entity (service agents never do)."""
    entity = request.entity
    ids = set()
    if step.approver_role_id:
        ids |= {u.pk for u in access.users_with_perm(entity, "approvals.request.act", role=step.approver_role)}
    if step.approver_user_id and has_perm(step.approver_user, entity, "approvals.request.act"):
        ids.add(step.approver_user_id)
    roles, users = _escalation_targets(request, step)
    for role in Role.objects.filter(pk__in=roles):
        ids |= {u.pk for u in access.users_with_perm(entity, "approvals.request.act", role=role)}
    for u in User.objects.filter(pk__in=users):
        if has_perm(u, entity, "approvals.request.act"):
            ids.add(u.pk)
    return ids


def _approved_principals(request):
    return {a.principal_id for a in request.actions.filter(action=A.APPROVE)}


def _step_done(request, step):
    return request.actions.filter(action=A.APPROVE, step=step).count() >= step.min_approvals


def _involved(request):
    """Creator, submitter and every editor of the draft (review A). Cached per request object."""
    ids = getattr(request, "_involved_ids", None)
    if ids is None:
        ids = {request.document_created_by_id, request.submitted_by_id}
        if request.pk:
            ids |= set(request.document_editors.values_list("pk", flat=True))
        request._involved_ids = ids
    return ids


def _sod_blocked(request, user_id):
    """R8: creator, submitter and editors never approve (D10 override handled separately)."""
    return user_id in _involved(request)


def _eligible_others(request, step, exclude_id):
    done = _approved_principals(request)
    return {
        uid for uid in step_candidates(request, step)
        if uid != exclude_id and not _sod_blocked(request, uid) and uid not in done
    }


def _mp_sole_approver(request, step, principal):
    """The D10 situation: the MP created, submitted or edited the document and nobody else is eligible."""
    return (
        principal.pk in _involved(request)
        and principal.is_human
        and has_role(principal, request.entity, MP_ROLE)
        and principal.pk in step_candidates(request, step)
        and not _eligible_others(request, step, principal.pk)
    )


def _covering(delegations, request):
    """Delegations whose entity, document types and amount limit cover the request."""
    out = []
    qs = delegations.filter(Q(entity__isnull=True) | Q(entity=request.entity))
    for d in qs.select_related("delegator", "delegate").prefetch_related("document_types"):
        types = list(d.document_types.all())
        if types and request.document_type not in types:
            continue
        if d.amount_max is not None:
            if request.amount is None or request.currency != d.currency or request.amount > d.amount_max:
                continue
        out.append(d)
    return out


def _active_delegations(request, delegate, at):
    """Active delegations to `delegate` covering the request."""
    return _covering(Delegation.objects.filter(delegate=delegate, starts_at__lte=at, ends_at__gt=at), request)


def _delegations_from(request, delegator, at):
    """Active delegations from `delegator` covering the request (D16)."""
    return _covering(Delegation.objects.filter(delegator=delegator, starts_at__lte=at, ends_at__gt=at), request)


def _self_approval_allowed(request, step, principal, at=None):
    """D10 + D16: MP self-approval only when he is the sole eligible approver AND has no active
    delegation covering the document (then the delegate approves instead)."""
    return _mp_sole_approver(request, step, principal) and not _delegations_from(request, principal, at or now())


@dataclass
class Eligibility:
    step: ApprovalStep
    on_behalf_of: User | None = None
    self_approval: bool = False


def _direct_eligibility(request, principal, at, via_delegate=False):
    if not principal.is_active or not principal.is_human or not has_perm(principal, request.entity, "approvals.request.act"):
        return None
    if principal.pk in _approved_principals(request):
        return None  # one person counts once per request
    for step in _steps_at(request):
        if _step_done(request, step) or principal.pk not in step_candidates(request, step):
            continue
        if not _sod_blocked(request, principal.pk):
            return Eligibility(step)
        if via_delegate:
            # D16: the delegate of an MP who is the sole approver of his own document approves for him.
            if _mp_sole_approver(request, step, principal):
                return Eligibility(step)
        elif _self_approval_allowed(request, step, principal, at):
            return Eligibility(step, self_approval=True)
    return None


def _delegate_can_act(request, delegate):
    """R16 + R8: active human delegate with view access, not creator/submitter, not already counted."""
    return (
        delegate.is_active and delegate.is_human
        and has_perm(delegate, request.entity, "approvals.request.view")
        and not _sod_blocked(request, delegate.pk)
        and delegate.pk not in _approved_principals(request)
    )


def eligibility(request, actor, on_behalf_of=None, at=None):
    """Return how `actor` may act on the current step, or None. Never raises."""
    if request.status != S.PENDING or not actor.is_active or not actor.is_human:
        return None  # R18, D17: service users never act
    at = at or now()
    if on_behalf_of is None:
        direct = _direct_eligibility(request, actor, at)
        if direct:
            return direct
        candidates = [d.delegator for d in _active_delegations(request, actor, at)]
    else:
        candidates = [d.delegator for d in _active_delegations(request, actor, at) if d.delegator_id == on_behalf_of.pk]
    if not candidates or not _delegate_can_act(request, actor):
        return None
    for delegator in candidates:
        e = _direct_eligibility(request, delegator, at, via_delegate=True)
        if e:
            return Eligibility(e.step, on_behalf_of=delegator)
    return None


def allowed_actions(request, user):
    out = []
    if eligibility(request, user):
        out += ["approve", "reject", "return"]
    if _can_cancel(request, user):
        out.append("cancel")
    return out


# ---------------------------------------------------------------- visibility (R1)


def visible_requests(user):
    """Requests the user may see: entity-wide view, own requests, or delegated to them."""
    if user is None or not user.is_authenticated or not user.is_active:
        return ApprovalRequest.objects.none()
    entities = access.entities_with_perm(user, "approvals.request.view")
    own_entities = access.entities_with_perm(user, "approvals.request.view_own")
    q = Q(entity__in=entities) | (Q(entity__in=own_entities) & (Q(submitted_by=user) | Q(document_created_by=user)))
    return ApprovalRequest.objects.filter(q).select_related("entity", "document_type", "policy", "submitted_by")


def get_visible(user, pk):
    try:
        return visible_requests(user).get(pk=pk)
    except (ApprovalRequest.DoesNotExist, ValueError, ValidationError):
        raise NotFound("not_found", "Approval request not found.", "طلب الموافقة غير موجود.")


def pending_for(user):
    """Inbox 'Pending for me': pending requests the user may act on now (directly or as delegate)."""
    return [r for r in visible_requests(user).filter(status=S.PENDING) if eligibility(r, user)]


# ---------------------------------------------------------------- notifications helpers


def _notify_step(request, ctx):
    """Notify eligible approvers of the current step; alert if none remains (R9)."""
    notified = set()
    self_case = False
    for step in _steps_at(request):
        if _step_done(request, step):
            continue
        for uid in _eligible_others(request, step, exclude_id=None):
            notified.add(uid)
        creator = request.document_created_by
        if not _eligible_others(request, step, None) and _mp_sole_approver(request, step, creator):
            delegations = _delegations_from(request, creator, now())
            if delegations:  # D16: the delegate approves
                notified |= {d.delegate_id for d in delegations if _delegate_can_act(request, d.delegate)}
            else:
                self_case = True
                notified.add(creator.pk)
    for user in User.objects.filter(pk__in=notified):
        notifications.notify(
            user, Notification.Kind.APPROVAL_PENDING, entity=request.entity, link=_link(request),
            title_en=f"Approval needed: {request.summary_en}"[:200],
            title_ar=f"مطلوب اعتماد: {request.summary_ar}"[:200],
            body_en=(f"{request.amount} {request.currency}. " if request.amount is not None else "")
            + ("You created this document; approving it will be logged as a self-approval override." if self_case else ""),
            body_ar=(f"{request.amount} {request.currency}. " if request.amount is not None else "")
            + ("أنت منشئ هذا المستند؛ سيتم تسجيل اعتمادك كاعتماد ذاتي استثنائي." if self_case else ""),
        )
    if not notified:
        _alert_no_eligible(request, ctx)
    return notified


def _mp_users(entity):
    return [g.user for g in access.active_grants_for_entity(entity).filter(role__code=MP_ROLE).select_related("user")]


def _alert_no_eligible(request, ctx):
    recipients = {u.pk: u for u in _mp_users(request.entity)}
    recipients.setdefault(request.submitted_by_id, request.submitted_by)
    for user in recipients.values():
        notifications.notify(
            user, Notification.Kind.ALERT, alert_code="no_eligible_approver", entity=request.entity,
            link=_link(request),
            title_en=f"No eligible approver: {request.summary_en}"[:200],
            title_ar=f"لا يوجد معتمد مؤهل: {request.summary_ar}"[:200],
            body_en="Segregation of duties leaves nobody who can approve this request. Update the approval matrix or delegations.",
            body_ar="لا يوجد من يمكنه اعتماد هذا الطلب بسبب الفصل بين المهام. حدّث مصفوفة الموافقات أو التفويضات.",
        )
    audit(access.system_user(), "approval.no_eligible_approver", request, ctx=ctx)


def _notify_submitter(request, kind, title_en, title_ar, body=""):
    for uid in {request.submitted_by_id, request.document_created_by_id}:
        user = User.objects.get(pk=uid)
        if not user.is_active:
            continue
        notifications.notify(
            user, kind, entity=request.entity, link=_link(request),
            title_en=f"{title_en}: {request.summary_en}"[:200], title_ar=f"{title_ar}: {request.summary_ar}"[:200],
            body_en=body, body_ar=body,
        )


# ---------------------------------------------------------------- submit


def submit(doc, user, ctx=None):
    """Submit a module document for approval (§3.3 step 4). Returns the ApprovalRequest."""
    code = code_for_model(type(doc))
    return submit_by_code(code, doc.pk, user, ctx)


@transaction.atomic
def submit_by_code(code, object_id, user, ctx=None):
    ctx = _ctx(ctx)
    adapter = adapter_for_code(code)
    doc_type = ApprovableType.objects.filter(code=code).first()
    if doc_type is None:
        raise RuleError("type_not_registered", f"Document type {code} is not registered.",
                        f"نوع المستند {code} غير مسجل.")
    try:
        doc = adapter.get_for_update(object_id)
    except (adapter.model.DoesNotExist, ValueError, ValidationError):
        raise NotFound("not_found", "Document not found.", "المستند غير موجود.")
    entity = adapter.entity(doc)
    if not user.is_active:
        raise AccessDenied("inactive", "Your user is inactive.", "المستخدم غير نشط.")
    if not user.is_human:
        # D20: AI agents and integrations create drafts only; a human must submit them.
        raise AccessDenied("service_user_cannot_submit",
                           "AI agents and integrations cannot submit for approval; a person must submit the draft (D20).",
                           "لا يمكن للوكلاء والتكاملات تقديم طلبات الموافقة؛ يجب أن يقدّم شخص المسودة (D20).")
    # R1/R2/T2: permission in the document's own entity only.
    access.require_perm(user, entity, "approvals.request.submit")
    if adapter.submit_permission:
        access.require_perm(user, entity, adapter.submit_permission)
    creator = adapter.created_by(doc)
    agent_draft = creator is not None and not creator.is_human
    if creator is None or (creator.pk != user.pk and not agent_draft):
        # Conservative build choice: users submit their own drafts only (spec §7.1 "own drafts").
        # D20: drafts made by an AI agent or integration are submitted by a person holding the
        # submit rights in the document's entity (that person then cannot approve it, R8).
        raise AccessDenied("not_own_draft", "You can only submit drafts you created.",
                           "يمكنك تقديم المسودات التي أنشأتها فقط.")
    if not adapter.is_submittable(doc):
        raise Conflict("not_draft", "Only drafts can be submitted for approval.", "يمكن تقديم المسودات فقط للموافقة.")
    if ApprovalRequest.objects.filter(document_type=doc_type, object_id=str(doc.pk), status=S.PENDING).exists():
        raise Conflict("already_pending", "This document already has a pending approval request.",
                       "يوجد طلب موافقة معلق لهذا المستند.")
    amount, currency = adapter.approval_amount(doc)
    if amount is not None:
        amount = Decimal(amount).quantize(Decimal("0.01"))
    policy = resolve_policy(entity, doc_type, amount, currency)
    first_seq = policy.steps.order_by("sequence").values_list("sequence", flat=True).first()
    summary_en, summary_ar = adapter.approval_summary(doc)
    at = now()
    before_doc = adapter.snapshot(doc)
    try:
        with transaction.atomic():
            request = ApprovalRequest.objects.create(
                entity=entity, document_type=doc_type, object_id=str(doc.pk),
                document_version=adapter.document_version(doc), amount=amount, currency=currency or "",
                summary_en=summary_en[:300], summary_ar=summary_ar[:300], policy=policy, status=S.PENDING,
                current_sequence=first_seq, step_started_at=at, submitted_by=user, document_created_by=creator,
                source=adapter.source(doc), submitted_at=at,
                expires_at=at + datetime.timedelta(hours=policy.expire_after_hours) if policy.expire_after_hours else None,
            )
    except IntegrityError:
        raise Conflict("already_pending", "This document already has a pending approval request.",
                       "يوجد طلب موافقة معلق لهذا المستند.")
    editors = set(adapter.editor_ids(doc)) | {creator.pk}
    request.document_editors.add(*User.objects.filter(pk__in=editors))  # append-only through table
    request._involved_ids = None
    ApprovalAction.objects.create(request=request, actor=user, action=A.SUBMIT, at=at, channel=_channel(ctx))
    adapter.on_submitted(doc, request)
    audit(user, "approval.submit", request, entity=entity, before={"document": before_doc},
          after={"request": snapshot(request), "document": adapter.snapshot(doc), "editors": sorted(editors)},
          ctx=ctx, source=request.source)
    _notify_step(request, ctx)
    return request


def _channel(ctx):
    valid = set(ApprovalAction.Channel.values)
    return ctx.channel if ctx.channel in valid else ApprovalAction.Channel.SYSTEM


# ---------------------------------------------------------------- act


def act(request, actor, action, *, comment="", document_version=None, on_behalf_of=None, ctx=None):
    """Approve / reject / return. Raises a BilingualError subclass on refusal."""
    ctx = _ctx(ctx)
    try:
        with transaction.atomic():
            result, changed = _act(request.pk, actor, action, comment, document_version, on_behalf_of, ctx)
    except _PostingError as failure:
        # D11: the approval (and the post) were rolled back with the block above. Record the failure
        # in its own transaction; if even that fails, log it and still return the posting error.
        try:
            with transaction.atomic():
                _record_posting_failure(failure, actor, ctx)
        except Exception:  # pragma: no cover - defensive
            import logging

            logging.getLogger("approvals").exception("Could not record posting failure for request %s",
                                                     failure.request_id)
        raise PostingFailed(
            "posting_failed",
            f"Approval rolled back: posting was refused. {failure.reason}",
            f"تم التراجع عن الاعتماد: رُفض الترحيل. {failure.reason}",
            detail=failure.messages,
        ) from failure.original
    if changed:
        # R12: the request is already committed as returned; now refuse with 409.
        raise DocumentChanged(
            "document_changed",
            "The document changed after submission. The request was returned to the submitter.",
            "تغيّر المستند بعد تقديمه. أُعيد الطلب إلى مقدّمه.",
        )
    return result


class _PostingError(Exception):
    def __init__(self, request_id, original):
        self.request_id = request_id
        self.original = original
        if isinstance(original, (ValidationError, BilingualError)):
            msgs = getattr(original, "messages", None) or [str(original)]
        else:
            # Unexpected error: do not leak internals to the approver; the type is kept for the audit row.
            msgs = [f"Unexpected error while posting ({type(original).__name__}). The administrator has been alerted."]
        self.messages = [str(m) for m in msgs]
        self.reason = " ".join(self.messages)


def _record_posting_failure(failure, actor, ctx):
    request = ApprovalRequest.objects.get(pk=failure.request_id)
    audit(actor, "approval.posting_failed", request, after={"errors": failure.messages}, ctx=ctx)
    recipients = {actor.pk: actor}
    for u in _mp_users(request.entity):
        recipients[u.pk] = u
    for user in recipients.values():
        notifications.notify(
            user, Notification.Kind.ALERT, alert_code="posting_failed", entity=request.entity, link=_link(request),
            title_en=f"Posting failed after approval: {request.summary_en}"[:200],
            title_ar=f"فشل الترحيل بعد الاعتماد: {request.summary_ar}"[:200],
            body_en=failure.reason, body_ar=failure.reason,
        )


def _act(request_id, actor, action, comment, document_version, on_behalf_of, ctx):
    if action not in {A.APPROVE, A.REJECT, A.RETURN}:
        raise RuleError("invalid_action", "Unknown action.", "إجراء غير معروف.")
    if not actor.is_human:
        # R18 hard check, regardless of roles.
        raise AccessDenied("service_user_cannot_act", "Service users cannot approve, reject or return.",
                           "لا يمكن لمستخدمي الخدمة الاعتماد أو الرفض أو الإعادة.")
    comment = (comment or "").strip()
    if action in {A.REJECT, A.RETURN} and not comment:
        raise RuleError("comment_required", "A comment is required to reject or return.",
                        "التعليق مطلوب عند الرفض أو الإعادة.")
    request = ApprovalRequest.objects.select_for_update().select_related("entity", "document_type", "policy").get(pk=request_id)
    if request.status != S.PENDING:
        raise Conflict("not_pending", f"This request is {request.status}.", f"حالة هذا الطلب: {request.get_status_display()}.")
    if document_version is not None and document_version != request.document_version:
        raise Conflict("stale_version", "You are looking at an old version of this request. Reload and try again.",
                       "أنت تطّلع على نسخة قديمة من هذا الطلب. أعد التحميل وحاول مجدداً.")
    elig = eligibility(request, actor, on_behalf_of=on_behalf_of)
    if elig is None:
        raise AccessDenied("not_eligible", "You are not an eligible approver for this step.",
                           "لست معتمداً مؤهلاً لهذه الخطوة.")

    adapter = adapter_for_code(request.document_type.code)
    doc = adapter.get_for_update(request.object_id)
    if adapter.document_version(doc) != request.document_version:
        _return_changed(request, adapter, doc, actor, ctx)  # R12: returned (kept), then 409
        return request, True

    at = now()
    before = snapshot(request)
    before_doc = adapter.snapshot(doc)
    ApprovalAction.objects.create(
        request=request, step=elig.step, actor=actor, on_behalf_of=elig.on_behalf_of, action=action,
        comment=comment, self_approval_override=elig.self_approval, at=at, channel=_channel(ctx),
    )
    if action == A.APPROVE:
        _advance(request, adapter, doc, actor, at, ctx)
    elif action == A.REJECT:
        request.status, request.decided_at = S.REJECTED, at
        request.save()
        adapter.on_rejected(doc, request)
        _notify_submitter(request, Notification.Kind.APPROVAL_DECIDED, "Rejected", "مرفوض", comment)
    else:
        request.status, request.decided_at = S.RETURNED, at
        request.save()
        adapter.on_returned(doc, request)
        _notify_submitter(request, Notification.Kind.APPROVAL_RETURNED, "Returned", "معاد", comment)

    audit_action = "approval.self_approval_override" if elig.self_approval else f"approval.{action}"
    audit(
        actor, audit_action, request, on_behalf_of=elig.on_behalf_of, ctx=ctx,
        before={"request": before, "document": before_doc},
        after={"request": snapshot(request), "document": adapter.snapshot(_reload(adapter, doc)),
               "comment": comment, "self_approval_override": elig.self_approval},
    )
    request.refresh_from_db()
    return request, False


def _reload(adapter, doc):
    return adapter.model.objects.get(pk=doc.pk)


def _advance(request, adapter, doc, actor, at, ctx):
    """R14: move to the next sequence when every step of the current one is done; final = approved + post (D11)."""
    if not all(_step_done(request, s) for s in _steps_at(request)):
        request.save()
        return
    next_seq = request.policy.steps.filter(sequence__gt=request.current_sequence).order_by("sequence") \
        .values_list("sequence", flat=True).first()
    if next_seq is not None:
        request.current_sequence = next_seq
        request.step_started_at = at
        request.save()
        _notify_step(request, ctx)
        return
    request.status, request.decided_at = S.APPROVED, at
    request.save()
    try:
        with transaction.atomic():
            adapter.on_approved(doc, request, actor)
    except Exception as exc:  # any failure of the owning module rolls the approval back (D11)
        raise _PostingError(request.pk, exc)
    _notify_submitter(request, Notification.Kind.APPROVAL_DECIDED, "Approved", "معتمد")


def _return_changed(request, adapter, doc, actor, ctx):
    before = snapshot(request)
    at = now()
    ApprovalAction.objects.create(
        request=request, actor=access.system_user(), action=A.RETURN, at=at, channel=ApprovalAction.Channel.SYSTEM,
        comment="Document changed after submission / تغيّر المستند بعد تقديمه",
    )
    request.status, request.decided_at = S.RETURNED, at
    request.save()
    adapter.on_returned(doc, request)
    audit(actor, "approval.returned_document_changed", request, before={"request": before},
          after={"request": snapshot(request)}, ctx=ctx)
    _notify_submitter(request, Notification.Kind.APPROVAL_RETURNED, "Returned (document changed)",
                      "معاد (تغيّر المستند)")


# ---------------------------------------------------------------- cancel (R13)


def _can_cancel(request, user):
    if request.status != S.PENDING or not user.is_active:
        return False
    if request.submitted_by_id == user.pk:
        return True
    return has_perm(user, request.entity, "approvals.request.cancel_any")  # stripped for agents (R18)


@transaction.atomic
def cancel(request, user, comment="", ctx=None):
    ctx = _ctx(ctx)
    request = ApprovalRequest.objects.select_for_update().get(pk=request.pk)
    if request.status != S.PENDING:
        raise Conflict("not_pending", f"This request is {request.status}.", f"حالة هذا الطلب: {request.get_status_display()}.")
    if not _can_cancel(request, user):
        raise AccessDenied("cannot_cancel", "Only the submitter can cancel this request.",
                           "يمكن لمقدّم الطلب فقط إلغاؤه.")
    adapter = adapter_for_code(request.document_type.code)
    doc = adapter.get_for_update(request.object_id)
    before = snapshot(request)
    at = now()
    ApprovalAction.objects.create(request=request, actor=user, action=A.CANCEL, comment=comment or "", at=at,
                                  channel=_channel(ctx))
    request.status, request.decided_at = S.CANCELLED, at
    request.save()
    adapter.on_cancelled(doc, request)
    audit(user, "approval.cancel", request, before={"request": before}, after={"request": snapshot(request)}, ctx=ctx)
    return request


# ---------------------------------------------------------------- escalation and expiry (R15)


@transaction.atomic
def run_escalations(at=None, ctx=None):
    """Run every 15 minutes (management command `run_escalations`). Returns (escalated, expired)."""
    at = at or now()
    ctx = ctx or Context(channel="system")
    sysuser = access.system_user()
    escalated = expired = 0
    for request in ApprovalRequest.objects.select_for_update().filter(status=S.PENDING).select_related("policy", "entity"):
        if request.expires_at and at >= request.expires_at:
            adapter = adapter_for_code(request.document_type.code)
            doc = adapter.get_for_update(request.object_id)
            before = snapshot(request)
            ApprovalAction.objects.create(request=request, actor=sysuser, action=A.EXPIRE, at=at,
                                          channel=ApprovalAction.Channel.SYSTEM)
            request.status, request.decided_at = S.EXPIRED, at
            request.save()
            adapter.on_expired(doc, request)
            audit(sysuser, "approval.expire", request, before={"request": before}, after={"request": snapshot(request)}, ctx=ctx)
            _notify_submitter(request, Notification.Kind.APPROVAL_DECIDED, "Expired", "منتهي الصلاحية")
            expired += 1
            continue
        for step in _steps_at(request):
            if not step.escalate_after_hours or _step_done(request, step):
                continue
            if not (step.escalate_to_role_id or step.escalate_to_user_id):
                continue
            if request.actions.filter(action=A.ESCALATE, step=step).exists():
                continue
            if at - request.step_started_at < datetime.timedelta(hours=step.escalate_after_hours):
                continue
            ApprovalAction.objects.create(
                request=request, step=step, actor=sysuser, action=A.ESCALATE, at=at,
                escalated_to_role=step.escalate_to_role, escalated_to_user=step.escalate_to_user,
                channel=ApprovalAction.Channel.SYSTEM,
            )
            audit(sysuser, "approval.escalate", request, ctx=ctx,
                  after={"step": step.pk, "to_role": step.escalate_to_role_id, "to_user": step.escalate_to_user_id})
            targets = set()
            if step.escalate_to_role_id:
                targets |= {u.pk for u in access.users_with_perm(request.entity, "approvals.request.act", role=step.escalate_to_role)}
            if step.escalate_to_user_id:
                targets.add(step.escalate_to_user_id)
            for user in User.objects.filter(pk__in=targets, is_active=True):
                notifications.notify(
                    user, Notification.Kind.ESCALATED, entity=request.entity, link=_link(request),
                    title_en=f"Escalated for your approval: {request.summary_en}"[:200],
                    title_ar=f"مُصعَّد لاعتمادك: {request.summary_ar}"[:200],
                    body_en=f"Not decided within {step.escalate_after_hours} hours.",
                    body_ar=f"لم يُبت فيه خلال {step.escalate_after_hours} ساعة.",
                )
            escalated += 1
    return escalated, expired


# ---------------------------------------------------------------- delegations (R16)


def _can_manage_delegation(user, delegator, entity):
    entities = [entity] if entity is not None else list(access.entities_with_perm(user, "core.entity.view"))
    if user.pk == delegator.pk:
        perm = "approvals.delegation.manage_own"
    else:
        perm = "approvals.delegation.manage_any"
    return bool(entities) and all(has_perm(user, e, perm) for e in entities)


@transaction.atomic
def create_delegation(user, *, delegator, delegate, starts_at, ends_at, entity=None, document_types=(),
                      amount_max=None, currency="SAR", reason="", ctx=None):
    if not user.is_human or not _can_manage_delegation(user, delegator, entity):
        raise AccessDenied("cannot_delegate", "You cannot create this delegation.", "لا يمكنك إنشاء هذا التفويض.")
    if not delegate.is_human or not delegate.is_active:
        raise RuleError("delegate_not_human", "Delegates must be active human users.",
                        "يجب أن يكون المفوَّض مستخدماً بشرياً نشطاً.")
    if delegate.pk == delegator.pk:
        raise RuleError("delegate_self", "You cannot delegate to yourself.", "لا يمكنك التفويض لنفسك.")
    if ends_at <= starts_at:
        raise RuleError("delegation_dates", "The delegation must end after it starts.", "يجب أن ينتهي التفويض بعد بدايته.")
    if entity is not None and not has_perm(delegate, entity, "approvals.request.view"):
        raise RuleError("delegate_no_access", "The delegate has no view access to this entity.",
                        "المفوَّض ليس لديه صلاحية عرض في هذه المنشأة.")
    d = Delegation.objects.create(
        delegator=delegator, delegate=delegate, entity=entity, amount_max=amount_max, currency=currency,
        starts_at=starts_at, ends_at=ends_at, reason=reason, created_by=user,
    )
    if document_types:
        d.document_types.set(document_types)
    audit(user, "delegation.create", d, entity=entity, after=snapshot(d) | {"document_types": [t.code for t in document_types]}, ctx=ctx)
    notifications.notify(
        delegate, Notification.Kind.DELEGATION_STARTED, entity=entity, link="/approvals?scope=delegated",
        title_en=f"{delegator.full_name_en} delegated approvals to you", title_ar=f"فوّضك {delegator.full_name_ar} بالاعتمادات",
        body_en=f"{starts_at:%Y-%m-%d} to {ends_at:%Y-%m-%d}", body_ar=f"من {starts_at:%Y-%m-%d} إلى {ends_at:%Y-%m-%d}",
    )
    return d


@transaction.atomic
def end_delegation(user, delegation, at=None, ctx=None):
    at = at or now()
    if not _can_manage_delegation(user, delegation.delegator, delegation.entity):
        raise AccessDenied("cannot_delegate", "You cannot change this delegation.", "لا يمكنك تعديل هذا التفويض.")
    before = snapshot(delegation)
    if delegation.ends_at > at:
        delegation.ends_at = max(at, delegation.starts_at + datetime.timedelta(seconds=1))
        delegation.save(update_fields=["ends_at"])
    audit(user, "delegation.end", delegation, entity=delegation.entity, before=before, after=snapshot(delegation), ctx=ctx)
    return delegation


def visible_delegations(user):
    if user is None or not user.is_authenticated:
        return Delegation.objects.none()
    q = Q(delegator=user) | Q(delegate=user) | Q(created_by=user)
    any_entities = access.entities_with_perm(user, "approvals.delegation.manage_any")
    q |= Q(entity__in=any_entities)
    return Delegation.objects.filter(q).distinct()


# ---------------------------------------------------------------- reports (D10)


def self_approval_report(viewer, year, month):
    """Monthly list of MP self-approvals for the Chairman and auditor (needs access.audit.view per entity)."""
    start = timezone.make_aware(datetime.datetime(year, month, 1))
    last = calendar.monthrange(year, month)[1]
    end = start + datetime.timedelta(days=last)
    entities = access.entities_with_perm(viewer, "access.audit.view")
    rows = (
        ApprovalAction.objects.filter(self_approval_override=True, at__gte=start, at__lt=end, request__entity__in=entities)
        .select_related("request", "request__entity", "request__document_type", "actor")
        .order_by("at")
    )
    return [
        {
            "at": a.at.isoformat(),
            "entity": a.request.entity.code,
            "document_type": a.request.document_type.code,
            "object_id": a.request.object_id,
            "request_id": a.request_id,
            "summary": {"en": a.request.summary_en, "ar": a.request.summary_ar},
            "amount": str(a.request.amount) if a.request.amount is not None else None,
            "currency": a.request.currency,
            "approver": a.actor.username,
            "comment": a.comment,
        }
        for a in rows
    ]


def summary(user, entity=None):
    """Dashboard / badge counts (R22: totals as Decimal strings per currency)."""
    mine = pending_for(user)
    if entity is not None:
        mine = [r for r in mine if r.entity_id == entity.pk]
    totals = {}
    for r in mine:
        if r.amount is not None:
            totals[r.currency] = totals.get(r.currency, Decimal("0")) + r.amount
    oldest = min((r.submitted_at for r in mine), default=None)
    return {
        "pending_for_me": len(mine),
        "totals": {c: str(v) for c, v in sorted(totals.items())},
        "oldest_submitted_at": oldest.isoformat() if oldest else None,
        "unread_alerts": Notification.objects.filter(user=user, kind=Notification.Kind.ALERT, read_at__isnull=True).count(),
    }


# ---------------------------------------------------------------- matrix management (R7)


def _require_policy_perm(user, entity):
    """Entity policy: approvals.policy.manage in that entity. Group-wide policy: in a root (parent) entity."""
    if not user.is_human:
        raise AccessDenied("permission_denied", "Service users cannot manage policies.",
                           "لا يمكن لمستخدمي الخدمة إدارة السياسات.")
    if entity is not None:
        access.require_perm(user, entity, "approvals.policy.manage")
        return
    roots = access.entities_with_perm(user, "approvals.policy.manage").filter(parent__isnull=True)
    if not roots.exists():
        raise AccessDenied("permission_denied", "Group-wide policies need policy rights in the parent entity.",
                           "السياسات على مستوى المجموعة تتطلب صلاحية في المنشأة الأم.")


POLICY_FIXED_FIELDS = {"entity", "entity_id", "document_type", "document_type_id", "currency"}


def manageable_policies(user):
    entities = access.entities_with_perm(user, "approvals.policy.manage")
    q = Q(entity__in=entities)
    if entities.filter(parent__isnull=True).exists():
        q |= Q(entity__isnull=True)
    return ApprovalPolicy.objects.filter(q).prefetch_related("steps")


@transaction.atomic
def create_policy(user, *, steps, ctx=None, **fields):
    """Add a tier / version to the matrix. steps: list of dicts with ApprovalStep fields."""
    _require_policy_perm(user, fields.get("entity"))
    if not steps:
        raise RuleError("policy_without_steps", "A policy needs at least one step.", "تحتاج السياسة إلى خطوة واحدة على الأقل.")
    policy = ApprovalPolicy(**fields)
    policy.full_clean(exclude=["steps"])
    policy.save()
    for s in steps:
        ApprovalStep.objects.create(policy=policy, **s)
    audit(user, "policy.create", policy, entity=policy.entity,
          after=snapshot(policy) | {"steps": [snapshot(s) for s in policy.steps.all()]}, ctx=ctx)
    return policy


@transaction.atomic
def update_policy(user, policy, *, ctx=None, **fields):
    """Unused policies can be edited; used ones only retired (is_active / valid_to)."""
    _require_policy_perm(user, policy.entity)
    fixed = set(fields) & POLICY_FIXED_FIELDS
    changed = {k for k in fixed if getattr(policy, k) != fields[k]}
    if changed:
        raise RuleError("policy_scope_fixed",
                        "Entity, document type and currency of a policy cannot change; create a new policy.",
                        "لا يمكن تغيير المنشأة أو نوع المستند أو العملة لسياسة؛ أنشئ سياسة جديدة.",
                        detail=sorted(changed))
    before = snapshot(policy)
    for k, v in fields.items():
        setattr(policy, k, v)
    policy.save()
    audit(user, "policy.update", policy, entity=policy.entity, before=before, after=snapshot(policy), ctx=ctx)
    return policy


def _policy_extra(policy):
    return {"steps": [snapshot(s) for s in policy.steps.all()]}


def check_policy_admin(user, policy, original=None):
    """Admin: permission on the policy's (new and old) scope; scope fields fixed after creation."""
    _require_policy_perm(user, policy.entity)
    if original is not None:
        _require_policy_perm(user, original.entity)
        changed = {f for f in ("entity_id", "document_type_id", "currency")
                   if getattr(original, f) != getattr(policy, f)}
        if changed:
            raise RuleError("policy_scope_fixed",
                            "Entity, document type and currency of a policy cannot change; create a new policy.",
                            "لا يمكن تغيير المنشأة أو نوع المستند أو العملة لسياسة؛ أنشئ سياسة جديدة.",
                            detail=sorted(changed))


def record_policy_change(user, policy, before, created, ctx=None):
    """Audit a policy saved through the admin, including its steps (R7, R19)."""
    audit(user, "policy.create" if created else "policy.update", policy, entity=policy.entity,
          before=before, after=snapshot(policy) | _policy_extra(policy), ctx=ctx)


@transaction.atomic
def delete_policy(user, policy, ctx=None):
    _require_policy_perm(user, policy.entity)
    before = snapshot(policy) | _policy_extra(policy)
    pk = policy.pk
    for step in list(policy.steps.all()):
        step.delete()
    policy.delete()
    audit(user, "policy.delete", None, entity=policy.entity, object_type="approvals.approvalpolicy",
          object_id=str(pk), before=before, ctx=ctx)
