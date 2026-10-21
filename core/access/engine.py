"""The one approval engine (access.md 3.10 and §10; A10, A25; R57 to R62, R69 to R83).

Every approvable document goes through here. ``core.access.require`` hands every **A** (approval) permission checked
for a document to :func:`authorise`, so no module approves outside the engine (R61, AT-67, AT-96):

1. the document's **approval request** is found or opened; opening it resolves the rules once and stores them
   (R77): the entity's effective matrix version and its rows for the activity, amount band and currency (R69,
   R71, R73), or the activity's **code default** when the entity has no row at all (R70);
2. the user must be eligible for the request's current step: through an own active membership **in that entity**
   (no cascade from the group parent, R64, R90) whose role the row names (or the named user, R72), or as a recorded
   **delegate** of an eligible person (R62, R74, R87, R88);
3. nobody decides two steps of one request (R75) or a document they benefit from (R82);
4. a creator, submitter, last modifier or ``drafted_for`` principal (R81) is accepted only as the entity's **top
   authority**, with self-approval switched on, for a permission that is not **NSA** (R79, R80); it is logged
   (R83). Everyone else is refused ``self_approval``;
5. a decision that is not the request's last one is recorded only through :func:`decide` (the engine inbox); a
   module called directly for such a document is refused ``approval_request_required``. The last decision returns
   the single-use, document-bound grant with which the module approves and posts (R30, R31, R61).

The engine posts nothing and changes no document; module services do (10.6). Module adapters (``register``) tell
the engine a document's activity, amount, currency, version, beneficiaries and approve / return services.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from core.models import Currency, Entity

from . import authority_defaults, catalogue
from .authority_models import (
    AccessAlert, ApprovalActivity, ApprovalDecision, ApprovalDelegation, ApprovalRequest, AssistantAssignment,
    AuthorityMatrixVersion, AuthorityRule, EntityGovernance, active_window_q,
)
from .models import AccessLog, Kind, Membership, Role

# Roles that never act as a delegate on their own (R88): observers and drafters.
NON_DELEGATE_ROLES = frozenset({"personal_assistant", "business_analyst", "internal_auditor", "external_auditor"})
DELEGATION_MAX_DAYS = 90  # OQ-45
DELEGATION_REMINDER_DAYS = 3  # R87

MESSAGES = {
    "no_authority_rule": (
        "{entity}'s authority matrix has no rule for {activity} of {amount}; it cannot be submitted until the matrix "
        "covers it (R71).",
        "لا تتضمن مصفوفة الصلاحيات في {entity} قاعدة لنشاط {activity_ar} بمبلغ {amount}؛ لا يمكن تقديمه حتى تغطيه "
        "المصفوفة.",
    ),
    "not_in_matrix": (
        "Under {entity}'s authority rules, you are not an approver of this step of {activity}.",
        "وفق قواعد الصلاحيات في {entity}، لست من معتمدي هذه المرحلة من {activity_ar}.",
    ),
    "self_approval": (
        "You created, submitted or changed this document (or it was drafted for you), so you cannot approve it. Only "
        "the entity's top authority may, where self-approval is allowed (R79).",
        "لا يمكنك اعتماد مستند أنشأته أو قدمته أو عدّلته أو أُعدّ لك. لا يعتمد ذلك إلا السلطة العليا للكيان حيث يُسمح "
        "بالاعتماد الذاتي.",
    ),
    "beneficiary": (
        "This document is about you or benefits you, so you cannot approve it; the next approver in the matrix does "
        "(R82).",
        "هذا المستند يخصك أو يعود بالنفع عليك، فلا يمكنك اعتماده؛ يعتمده المعتمد التالي في المصفوفة.",
    ),
    "same_person_two_steps": (
        "You already decided another step of this approval; each step needs a different person (R75).",
        "سبق أن قررت مرحلة أخرى من هذا الاعتماد؛ تحتاج كل مرحلة إلى شخص مختلف.",
    ),
    "delegation_not_allowed": (
        "This step of {activity} must be decided by the delegator in person; delegates are not eligible (R74).",
        "يجب أن يقرر المفوِّض هذه المرحلة من {activity_ar} بنفسه؛ لا يحق للمفوَّض ذلك.",
    ),
    "approval_request_required": (
        "This document needs {remaining} more approval step(s); decide it through the approval engine (R61).",
        "يحتاج هذا المستند إلى {remaining} مرحلة اعتماد أخرى؛ قرّر من خلال محرك الاعتماد.",
    ),
    "approval_request_closed": (
        "This approval request is closed; the document was returned, rejected or changed.",
        "طلب الاعتماد هذا مغلق؛ فقد أعيد المستند أو رُفض أو تغيّر.",
    ),
    "amount_decimals": (
        "{amount} has more decimals than {currency} allows ({places}).",
        "المبلغ {amount} يتجاوز عدد الخانات العشرية المسموح بها للعملة {currency} ({places}).",
    ),
}

_explicit = ContextVar("approval_engine_decide", default=None)
_purpose = ContextVar("approval_engine_purpose", default="approve")


class StepRecorded(Exception):
    """Raised inside :func:`decide` when a decision is not the request's last; :func:`decide` writes it after the
    module's work is rolled back (the module never acts on a partial approval)."""

    def __init__(self, payload):
        super().__init__("approval step recorded")
        self.payload = payload


