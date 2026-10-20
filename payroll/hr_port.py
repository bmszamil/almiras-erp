"""payroll's side of ``hr.payroll_port`` (hr R38, R40, R41; payroll R28, R34, R49; compliance C-A11): the final
settlement draft is created inside hr's termination approval, a withdrawn termination discards it, and hr learns
when it is approved (EOSB recalculation and withdrawal then refused). Loan and bai' ajel balances are reported for
the settlement input."""
from decimal import Decimal

from django.db.models import Sum

from hr.payroll_port import AdditionalWage, LoanBalances, NoPayrollPort


class PayrollHrPort(NoPayrollPort):
    def create_draft(self, settlement_input, user):
        from .services import _settlement_draft
        return _settlement_draft(settlement_input, user)

    def is_approved(self, termination):
        from .services import _settlement_approved
        return _settlement_approved(termination)

    def discard_draft(self, termination, user):
        from .services import _discard_settlement
        return _discard_settlement(termination, user)

    def outstanding_balances(self, employee, as_of):
        from .models import DeferredSaleContract, DeferredSaleInstallment
        from .services import _loan_outstanding

        sold = DeferredSaleContract.objects.filter(employee=employee, status=DeferredSaleContract.Status.SOLD)
        bai = DeferredSaleInstallment.objects.filter(contract__in=sold, status=DeferredSaleInstallment.Status.PLANNED) \
            .aggregate(t=Sum("amount"))["t"] or Decimal("0.00")
        return LoanBalances(loans=_loan_outstanding(employee), bai_ajel=bai,
                            bai_ajel_undertaking=sold.filter(final_dues_undertaking=True).exists())

    def eosb_additional_wage(self, employee, as_of):
        return AdditionalWage()
