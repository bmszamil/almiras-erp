"""payroll's side of ``hr.payroll_port`` (hr R38, R40, R41; payroll R28, R49). Slice 1 answers the loan and bai'
ajel balances HR copies onto the final-settlement input; the payroll final settlement itself (R34, E5) is slice 2,
so drafts are not created yet and nothing is reported approved (HR keeps its inputs, as with ``NoPayrollPort``)."""
from decimal import Decimal

from django.db.models import Sum

from hr.payroll_port import AdditionalWage, LoanBalances, NoPayrollPort


class PayrollHrPort(NoPayrollPort):
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