@dataclass
class Outcome:
    """Result of :func:`decide`: the request, whether the document is now approved, and what the module returned."""

    request: ApprovalRequest
    final: bool
    decision: str
    result: object = None


# --- adapters ---------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Adapter:
    model_label: str
    activity: str
    permission: str | None = None
    amount: object = None  # callable(doc) -> Decimal | None, in the document's currency
    currency: object = None  # callable(doc) -> ISO code | None
    functional_amount: object = None  # callable(doc) -> Decimal | None, in the functional currency
    beneficiaries: object = None  # callable(doc) -> iterable of user ids (R82)
    people: object = None  # callable(doc) -> iterable of user ids who also count as its makers (R14, R79)
    version: object = None  # callable(doc) -> str
    label: object = None  # callable(doc) -> (en, ar), number and period only (no personal data)
    approve: object = None  # callable(doc, user) -> doc: the module's approval service
    reject: object = None  # callable(doc, user, note) -> doc
    return_: object = None  # callable(doc, user, note) -> doc
    entity_of: object = None  # callable(doc) -> Entity


_ADAPTERS = {}


def register(model, activity, **hooks):
    """Register an engine adapter for ``model`` (or its ``app_label.model`` label) under ``activity``.

    Called by the owning module in its ``AppConfig.ready()``. ``permission`` picks this adapter when one model is
    approved under several activities (e.g. a purification transfer: funding or payout)."""
    label = model if isinstance(model, str) else model._meta.label_lower
    authority_defaults.get(activity)  # unknown activity: a programming error
    adapter = Adapter(label, activity, **hooks)
    _ADAPTERS[(label, activity)] = adapter
    return adapter


def adapters():
    return dict(_ADAPTERS)


def _label_of(document):
    meta = getattr(document, "_meta", None)
    return getattr(meta, "label_lower", None) or type(document).__name__.lower()


def adapter_for(permission, document):
    """The adapter for ``document`` approved with ``permission``; a generic one (activity of the permission, no
    amount) for a model that registered none."""
    label = _label_of(document)
    candidates = [a for (lbl, _), a in _ADAPTERS.items() if lbl == label]
    if candidates:
        own = [a for a in candidates if a.permission == permission]
        if own:
            return own[0]
        by_perm = [a for a in candidates if permission in authority_defaults.get(a.activity).permissions]
        if by_perm:
            return by_perm[0]
    activities = authority_defaults.for_permission(permission)
    if not activities:
        raise KeyError(f"{permission} has no approval activity in core/access/authority_defaults.py (R61, AT-96).")
    return Adapter(label, activities[0].code, permission=permission)


# --- governance of an entity -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Governance:
    entity: Entity
    top_authority_role: str
    self_approval_enabled: bool
    recipients: frozenset
    stored: bool


def is_group_parent(entity):
    return entity.parent_id is None and entity.consolidation == Entity.Consolidation.PARENT


def default_top_authority(entity):
    """R67: ``managing_partner`` for a top-level group parent, ``ceo`` for every other entity."""
    return "managing_partner" if is_group_parent(entity) else "ceo"


def governance_of(entity):
    """The entity's governance settings; an entity created before its row exists gets the R67 defaults."""
    row = EntityGovernance.objects.filter(entity_id=entity.pk).select_related("top_authority_role").first()
    if row is None:
        top = default_top_authority(entity)
        return Governance(entity, top, True, frozenset({"internal_auditor", top}), False)
    return Governance(entity, row.top_authority_role.code, row.self_approval_enabled,
                      frozenset(row.governance_report_recipients.values_list("code", flat=True)), True)


def ensure_governance(entity):
    """Create the entity's governance row with the R67 defaults if it has none (create_entity, setup)."""
    row = EntityGovernance.objects.filter(entity_id=entity.pk).first()
    if row is not None:
        return row
    top = Role.objects.get(code=default_top_authority(entity))
    row = EntityGovernance(entity=entity, top_authority_role=top, self_approval_enabled=True)
    row._service_write = True
    row.save()
    row.governance_report_recipients.set(Role.objects.filter(code__in={"internal_auditor", top.code}))
    return row


# --- helpers -----------------------------------------------------------------------------------------------------

def _now():
    from . import services

    return services.now()


def _today():
    return timezone.localdate(_now())


def _services():
    from . import services

    return services


def _fmt(code, user, entity, permission, **values):
    en, ar = MESSAGES[code]
    return en.format(entity=getattr(entity, "code", ""), **values), ar.format(entity=getattr(entity, "code", ""),
                                                                              **values)


def refuse(code, user, entity, permission, details=None, **values):
    """Log a ``permission_denied`` row (R27) and raise AccessDenied with the engine's en/ar message."""
    s = _services()
    en, ar = _fmt(code, user, entity, permission, **values)
    s.log_denial(actor=user if getattr(user, "pk", None) else None, entity=entity, permission=permission,
                 details={"reason": code, **(details or {})})
    raise s.AccessDenied(code, en, ar, s._language(user))


