"""The approval inbox of the web UI, as access-engine services (web.md R39, R40, R6 to R8; access §10, A10, R61).

The web layer calls only these functions; it never reads or writes approval requests, decisions or documents itself.

* :func:`pending` lists the open approval requests **of one entity** whose current step the user may decide now:
  through an own membership the request's rules name (no group cascade, R64) or as a recorded delegate (R62, R74,
  R88). Requests the user made, submitted, last changed, had drafted for them (R79, R81) or benefits from (R82) are
  left out unless the engine would accept them (the single top authority's logged self-approval). This filtering is
  a hint only (web.md R6, R39): :func:`decide` hands every decision to ``engine.decide``, which applies every rule
  again through ``require`` and refuses (and logs) what is not allowed.
* :func:`item` is one request of the entity, readable by a user who may decide it, or who holds the request's
  approval right in that entity (e.g. its maker, who sees it read-only); anything else is 404 (R2, web.md R3).
* :func:`decide` approves, or sends back (return / reject, with the reason in Arabic, English optional, web.md R8),
  after checking that the request is still open and the document version is the one shown (web.md R7).

Labels and amounts come from the request (written by the engine from the module adapters: type and number, never a
person's name), so the inbox shows no personal data (PDPL; web.md R24).
"""
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404

from . import catalogue, engine
from . import services as access
from .authority_models import ApprovalDecision, ApprovalRequest

MESSAGES = {
    "document_changed": (
        "This document changed after you opened it, or its approval request was closed. Reload it and review it "
        "again.",
        "تغيّر هذا المستند بعد أن فتحته، أو أُغلق طلب اعتماده. أعد تحميله وراجعه مرة أخرى.",
    ),
    "no_version": (
        "This document has no version to check, so it cannot be decided from the inbox; its module must supply one.",
        "لا يوجد إصدار لهذا المستند للتحقق منه، فلا يمكن البت فيه من صندوق الاعتمادات؛ يجب أن توفره وحدته.",
    ),
    "no_send_back": (
        "This document type cannot be sent back from the inbox yet; approve it or leave it for its preparer.",
        "لا يمكن إعادة هذا النوع من المستندات من صندوق الاعتمادات بعد؛ اعتمده أو اتركه لمُعدّه.",
    ),
    "reason_required": (
        "Give the reason in Arabic (English is optional).",
        "اكتب السبب بالعربية (الإنجليزية اختيارية).",
    ),
}


class InboxRefused(ValidationError):
    """A refusal of the inbox itself (not of the access layer), with its English and Arabic message."""

    def __init__(self, code):
        self.code_name = code
        self.message_en, self.message_ar = MESSAGES[code]
        super().__init__(f"{self.message_en} / {self.message_ar}", code=code)


@dataclass
class InboxItem:
    request: ApprovalRequest
    document: object
    eligibility: engine.Eligibility
    activity_en: str = ""
    activity_ar: str = ""
    send_back: str | None = None
    steps_total: int = 1
    age_days: int = 0
    source_kind: str = ""  # human / agent / system: who submitted it (web.md R39 source badge)
    decisions: list = field(default_factory=list)

    @property
    def can_decide(self):
        return self.eligibility.ok

    @property
    def delegator(self):
        d = self.eligibility.delegation
        return d.delegator if d is not None else None

    @property
    def version(self):
        return self.request.document_version


def _activity_names(request):
    act = request.activity
    return act.name_en, act.name_ar


def _open_requests(entity):
    return (ApprovalRequest.objects.filter(entity=entity, status=ApprovalRequest.Status.OPEN)
            .select_related("entity", "activity", "submitted_by", "submitted_by__access_profile")
            .order_by("opened_at", "id"))


