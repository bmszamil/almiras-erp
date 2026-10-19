"""Seam to the per-entity authority matrix (access 3.8, A24 to A26; payroll P20). Not built yet.

Until the approval engine and matrix exist, the seed rows of payroll.md §7 / OQ-47 live here as code, read by
``payroll.services`` after ``core.access.require`` has issued the grant:

- ``payroll.run``: ``hr_officer`` in every entity (P14); never the Managing Partner ("contracted payroll needs no
  Managing Partner approval", P20(a)), although managing_partner holds every A permission as fallback (OQ-1).
- ``payroll.overtime``: the entity's hr route (``HrSettings.overtime_approval_route``, data): ``managing_partner`` or
  the head of the employee's department (role ``department_head``); see ``payroll.services._overtime_route``.
- ``payroll.loan``, ``payroll.deferred_sale_terms``, ``payroll.deferred_sale``: ``managing_partner`` (P12; the
  ``ceo`` seed row of other entities waits for the ``ceo`` role, access A28).
- ``payroll.payment``, ``payroll.reversal``, ``payroll.maternity_clearance``: ``approver``, ``managing_partner``
  as fallback (OQ-36).

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
}


def allowed_roles(entity, activity):
    """Role codes eligible for ``activity`` in ``entity`` (matrix seed rows; the same in every entity today)."""
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