def _activity_names(activity):
    spec = authority_defaults.get(activity)
    return spec.name_en, spec.name_ar


def _attr(document, *names):
    for name in names:
        value = getattr(document, name, None)
        if value is not None:
            return value
    return None


def _default_amount(document, activity):
    basis = authority_defaults.get(activity).amount_basis
    if basis == authority_defaults.NONE:
        return None
    if basis == authority_defaults.PRINCIPAL:
        return _attr(document, "principal", "sale_price", "amount", "total_gross")
    if basis == authority_defaults.AMOUNT:
        return _attr(document, "amount", "total_gross", "net_amount")
    return _attr(document, "total_gross", "vendor_total_gross", "amount")


def _decimal(value):
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


@dataclass
class DocInfo:
    adapter: Adapter
    activity: str
    label: str
    pk: int
    version: str
    amount: Decimal | None
    currency: str
    functional_amount: Decimal | None
    beneficiaries: frozenset
    label_en: str = ""
    label_ar: str = ""


def describe(document, permission, entity):
    """What the engine needs to know about ``document`` (through its adapter, else generic attributes)."""
    adapter = adapter_for(permission, document)
    activity = adapter.activity
    amount = _decimal(adapter.amount(document) if adapter.amount else _default_amount(document, activity))
    currency = (adapter.currency(document) if adapter.currency else None) or getattr(document, "currency_id", None) \
        or entity.base_currency_id
    amount = _at_places(amount, currency)
    if adapter.functional_amount:
        functional = _decimal(adapter.functional_amount(document))
    elif amount is None or currency == entity.base_currency_id:
        functional = amount
    else:
        functional = _to_functional(entity, amount, currency, document)
    if adapter.version:
        version = str(adapter.version(document))
    else:
        value = getattr(document, "version", None)
        if not isinstance(value, int):
            value = getattr(document, "submitted_at", None) or getattr(document, "modified_at", None) or ""
        version = str(value.isoformat() if hasattr(value, "isoformat") else value)
    people = frozenset(adapter.beneficiaries(document) or ()) if adapter.beneficiaries else frozenset()
    en = ar = ""
    if adapter.label:
        en, ar = adapter.label(document)
    return DocInfo(adapter, activity, _label_of(document), document.pk, version[:64], amount, currency, functional,
                   people, en[:200], ar[:200])


def _to_functional(entity, amount, currency, document):
    """The amount in the functional currency at the document's booking rate (R73, OQ-35): its pinned rate if it has
    one, else the ledger rate of its date."""
    rate = getattr(document, "fx_rate", None)
    if rate is None:
        from ledger.services import _lookup_rate

        on = _attr(document, "accounting_date", "issue_date", "date", "payment_date") or _today()
        found = _lookup_rate(entity, currency, entity.base_currency_id, on)
        rate = found.rate if found else None
    if rate is None:
        return None
    return (amount * Decimal(str(rate))).quantize(Decimal("0.01"))


def _places_of(currency_code):
    places = Currency.objects.filter(code=currency_code).values_list("minor_units", flat=True).first()
    return 2 if places is None else min(places, 3)


def _at_places(amount, currency_code):
    """A document amount shown at its currency's own decimals when that loses nothing (30000.000 -> 30000.00)."""
    if amount is None:
        return None
    q = amount.quantize(Decimal(1).scaleb(-_places_of(currency_code)))
    return q if q == amount else amount


def check_amount_places(amount, currency_code):
    """CLAUDE.md rule 7: an amount has at most its currency's own decimals (and the 3 stored)."""
    if amount is None:
        return None
    places = Currency.objects.filter(code=currency_code).values_list("minor_units", flat=True).first()
    places = 2 if places is None else min(places, 3)
    if amount != amount.quantize(Decimal(1).scaleb(-places)):
        return places
    return None


# --- matrix resolution -------------------------------------------------------------------------------------------

def effective_version(entity, on):
    """The matrix version in force for ``entity`` on date ``on``: the approved (or since superseded) version with the
    latest ``effective_from`` on or before ``on`` (R77). None: the entity has no matrix yet (every activity uses its
    code default)."""
    return (AuthorityMatrixVersion.objects.filter(
        entity=entity, effective_from__lte=on,
        status__in=(AuthorityMatrixVersion.Status.APPROVED, AuthorityMatrixVersion.Status.SUPERSEDED),
    ).order_by("-effective_from", "-version_no").first())


@dataclass
class Resolution:
    source: str
    version: AuthorityMatrixVersion | None
    steps: list = field(default_factory=list)

    @property
    def rule_ids(self):
        return [rid for s in self.steps for rid in s["rule_ids"]]


DEFAULT_STEPS = [{"step_no": 1, "min_approvals": 1, "rule_ids": [], "delegation_allowed": True}]