def _item(user, req, at, *, with_history=False):
    document = engine.document_of(req)
    if document is None:
        return None
    eligibility = engine.can_decide(user, req, document, at=at)
    en, ar = _activity_names(req)
    item = InboxItem(req, document, eligibility, en, ar, steps_total=len(req.steps or []) or 1,
                     age_days=max((at - req.opened_at).days, 0),
                     source_kind=access.kind_of(req.submitted_by) if req.submitted_by_id else "")
    if with_history:
        item.decisions = list(req.decisions.select_related("user", "user__access_profile", "on_behalf_of",
                                                           "on_behalf_of__access_profile").order_by("at", "id"))
        item.send_back = engine.send_back_action(req, document) if req.status == ApprovalRequest.Status.OPEN else None
    return item


def pending(user, entity, *, at=None):
    """Open requests of ``entity`` whose current step ``user`` may decide now (own membership or delegation), oldest
    first. Not filtered by any view right: eligibility is the engine's (R61). Agent and system users get nothing:
    they never use the web inbox (access R20, R21)."""
    if not access._is_active_human(user):
        return []
    at = at or access.now()
    out = []
    for req in _open_requests(entity):
        item = _item(user, req, at)
        if item is not None and item.can_decide:
            out.append(item)
    return out


def _readable(user, entity, req, item):
    if item.can_decide:
        return True
    permission = req.activity.permission
    if permission not in catalogue.CATALOGUE:
        return False
    return access.has(user, entity, permission)


def item(user, entity, pk, *, at=None):
    """One approval request of ``entity`` with its decisions (history). 404 when the request is in another entity,
    its document is gone, or the user may neither decide it nor holds its approval right in this entity."""
    if not access._is_active_human(user):
        raise Http404("Not found / غير موجود")
    at = at or access.now()
    req = (ApprovalRequest.objects.filter(entity=entity, pk=pk)
           .select_related("entity", "activity", "submitted_by", "submitted_by__access_profile").first())
    if req is None:
        raise Http404("Not found / غير موجود")
    found = _item(user, req, at, with_history=True)
    if found is None or not _readable(user, entity, req, found):
        raise Http404("Not found / غير موجود")
    return found


def _locked_request(entity, pk, version):
    req = (ApprovalRequest.objects.select_for_update(of=("self",)).filter(entity=entity, pk=pk)
           .select_related("entity", "activity").first())
    if req is None:
        raise Http404("Not found / غير موجود")
    document = engine.document_of(req)
    if document is None:
        raise Http404("Not found / غير موجود")
    if not req.document_version or not str(version or ""):
        raise InboxRefused("no_version")  # web.md R7: nothing to compare, so nothing is decided (review item 5)
    if req.status != ApprovalRequest.Status.OPEN or str(version) != req.document_version \
            or engine.current_version(req, document) != req.document_version:
        raise InboxRefused("document_changed")  # web.md R7: reload and review again
    return req, document


def decide(user, entity, pk, *, version, approve, reason_ar="", reason_en=""):
    """Decide request ``pk`` of ``entity`` as ``user`` through the engine (R61).

    ``version`` is the document version the user reviewed (web.md R7). ``approve=False`` sends the document back
    the way its module allows (return or reject; refused when the module has no such service yet, OQ-12) and needs
    ``reason_ar`` (web.md R8, OQ-12 default; ``reason_en`` optional). Every engine rule (four-eyes, self-approval,
    delegation, beneficiary, one person per request, matrix steps) is applied by ``engine.decide`` and the module's
    service; a refusal raises ``AccessDenied`` and is logged there.
    """
    reason_ar, reason_en = (reason_ar or "").strip(), (reason_en or "").strip()
    with transaction.atomic():
        req, document = _locked_request(entity, pk, version)
        permission = req.activity.permission
        if approve:
            return engine.decide(document, user, approve=True, permission=permission)
        action = engine.send_back_action(req, document)
        if action is None:
            raise InboxRefused("no_send_back")
        if not reason_ar:
            raise InboxRefused("reason_required")
        return engine.decide(document, user, approve=False, action=action, permission=permission, note=reason_ar,
                             note_en=reason_en)


def decision_labels():
    """``{decision value: "English / العربية"}`` for the history (model choices, bilingual)."""
    return dict(ApprovalDecision.Decision.choices)
