"""Payroll's seam to the one approval engine (access.md §10, R70; payroll.md §11 R82 to R84).

Every payroll approval permission is an **A** permission, so ``core.access.require`` hands it to the approval engine
(``core.access.engine``): the entity's authority matrix decides who approves, and where the entity has no row for
the activity the engine applies the activity's **code default** (``core/access/authority_defaults.py``):

- ``payroll.run``: ``hr_officer`` in every entity (P14); never the managing partner ("contracted payroll needs no
  Managing Partner approval", P20(a)).
- ``payroll.overtime``: the entity's hr route (``HrSettings.overtime_approval_route``, data): ``managing_partner`` or
  the head of the employee's department (role ``department_head``); see ``payroll.services._overtime_route``.
- ``payroll.loan``, ``payroll.deferred_sale_terms``, ``payroll.deferred_sale``, ``payroll.loan_write_off``: the
  entity's top authority (``EntityGovernance``: the managing partner in Almiras, its ``ceo`` elsewhere; OQ-39).
- the other activities: the CFO (``approver``), the ``ceo`` in parallel (OQ-41) and, in the group parent only, the
  managing partner (no cascade, R64).

:func:`check` is the module's own extra check after the engine issued the grant: under code defaults it applies the
roles above (and the overtime route); when the entity's matrix rows applied (``grant.rule_source == "matrix"``) the
engine already checked the named roles and users, so nothing is added. Top-authority self-approval is decided only by
the engine (access R79, payroll R84); payroll never passes ``allow_self_approval``.
"""
from core.access import authority_defaults
from core.access import services as access_services
from core.access.engine import governance_of, grant_rule_source, is_group_parent
from core.access.models import Membership

# Payroll's names for its approvals -> the engine's activity codes (payroll.md §11 R82).
ACTIVITY = {
    "payroll.reversal": "payroll.run_reversal",
    "payroll.cost_report": "payroll.secondment_cost_report",
}

LABELS = {
    "payroll.run": ("payroll runs", "مسيرات الرواتب"),
    "payroll.loan": ("employee loans", "قروض الموظفين"),
    "payroll.deferred_sale_terms": ("bai' ajel terms", "شروط البيع الآجل"),
    "payroll.deferred_sale": ("bai' ajel sales", "البيع الآجل"),
    "payroll.payment": ("payroll payments", "مدفوعات الرواتب"),
    "payroll.reversal": ("run reversals", "عكس المسيرات"),
    "payroll.maternity_clearance": ("maternity legal clearances", "الإذن القانوني لأجر الأمومة"),
    "payroll.input": ("payroll inputs", "مدخلات الرواتب"),
    "payroll.final_settlement": ("final settlements", "المخالصات النهائية"),
    "payroll.settlement_correction": ("final settlement corrections", "تصحيحات المخالصات النهائية"),
    "payroll.loan_write_off": ("loan write-offs", "شطب القروض"),
    "payroll.cost_item": ("employee cost items", "بنود تكاليف الموظفين"),
    "payroll.cost_report": ("secondment cost reports", "تقارير تكلفة الإعارة"),
    "payroll.gosi_fine": ("GOSI late-payment fines", "غرامات تأخير التأمينات"),
    "payroll.payment_reversal": ("payroll payment reversals", "عكس مدفوعات الرواتب"),
    "payroll.profit_recognition": ("bai' ajel profit recognition", "إثبات أرباح البيع الآجل"),
}


def _spec(activity):
    return authority_defaults.get(ACTIVITY.get(activity, activity)).default


def allowed_roles(entity, activity):
    """Role codes eligible for ``activity`` in ``entity`` under the code default (R70): the default's roles, or the
    entity's top-authority role. ``None`` for an activity whose default is every holder of the permission."""
    spec = _spec(activity)
    if spec.top_authority:
        return (governance_of(entity).top_authority_role,)
    if spec.roles is None:
        return None
    if not is_group_parent(entity):  # no cascade (R64): the managing partner approves only in the group parent
        return tuple(r for r in spec.roles if r != "managing_partner")
    return spec.roles


def check(grant, entity, activity, *, roles=None):
    """Under code defaults, the grant's memberships must include a role eligible for ``activity`` in ``entity``;
    else a logged refusal (``AccessDenied``, en/ar) naming the activity. ``roles`` overrides the default (overtime
    routes). Under the entity's matrix the engine has already decided (R69 to R75)."""
    if grant_rule_source(grant) == "matrix":
        return
    roles = roles if roles is not None else allowed_roles(entity, activity)
    if roles is None:
        return
    roles = tuple(roles)
    held = set(Membership.objects.filter(pk__in=grant.membership_ids).values_list("role__code", flat=True))
    if held & set(roles):
        return
    en, ar = LABELS.get(activity, (activity, activity))
    access_services.log_denial(actor=grant.user, entity=entity, permission=grant.permission,
                               details={"reason": "not_in_authority_matrix", "activity": activity})
    raise access_services.AccessDenied(
        "not_in_matrix", f"Under {entity.code}'s authority matrix, {en} are approved by {', '.join(roles)} only (P20).",
        f"وفق مصفوفة الصلاحيات في {entity.code} يعتمد {ar} {', '.join(roles)} فقط.",
        access_services._language(grant.user))
