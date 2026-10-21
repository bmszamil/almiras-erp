"""HR's adapters to the one approval engine (access.md §10.5, R61, R82). Registered in ``HrConfig.ready``.

Every HR approval is about one employee: the employee's own user is its beneficiary and never approves it (R82;
hr OQ-25), whatever their role. Labels carry the record type and id only (no names, R36 / PDPL).
"""


def _employee_user(record):
    employee = getattr(record, "employee", None)
    user_id = getattr(employee, "user_id", None)
    return {user_id} if user_id else set()


def register():
    from core.access import engine

    from . import services
    from .models import (Assignment, EmploymentContract, IbanChangeRequest, LeaveAdjustment, LeaveRequest,
                         StatutoryParameter, Termination)

    def label(kind_en, kind_ar):
        return lambda r: (f"{kind_en} #{r.pk}", f"{kind_ar} رقم {r.pk}")

    engine.register(EmploymentContract, "hr.contract", permission="hr.approve_contract", beneficiaries=_employee_user,
                    label=label("Employment contract", "عقد عمل"),
                    approve=lambda r, user: services.approve_contract(r, user=user),
                    reject=lambda r, user, note: services.reject_contract(r, user=user, reason_ar=note or "مرفوض"))
    engine.register(Assignment, "hr.assignment", permission="hr.approve_assignment", beneficiaries=_employee_user,
                    label=label("Assignment", "تكليف"),
                    approve=lambda r, user: services.approve_assignment(r, user=user))
    engine.register(LeaveRequest, "hr.leave", permission="hr.approve_leave", beneficiaries=_employee_user,
                    label=label("Leave request", "طلب إجازة"),
                    approve=lambda r, user: services.approve_leave(r, user=user),
                    reject=lambda r, user, note: services.reject_leave(r, user=user, reason_ar=note))
    engine.register(LeaveAdjustment, "hr.leave_adjustment", permission="hr.adjust_leave_balance",
                    beneficiaries=_employee_user, label=label("Leave adjustment", "تسوية رصيد إجازة"),
                    approve=lambda r, user: services.approve_leave_adjustment(r, user=user),
                    reject=lambda r, user, note: services.reject_leave_adjustment(r, user=user,
                                                                                    reason_ar=note or "مرفوض"))
    engine.register(Termination, "hr.termination", permission="hr.approve_termination", beneficiaries=_employee_user,
                    label=label("Termination", "إنهاء خدمة"),
                    approve=lambda r, user: services.approve_termination(r, user=user))
    engine.register(StatutoryParameter, "hr.statutory_parameter", permission="hr.verify_statutory_parameter",
                    label=label("Statutory parameter", "معيار نظامي"),
                    approve=lambda r, user: services.verify_statutory_parameter(r, user=user))
    # access R61, R80 f (review of 92c35c2, item 10): the IBAN change's second person under hr.manage_employee (not an
    # A right), routed with require(..., through_engine=True); NSA: never self-approved, the top authority included.
    engine.register(IbanChangeRequest, "hr.iban_change", permission="hr.manage_employee", beneficiaries=_employee_user,
                    amount=lambda r: None, version=lambda r: str(r.pk),
                    label=label("IBAN change", "تغيير آيبان"),
                    approve=lambda r, user: services.approve_iban_change(r, user=user),
                    reject=lambda r, user, note: services.reject_iban_change(r, user=user, reason_ar=note or "مرفوض"))
