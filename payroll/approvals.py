"""Seam to the per-entity authority matrix (access 3.8, A24 to A26; payroll P20). Not built yet.

Until the approval engine and matrix exist, the seed rows of payroll.md §7 / OQ-47 live here as code, read by
``payroll.services`` after ``core.access.require`` has issued the grant:

- ``payroll.run``: ``hr_officer`` in every entity (P14); never the Managing Partner ("contracted payroll needs no
  Managing Partner approval", P20(a)), although managing_partner holds every A permission as fallback (OQ-1).
- ``payroll.overtime``: the entity's hr route (``HrSettings.overtime_approval_route``, data): ``managing_partner`` or
  the head of the employee's department (role ``department_head``); see ``payroll.services._overtime_route``.
- ``payroll.loan``, ``payroll.deferred_sale_terms``, ``payroll.deferred_sale``: ``managing_partner`` (P12; the
  ``ceo`` seed row of other entities waits for the ``ceo`` role, access A28).
- ``payroll.payment``, ``payroll.reversal``, ``payroll.maternity_clearance``, ``payroll.final_settlement``:
  ``approver``, ``managing_partner`` as fallback (OQ-36).

- ``payroll.loan_write_off`` (slice 2, OQ-7 default): the entity's top authority: ``managing_partner`` in the group
  parent (Almiras), ``ceo`` in every other entity (access A26, R58 default). The ``ceo`` role is part of the access
  build (A28); until it exists a subsidiary's write-off is refused, naming the role.
- ``payroll.settlement_correction``, ``payroll.cost_item``, ``payroll.cost_report``, ``payroll.gosi_fine``,
  ``payroll.payment_reversal``, ``payroll.profit_recognition`` (slice 2): ``approver``, ``managing_partner`` as
  fallback (OQ-36 default).

Top-authority self-approval (access R58) needs the matrix's ``top_authority_role``; until then nobody self-approves
in payroll (the access layer refuses the creator, submitter and last modifier of every A document).

When the matrix is built, :func:`allowed_roles` reads it instead of ``SEED_ROWS``; callers do not change.
"""
from core.access import services as access_services
from core.access.models import Membership


SEED_ROWS = {
    "payroll.run": ("hr_officer",),
    "payroll.loan": ("managing_partner",),
    "payroll.deferred_sale_terms": ("managing_partner",),
    "payroll.deferred_sale": ("managing_partner",),
    "payroll.payment": ("approver", "managing_partner"),
    "payroll.reversal": ("approver", "managing_partner"),
    "payroll.maternity_clearance": ("approver", "managing_partner"),
    "payroll.input": ("payroll_officer", "managing_partner"),
    "payroll.final_settlement": ("approver", "managing_partner"),
    "payroll.settlement_correction": ("approver", "managing_partner"),
    "payroll.cost_item": ("approver", "managing_partner"),
    "payroll.cost_report": ("approver", "managing_partner"),
    "payroll.gosi_fine": ("approver", "managing_partner"),
    "payroll.payment_reversal": ("approver", "managing_partner"),
    "payroll.profit_recognition": ("approver", "managing_partner"),
}

# Activities whose seed row is the entity's top authority (access A26): the MP in the group parent, the CEO elsewhere.
TOP_AUTHORITY_ROWS = {
    "payroll.loan_write_off": {"group_parent": ("managing_partner",), "other": ("ceo",)},
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
    "payroll.loan_write_off": ("loan and bai' ajel write-offs and recoveries", "شطب القروض والبيع الآجل واستردادها"),
    "payroll.cost_item": ("employee cost items", "بنود تكاليف الموظفين"),
    "payroll.cost_report": ("secondment cost reports", "تقارير تكلفة الإعارة"),
    "payroll.gosi_fine": ("GOSI late-payment fines", "غرامات تأخير التأمينات"),
    "payroll.payment_reversal": ("payroll payment reversals", "عكس مدفوعات الرواتب"),
    "payroll.profit_recognition": ("bai' ajel profit recognition", "إثبات أرباح البيع الآجل"),
}


def is_group_parent(entity):
    """The group parent (Almiras): a parent-consolidation entity without a parent (data, never a code)."""
    return entity.parent_id is None and entity.consolidation == "parent"


def allowed_roles(entity, activity):
    """Role codes eligible for ``activity`` in ``entity`` (matrix seed rows; the same in every entity except the
    top-authority rows, which differ between the group parent and the other entities)."""
    if activity in TOP_AUTHORITY_ROWS:
        return TOP_AUTHORITY_ROWS[activity]["group_parent" if is_group_parent(entity) else "other"]
    return SEED_ROWS[activity]


def check(grant, entity, activity, *, roles=None):
    """The grant's memberships must include a role eligible for ``activity`` in ``entity``; else a logged refusal
    (``AccessDenied``, en/ar) naming the activity. ``roles`` overrides the seed (overtime routes)."""
    roles = tuple(roles if roles is not None else allowed_roles(entity, activity))
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