def resolve(entity, activity, info, on, *, user=None, permission=""):
    """Rules for one document (R69 to R73). Refused ``no_authority_rule`` for a gap inside the entity's bands."""
    version = effective_version(entity, on)
    rows = list(AuthorityRule.objects.filter(version=version, activity__code=activity).select_related("band_currency")
                ) if version else []
    if not rows:
        return Resolution(ApprovalRequest.Source.DEFAULT, None, [dict(s) for s in DEFAULT_STEPS])
    functional = entity.base_currency_id
    own = [r for r in rows if info.currency != functional and r.band_currency_id == info.currency]
    if own:
        candidates, measure, measured_in = own, info.amount, info.currency
    else:
        candidates = [r for r in rows if r.band_currency_id in (None, functional)]
        measure, measured_in = info.functional_amount, functional
    if measure is not None:
        places = check_amount_places(measure, measured_in)
        if places is not None:
            name_en, name_ar = _activity_names(activity)
            refuse("amount_decimals", user, entity, permission, amount=measure, currency=measured_in, places=places)
    if measure is None:
        matching = [r for r in candidates if r.amount_from == 0 and r.amount_to is None]
    else:
        matching = [r for r in candidates if r.amount_from <= measure and (r.amount_to is None or measure <= r.amount_to)]
    if not matching:
        name_en, name_ar = _activity_names(activity)
        amount = "no amount" if measure is None else f"{measured_in} {measure}"
        refuse("no_authority_rule", user, entity, permission,
               details={"activity": activity, "amount": None if measure is None else str(measure),
                        "currency": measured_in, "matrix_version": version.pk},
               activity=name_en, activity_ar=name_ar, amount=amount)
    steps = {}
    for r in matching:
        s = steps.setdefault(r.step_no, {"step_no": r.step_no, "min_approvals": 1, "rule_ids": [],
                                         "delegation_allowed": True})
        s["min_approvals"] = max(s["min_approvals"], r.min_approvals)
        s["rule_ids"].append(r.pk)
    return Resolution(ApprovalRequest.Source.MATRIX, version, [steps[k] for k in sorted(steps)])


# --- requests ----------------------------------------------------------------------------------------------------

def _drafted_for(entity, document, at):
    """R81: the principal(s) a personal assistant drafted ``document`` for (active assignment of its creator)."""
    creator = getattr(document, "created_by_id", None)
    if creator is None:
        return []
    return list(AssistantAssignment.objects.filter(active_window_q(at), entity=entity, assistant_id=creator)
                .values_list("principal_id", flat=True))


def activity_row(code):
    """The ``ApprovalActivity`` row of ``code``; created from the code catalogue if a flushed test database lacks it
    (the rows mirror core/access/authority_defaults.py; migration core.0027 seeds them)."""
    row = ApprovalActivity.objects.filter(code=code).first()
    if row is None:
        spec = authority_defaults.get(code)
        row, _ = ApprovalActivity.objects.get_or_create(
            code=code, defaults=dict(name_en=spec.name_en, name_ar=spec.name_ar, permission=spec.permission,
                                     amount_basis=spec.amount_basis, is_pending=spec.pending))
    return row


def _save(obj):
    obj._service_write = True
    obj.save()
    return obj


def find_request(entity, info):
    return (ApprovalRequest.objects.filter(entity=entity, document_type=info.label, document_id=info.pk,
                                           activity__code=info.activity, document_version=info.version)
            .order_by("-opened_at", "-id").first())


def _open_request(entity, info, user, at, *, submitted_by=None):
    """Open the request for this document version, resolving its rules now (R77); older open requests of the same
    document are superseded (a returned and resubmitted document resolves again)."""
    resolution = resolve(entity, info.activity, info, timezone.localdate(at), user=user,
                         permission=authority_defaults.get(info.activity).permission)
    for old in ApprovalRequest.objects.filter(entity=entity, document_type=info.label, document_id=info.pk,
                                              activity__code=info.activity, status=ApprovalRequest.Status.OPEN
                                              ).exclude(document_version=info.version):
        old.status, old.closed_at = ApprovalRequest.Status.SUPERSEDED, at
        _save(old)
    principals = _drafted_for(entity, info.document, at) if getattr(info, "document", None) is not None else []
    req = ApprovalRequest(
        entity=entity, activity=activity_row(info.activity), document_type=info.label,
        document_id=info.pk, document_version=info.version, label_en=info.label_en, label_ar=info.label_ar,
        amount=info.amount, currency=info.currency or "", functional_amount=info.functional_amount,
        matrix_version=resolution.version, rule_source=resolution.source, rule_ids=resolution.rule_ids,
        steps=resolution.steps, drafted_for_id=principals[0] if len(principals) == 1 else None,
        submitted_by=submitted_by, opened_at=at,
    )
    _save(req)
    _services().log(AccessLog.Event.APPROVAL_SUBMITTED, actor=submitted_by or user, entity=entity,
                    permission=authority_defaults.get(info.activity).permission,
                    details={"request": req.pk, "activity": info.activity, "document": [info.label, info.pk],
                             "document_version": info.version, "rule_source": req.rule_source,
                             "matrix_version": req.matrix_version_id, "rule_ids": req.rule_ids,
                             "amount": None if info.amount is None else str(info.amount), "currency": req.currency})
    _alert_if_no_eligible(req, at)
    return req


