"""The hand-off from HR to the (not yet built) ``payroll`` module for final settlements (hr R38, R40, R41;
payroll.md 3.8, R34).

HR computes the settlement inputs (``hr.FinalSettlementInput``: EOSB snapshot, leave balance and encashment,
notice shortfall) at termination approval. Payroll will turn them into its ``FinalSettlement`` draft, approve and
post it. HR needs two answers back:

- ``create_draft(settlement_input, user)``: payroll creates its draft (called inside the termination
  approval transaction; an exception rolls the approval back, R38).
- ``is_approved(termination)``: True once payroll approved the final settlement: then EOSB recalculation and a
  withdrawal of the termination are refused (R40, R41).
- ``discard_draft(termination, user)``: a withdrawn termination discards payroll's draft (R41).

Setting ``HR_PAYROLL_PORT`` picks the port. The default :class:`NoPayrollPort` keeps the inputs in HR only and
reports nothing approved. Tests use ``hr.testing.FakePayrollPort``.
"""
from django.conf import settings
from django.utils.module_loading import import_string

DEFAULT_PORT = "hr.payroll_port.NoPayrollPort"


class NoPayrollPort:
    def create_draft(self, settlement_input, user):
        return None

    def is_approved(self, termination):
        return False

    def discard_draft(self, termination, user):
        return None


_override = None


def get_port():
    if _override is not None:
        return _override
    return import_string(getattr(settings, "HR_PAYROLL_PORT", DEFAULT_PORT))()
