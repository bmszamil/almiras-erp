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
- ``eosb_additional_wage(employee, as_of)``: an :class:`AdditionalWage`, the regular bonuses, commissions and
  in-kind benefits payroll paid on top of the contract components (R39). Until payroll supplies actuals, the EOSB
  wage is the contract components flagged ``counts_for_eosb`` (a COMMISSION or BONUS component on the contract
  flagged for EOSB counts there) and this returns zero.

Setting ``HR_PAYROLL_PORT`` picks the port. The default :class:`NoPayrollPort` keeps the inputs in HR only and
reports nothing approved. Tests use ``hr.testing.FakePayrollPort``.
"""
from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings
from django.utils.module_loading import import_string

DEFAULT_PORT = "hr.payroll_port.NoPayrollPort"


@dataclass(frozen=True)
class AdditionalWage:
    """R39 (Art. 2, 84, 86): what payroll paid regularly on top of the contract components counting for EOSB
    (regular bonuses, commissions and sales percentages not excluded in writing, in-kind benefits), as a monthly
    amount with its parts ``{code: amount}``. Decimal, entity functional currency."""

    amount: Decimal = Decimal("0.00")
    parts: dict = None
    # Payroll slice 3 (verification 1, hr R7): the parameter rows the additions used (taken into the caller's
    # ParamUse, so they are checked and recorded) and bilingual warnings (e.g. an unverified row not used).
    rows: tuple = ()
    warnings: tuple = ()


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

    def eosb_additional_wage(self, employee, as_of):
        return AdditionalWage()


_override = None


def get_port():
    if _override is not None:
        return _override
    return import_string(getattr(settings, "HR_PAYROLL_PORT", DEFAULT_PORT))()