def submit(document, user, *, permission=None, entity=None):
    """Open the approval request when a module submits a document (R61, R77). A gap in the entity's bands is refused
    here (``no_authority_rule``), inside the module's transaction, so the document stays a draft (R71)."""
    entity = entity or document.entity
    permission = permission or _main_permission(document)
    info = describe(document, permission, entity)
    info.document = document
    at = _now()
    existing = find_request(entity, info)
    if existing is not None and existing.status == ApprovalRequest.Status.OPEN:
        return existing
    return _open_request(entity, info, user, at, submitted_by=user)


def _main_permission(document):
    label = _label_of(document)
    for (lbl, activity), adapter in _ADAPTERS.items():
        if lbl == label:
            return adapter.permission or authority_defaults.get(activity).permission
    raise KeyError(f"{label} has no approval adapter (R61).")



# --- eligibility -------------------------------------------------------------------------------------------------

def _direct_memberships(user, entity, permission, at):
    """Active memberships giving ``permission`` **in** ``entity`` itself: no group cascade (R64, R90)."""
    return [m for m in _services()._memberships_for(user, entity, permission, at) if m.entity_id == entity.pk]


def _role_codes(memberships):
    return {m.role.code for m in memberships}


def _rules_of(step):
    return list(AuthorityRule.objects.filter(pk__in=step["rule_ids"]).prefetch_related("approver_roles"))


def _eligible_by_rules(user, entity, permission, step, rule_source, activity, at):
    """(memberships, rule) through which ``user`` is eligible for ``step`` by their own memberships, else ([], None)."""
    ms = _direct_memberships(user, entity, permission, at)
    if not ms:
        return [], None
    if rule_source == ApprovalRequest.Source.DEFAULT:
        spec = authority_defaults.get(activity).default
        if spec.top_authority:
            roles = {governance_of(entity).top_authority_role}
        elif spec.roles is not None:
            roles = set(spec.roles)
        else:
            return ms, None
        own = [m for m in ms if m.role.code in roles]
        return own, None
    for rule in _rules_of(step):
        if rule.approver_user_id is not None:
            if rule.approver_user_id == user.pk:
                return ms, rule
            continue
        roles = set(rule.approver_roles.values_list("code", flat=True))
        own = [m for m in ms if m.role.code in roles]
        if own:
            return own, rule
    return [], None


def _human_member(user, entity, at):
    """Active memberships of ``user`` in ``entity`` (own, not cascaded) through human roles."""
    return list(Membership.objects.active(at).filter(user=user, entity=entity, role__kind=Kind.HUMAN)
                .select_related("role"))


def _delegation_eligibility(user, entity, permission, step, rule_source, info, at):
    """(delegation, delegator memberships, rule, refusal code) for ``user`` acting as a delegate (R62, R74, R88)."""
    perm = catalogue.get(permission)
    if perm.is_dual_control or perm.is_segregated or perm.is_group_only:  # R88: never NSA rows c and d (nor G)
        return None, [], None, None
    from .services import kind_of

    if kind_of(user) != Kind.HUMAN:
        return None, [], None, None
    own = _human_member(user, entity, at)
    if not own or not {m.role.code for m in own} - NON_DELEGATE_ROLES:
        return None, [], None, None
    candidates = ApprovalDelegation.objects.filter(active_window_q(at), entity=entity, delegate=user).order_by("pk")
    refusal = None
    for d in candidates:
        codes = set(d.activities.values_list("code", flat=True))
        if codes and info.activity not in codes:
            continue
        if d.amount_max is not None and (info.functional_amount is None or info.functional_amount > d.amount_max):
            refusal = refusal or "not_in_matrix"
            continue
        ms, rule = _eligible_by_rules(d.delegator, entity, permission, step, rule_source, info.activity, at)
        if not ms:
            continue
        if rule is not None and not rule.delegation_allowed:
            refusal = "delegation_not_allowed"
            continue
        if rule is None and rule_source == ApprovalRequest.Source.MATRIX:
            continue
        return d, ms, rule, None
    return None, [], None, refusal


def eligible_users(entity, permission, step, rule_source, activity, at):
    """User ids eligible for ``step`` through their own memberships (for alerts and beneficiary skipping)."""
    qs = (Membership.objects.active(at).filter(entity=entity, role__permission_rows__codename=permission,
                                               role__kind=Kind.HUMAN).select_related("role", "user").distinct())
    users = {}
    for m in qs:
        users.setdefault(m.user_id, m.user)
    return {uid for uid, u in users.items()
            if _eligible_by_rules(u, entity, permission, step, rule_source, activity, at)[0]}


