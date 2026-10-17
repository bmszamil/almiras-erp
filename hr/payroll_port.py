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
- ``outstanding_balances(employee, as_of)``: the employee's loan and bai' ajel balances on the last working day,
  as a :class:`LoanBalances`. HR copies them onto the settlement input (H18): employee loans are recovered from the
  final dues, end-of-service award included, **without** the monthly 10% (Art. 92(1)) and 50% (Art. 93) caps,
  which apply to monthly pay deductions only; a bai' ajel balance gets the same treatment only when its contract
  holds the employee's signed undertaking (``bai_ajel_undertaking``). Payroll decides and posts the deduction.

Setting ``HR_PAYROLL_PORT`` picks the port. The default :class:`NoPayrollPort` keeps the inputs in HR only and
reports nothing approved. Tests use ``hr.testing.FakePayrollPort``.
"""
from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings
from django.utils.module_loading import import_string

DEFAULT_PORT = "hr.payroll_port.NoPayrollPort"


@dataclass(frozen=True)
class LoanBalances:
    """Outstanding balances payroll reports for the final settlement (H18). Decimal, entity functional currency."""

    loans: Decimal = Decimal("0.00")
    bai_ajel: Decimal = Decimal("0.00")
    bai_ajel_undertaking: bool = False


class NoPayrollPort:
    def create_draft(self, settlement_input, user):
        return None

    def is_approved(self, termination):
        return False

    def discard_draft(self, termination, user):
        return None

    def outstanding_balances(self, employee, as_of):
        return LoanBalances()


_override = None


def get_port():
    if _override is not None:
        return _override
    return import_string(getattr(settings, "HR_PAYROLL_PORT", DEFAULT_PORT))()
