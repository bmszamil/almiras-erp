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
        """Slice 3, compliance C4 (Labour Law Art. 2, 84; hr R39): the regular additions to the EOSB wage, monthly:
        the average over the 12 months before ``as_of``'s month of the bonuses and the commission or profit-share
        inputs paid by approved runs (never contract components: hr counts those itself), plus in-kind housing at
        ``eosb.in_kind_housing`` (SA, months of BASIC a year, [verify]) when the contract gives housing in kind."""
        from decimal import ROUND_HALF_UP

        from hr import services as hr
        from .models import PayrollRun, PayslipLine

        months, end = [], as_of.replace(day=1)
        y, m = end.year, end.month
        for _ in range(12):
            y, m = (y - 1, 12) if m == 1 else (y, m - 1)
            months.append(f"{y:04d}-{m:02d}")
        lines = PayslipLine.objects.filter(
            payslip__employee=employee, payslip__refused=False,
            payslip__run__status__in=(PayrollRun.Status.APPROVED, PayrollRun.Status.PAID),
            payslip__run__period__in=months)
        regular = (lines.filter(line_type=PayslipLine.LineType.BONUS)
                   | lines.filter(line_type=PayslipLine.LineType.ONE_OFF_EARNING,
                                  component__code__in=("COMMISSION", "PROFIT_SHARE")))
        parts = {}
        for code, total in (("BONUS", regular.filter(line_type=PayslipLine.LineType.BONUS)),
                            ("COMMISSION", regular.exclude(line_type=PayslipLine.LineType.BONUS))):
            amount = total.aggregate(t=Sum("amount"))["t"] or Decimal("0")
            if amount:
                parts[code] = (amount / 12).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        terms = hr._terms_on(employee, as_of)
        if terms is not None and getattr(terms, "housing_in_kind", False):
            row = hr.param("SA", "eosb.in_kind_housing", as_of, required=False)
            if row is not None and (row.value or {}).get("basic_months_per_year"):
                basic = sum((c.monthly_amount for c in terms.components.select_related("component")
                             if c.component.code == "BASIC"), Decimal("0"))
                parts["HOUSING_IN_KIND"] = (basic * Decimal(row.value["basic_months_per_year"]) / 12).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP)
        return AdditionalWage(amount=sum(parts.values(), Decimal("0.00")), parts={k: str(v) for k, v in parts.items()})