def _alert_if_no_eligible(req, at):
    """R72: a step with no eligible approver waits (R18); the top authority and the access admins are told."""
    if req.rule_source != ApprovalRequest.Source.MATRIX:
        return
    permission = req.activity.permission
    if permission not in catalogue.CATALOGUE:
        return
    for step in req.steps:
        if eligible_users(req.entity, permission, step, req.rule_source, req.activity.code, at):
            continue
        gov = governance_of(req.entity)
        recipients = set(Membership.objects.active(at).filter(
            entity=req.entity, role__code__in=(gov.top_authority_role, "access_admin")).values_list("user_id", flat=True))
        name_en, name_ar = _activity_names(req.activity.code)
        for uid in recipients:
            alert(req.entity, uid, AccessAlert.Kind.NO_ELIGIBLE_APPROVER,
                  f"{req.entity.code}: step {step['step_no']} of {name_en} ({req.document_type} {req.document_id}) has "
                  "no eligible approver under the authority matrix; it waits until the matrix or the memberships "
                  "change (R72).",
                  f"{req.entity.code}: المرحلة {step['step_no']} من {name_ar} ({req.document_type} {req.document_id}) "
                  "لا يوجد لها معتمد مؤهل وفق مصفوفة الصلاحيات؛ تنتظر حتى تتغير المصفوفة أو العضويات.",
                  key=f"no_eligible:{req.pk}:{step['step_no']}:{uid}", details={"request": req.pk,
                                                                                 "step_no": step["step_no"]})


def alert(entity, user_id, kind, en, ar, *, key, details=None):
    """Write one bilingual in-app alert once (``key``), logged."""
    if AccessAlert.objects.filter(key=key).exists():
        return None
    row = AccessAlert.objects.create(entity=entity, recipient_id=user_id, kind=kind, message_en=en, message_ar=ar,
                                     key=key, details=details or {})
    _services().log(AccessLog.Event.ALERT_SENT, entity=entity, subject_user=row.recipient,
                    details={"alert": row.pk, "kind": kind, **(details or {})}, source="system:access")
    return row


# --- progress ----------------------------------------------------------------------------------------------------

def _approvals_by_step(req):
    done = {}
    for d in req.decisions.filter(decision__in=(ApprovalDecision.Decision.APPROVED, ApprovalDecision.Decision.SKIPPED)):
        done.setdefault(d.step_no, []).append(d)
    return done


def _current_step(req):
    """(step, index) of the first step not yet complete, or (last step, index) when every step is complete."""
    done = _approvals_by_step(req)
    for i, step in enumerate(req.steps):
        rows = done.get(step["step_no"], [])
        if any(d.decision == ApprovalDecision.Decision.SKIPPED for d in rows):
            continue
        if len({d.user_id for d in rows}) < step["min_approvals"]:
            return step, i, False
    return req.steps[-1], len(req.steps) - 1, True


def _remaining_after(req, step_index, adding_user):
    done = _approvals_by_step(req)
    remaining = 0
    for i, step in enumerate(req.steps):
        rows = done.get(step["step_no"], [])
        if any(d.decision == ApprovalDecision.Decision.SKIPPED for d in rows):
            continue
        people = {d.user_id for d in rows}
        if i == step_index:
            people.add(adding_user)
        remaining += max(0, step["min_approvals"] - len(people))
    return remaining


def _skip_beneficiary_steps(req, info, permission, at):
    """R82: a matrix step whose every eligible approver benefits from the document passes to the next step."""
    if req.rule_source != ApprovalRequest.Source.MATRIX or not info.beneficiaries:
        return
    while True:
        step, index, complete = _current_step(req)
        if complete or index == len(req.steps) - 1:
            return
        eligible = eligible_users(req.entity, permission, step, req.rule_source, req.activity.code, at)
        if not eligible or not eligible <= set(info.beneficiaries):
            return
        _save(ApprovalDecision(request=req, entity=req.entity, step_no=step["step_no"],
                               decision=ApprovalDecision.Decision.SKIPPED, rule_source=req.rule_source,
                               permission=permission, note="every eligible approver is the beneficiary (R82)", at=at))


# --- the gate used by require() ------------------------------------------------------------------------------------

@dataclass
class Authorisation:
    memberships: list
    request: ApprovalRequest
    step: dict
    rule: AuthorityRule | None
    delegation: ApprovalDelegation | None
    self_approval: bool
    top_authority: bool
    final: bool
    info: DocInfo
    already_decided: bool = False


def document_people(entity, document, at):
    """Creator, submitter, last modifier, plus the principals an assistant drafted it for (R14, R81)."""
    people = set(_services()._document_people(document))
    people |= set(_drafted_for(entity, document, at))
    return people


def _top_authority_ok(user, entity, permission, memberships, at):
    """R79 (a), (b), (d) and OQ-47: the user holds the entity's top-authority role in it, it has exactly one active
    holder, self-approval is on, and the permission is not NSA."""
    if catalogue.get(permission).never_self_approved:
        return False, "nsa"
    gov = governance_of(entity)
    if not gov.self_approval_enabled:
        return False, "self_approval_disabled"
    if gov.top_authority_role not in _role_codes(memberships):
        return False, "not_top_authority"
    holders = set(Membership.objects.active(at).filter(entity=entity, role__code=gov.top_authority_role)
                  .values_list("user_id", flat=True))
    if holders != {user.pk}:
        return False, "top_authority_not_single"
    return True, None


def authorise(user, entity, permission, document, *, at):
    """Decide whether ``user`` may approve ``document`` with ``permission`` now (called by ``require``)."""
    info = describe(document, permission, entity)
    info.document = document
    name_en, name_ar = _activity_names(info.activity)
    req = find_request(entity, info)
    if req is None or req.status == ApprovalRequest.Status.SUPERSEDED:
        req = _open_request(entity, info, user, at)
    elif req.status in (ApprovalRequest.Status.REJECTED, ApprovalRequest.Status.RETURNED):
        req = _open_request(entity, info, user, at)
    if _purpose.get() == "approve" and user.pk in info.beneficiaries:  # R82: before any step, for every role
        refuse("beneficiary", user, entity, permission, {"request": req.pk, "activity": info.activity})
    _skip_beneficiary_steps(req, info, permission, at)
    step, index, complete = _current_step(req)
    memberships, rule = _eligible_by_rules(user, entity, permission, step, req.rule_source, info.activity, at)
    delegation = None
    if not memberships:
        delegation, memberships, rule, why = _delegation_eligibility(user, entity, permission, step, req.rule_source,
                                                                     info, at)
        if delegation is None:
            details = {"activity": info.activity, "request": req.pk, "step_no": step["step_no"],
                       "rule_source": req.rule_source}
            if why == "delegation_not_allowed":
                refuse("delegation_not_allowed", user, entity, permission, details, activity=name_en,
                       activity_ar=name_ar)
            held = _services()._memberships_for(user, entity, permission, at)
            if not held:
                _services()._deny(user, entity, permission, "no_membership")
            if not [m for m in held if m.entity_id == entity.pk]:  # only through the group parent: no cascade (R64)
                _services()._deny(user, entity, permission, "no_membership")
            refuse("not_in_matrix", user, entity, permission, details, activity=name_en, activity_ar=name_ar)
    decided = {d.step_no for d in req.decisions.filter(user=user, decision=ApprovalDecision.Decision.APPROVED)}
    already = step["step_no"] in decided
    approving = _purpose.get() == "approve"
    if approving and decided - {step["step_no"]}:
        refuse("same_person_two_steps", user, entity, permission, {"request": req.pk, "step_no": step["step_no"]})
    people = document_people(entity, document, at)
    if info.adapter.people:
        people |= {uid for uid in info.adapter.people(document) if uid is not None}
    self_approval = user.pk in people
    top = False
    if self_approval:
        reason = "delegate" if delegation is not None else None
        if reason is None:
            ok, reason = _top_authority_ok(user, entity, permission, memberships, at)
            top = ok
        if not top:
            refuse("self_approval", user, entity, permission,
                   {"activity": info.activity, "request": req.pk, "why": reason,
                    "document": [info.label, info.pk]})
    remaining = 0 if (complete or already) else _remaining_after(req, index, user.pk)
    final = remaining == 0
    return Authorisation(memberships, req, step, rule, delegation, self_approval, top, final, info, already)


def record(auth, user, permission, grant_log, at):
    """Write the decision of a granted authorisation (final, or a step inside :func:`decide`) and the R83 log row."""
    req, info = auth.request, auth.info
    purpose = _purpose.get()
    if purpose in ("return", "reject") and req.status == ApprovalRequest.Status.OPEN:
        status = ApprovalRequest.Status.RETURNED if purpose == "return" else ApprovalRequest.Status.REJECTED
        _save(ApprovalDecision(request=req, entity=req.entity, step_no=auth.step["step_no"], decision=status, user=user,
                               rule_source=req.rule_source, permission=permission, grant_log=grant_log, at=at))
        req.status, req.closed_at = status, at
        _save(req)
        _services().log(AccessLog.Event.APPROVAL_DECIDED, actor=user, entity=req.entity, permission=permission, at=at,
                        details={"request": req.pk, "step_no": auth.step["step_no"], "decision": status})
        return None
    if purpose != "approve":
        return None
    decision = None
    if not auth.already_decided:
        decision = _save(ApprovalDecision(
            request=req, entity=req.entity, step_no=auth.step["step_no"], decision=ApprovalDecision.Decision.APPROVED,
            user=user, on_behalf_of=auth.delegation.delegator if auth.delegation else None, delegation=auth.delegation,
            rule_id=auth.rule.pk if auth.rule else None, rule_source=req.rule_source,
            self_approval=auth.self_approval, top_authority=auth.top_authority, permission=permission,
            grant_log=grant_log, at=at))
    if auth.final:
        req.status, req.closed_at = ApprovalRequest.Status.APPROVED, at
    if auth.self_approval:
        req.self_approval = True
        req.top_authority = req.top_authority or auth.top_authority
    _save(req)
    s = _services()
    s.log(AccessLog.Event.APPROVAL_DECIDED, actor=user, entity=req.entity, permission=permission, at=at,
          details={"request": req.pk, "step_no": auth.step["step_no"], "decision": "approved", "final": auth.final,
                   "rule_source": req.rule_source, "rule_id": auth.rule.pk if auth.rule else None,
                   "on_behalf_of": auth.delegation.delegator_id if auth.delegation else None,
                   "delegation": auth.delegation.pk if auth.delegation else None,
                   "grant_log": grant_log.pk if grant_log else None})
    if auth.self_approval and auth.top_authority and not auth.already_decided:
        role = governance_of(req.entity).top_authority_role
        s.log(AccessLog.Event.SELF_APPROVAL, actor=user, entity=req.entity, permission=permission, at=at,
              role=Role.objects.filter(code=role).first(),
              details={"activity": info.activity, "document": [info.label, info.pk],
                       "document_version": info.version, "amount": None if info.amount is None else str(info.amount),
                       "currency": info.currency, "matrix_version": req.matrix_version_id or "default",
                       "rule_id": auth.rule.pk if auth.rule else None, "role": role, "request": req.pk,
                       "drafted_for": req.drafted_for_id, "grant_log": grant_log.pk if grant_log else None,
                       "label_en": req.label_en, "label_ar": req.label_ar})
    return decision


def step_pending(auth, user, permission):
    """A non-final decision: recorded by :func:`decide`; refused for a direct module call (R61)."""
    if _purpose.get() != "approve":
        return
    if _explicit.get() is not None:
        raise StepRecorded({"auth": auth, "user": user, "permission": permission})
    remaining = _remaining_after(auth.request, auth.request.steps.index(auth.step), user.pk)
    refuse("approval_request_required", user, auth.request.entity, permission,
           {"request": auth.request.pk, "remaining": remaining}, remaining=remaining)


@contextmanager
def purpose(name):
    """Mark a module's use of an approval permission that is not an approval (``return``, ``reject``, ``check``):
    eligibility is checked, nothing is recorded as approved."""
    token = _purpose.set(name)
    try:
        yield
    finally:
        _purpose.reset(token)


# --- the engine inbox: decide ------------------------------------------------------------------------------------

def decide(document, user, *, approve=True, note="", permission=None, action=None):
    """Decide the current step of ``document``'s approval as ``user`` (the engine inbox, R61).

    ``approve=True`` calls the module's approval service through its adapter: a decision that completes the request
    lets the module approve (and post) with the grant; an earlier step is recorded and the module does nothing.
    ``approve=False`` returns (``action="return"``, default) or rejects (``action="reject"``) through the module.
    """
    permission = permission or _main_permission(document)
    entity = document.entity
    info = describe(document, permission, entity)
    adapter = info.adapter
    if approve:
        if adapter.approve is None:
            raise KeyError(f"{info.label} registered no approve service with the engine (R61).")
        token = _explicit.set(True)
        try:
            with transaction.atomic():
                try:
                    with transaction.atomic():
                        result = adapter.approve(document, user)
                except StepRecorded as step:
                    auth, actor = step.payload["auth"], step.payload["user"]
                    req = ApprovalRequest.objects.get(pk=auth.request.pk) if ApprovalRequest.objects.filter(
                        pk=auth.request.pk).exists() else None
                    if req is None:  # the request was opened inside the rolled-back call: open it again
                        info.document = document
                        req = _open_request(entity, info, actor, _now())
                    auth.request = req
                    at = _now()
                    record(auth, actor, step.payload["permission"], None, at)
                    return Outcome(req, False, "approved", None)
        finally:
            _explicit.reset(token)
        req = find_request(entity, info) or ApprovalRequest.objects.filter(
            entity=entity, document_type=info.label, document_id=info.pk, activity__code=info.activity,
        ).order_by("-opened_at", "-id").first()
        return Outcome(req, True, "approved", result)
    action = action or "return"
    hook = adapter.return_ if action == "return" else adapter.reject
    with transaction.atomic():
        result = None
        if hook is not None:
            result = hook(document, user, note)
        req = find_request(entity, info)
        if req is not None and req.status == ApprovalRequest.Status.OPEN:
            with purpose("check"):
                _services().require(user, entity, permission, document=document)
            status = ApprovalRequest.Status.RETURNED if action == "return" else ApprovalRequest.Status.REJECTED
            _save(ApprovalDecision(request=req, entity=entity, step_no=_current_step(req)[0]["step_no"],
                                   decision=status, user=user, rule_source=req.rule_source, permission=permission,
                                   note=note[:500]))
            req.status, req.closed_at = status, _now()
            _save(req)
            _services().log(AccessLog.Event.APPROVAL_DECIDED, actor=user, entity=entity, permission=permission,
                            details={"request": req.pk, "decision": status, "note": note[:500]})
        return Outcome(req, False, action, result)


def inbox(user, *, at=None):
    """Open requests whose current step ``user`` may decide through their own memberships, per entity (R2)."""
    at = at or _now()
    out = []
    for req in ApprovalRequest.objects.filter(status=ApprovalRequest.Status.OPEN).select_related("entity", "activity"):
        permission = req.activity.permission
        if permission not in catalogue.CATALOGUE:
            continue
        step, _, _ = _current_step(req)
        if _eligible_by_rules(user, req.entity, permission, step, req.rule_source, req.activity.code, at)[0]:
            out.append(req)
    return out


# --- module seams --------------------------------------------------------------------------------------------------

def grant_rule_source(grant):
    """``matrix`` or ``default``: which rules the engine applied for ``grant`` (payroll's seam, R70)."""
    return getattr(grant, "rule_source", "") or ""



def top_authority_holders(entity, at=None):
    at = at or _now()
    role = governance_of(entity).top_authority_role
    return set(Membership.objects.active(at).filter(entity=entity, role__code=role).values_list("user_id", flat=True))


