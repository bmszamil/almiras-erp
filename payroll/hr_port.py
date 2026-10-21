"""payroll's side of ``hr.payroll_port`` (hr R38, R40, R41; payroll R28, R34, R49; compliance C-A11): the final
settlement draft is created inside hr's termination approval, a withdrawn termination discards it, and hr learns
when it is approved (EOSB recalculation and withdrawal then refused). Loan and bai' ajel balances are reported for
the settlement input."""
from datetime import date, timedelta
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
        """Slice 3, compliance C4 and verification 1 and 3 (Labour Law Art. 2, 84; hr R39): the regular additions to
        the EOSB wage, monthly. (a) The bonus and commission / profit-share inputs marked regular, contractual or
        customary (``PayrollInput.eosb_regular``; default not) paid by approved runs in the 12 months before
        ``as_of``'s month, divided by the months of that window the employee was employed (never contract components:
        hr counts those). (b) In-kind housing at ``eosb.in_kind_housing`` (SA, months of BASIC a year) only once a
        person has verified that row (OQ-62); until then it counts 0 with a bilingual warning. The rows used are
        returned for the caller's ParamUse (R7)."""
        from decimal import ROUND_HALF_UP

        from hr import services as hr
        from hr.models import StatutoryParameter
        from .models import PayrollInput, PayrollRun, PayslipLine

        cent = Decimal("0.01")
        months, y, m = [], as_of.year, as_of.month
        for _ in range(12):
            y, m = (y - 1, 12) if m == 1 else (y, m - 1)
            months.append((y, m))
        last_day = hr._end_of_service(employee)
        employed = 0
        for y_, m_ in months:
            start = date(y_, m_, 1)
            end = date(y_ + (m_ == 12), m_ % 12 + 1, 1) - timedelta(days=1)
            if employee.hire_date <= end and (last_day is None or last_day >= start):
                employed += 1
        regular_inputs = PayrollInput.objects.filter(employee=employee, eosb_regular=True).values_list("pk", flat=True)
        refs = [f"input:{pk}" for pk in regular_inputs]
        lines = PayslipLine.objects.filter(
            payslip__employee=employee, payslip__refused=False, source_ref__in=refs,
            payslip__run__status__in=(PayrollRun.Status.APPROVED, PayrollRun.Status.PAID),
            payslip__run__period__in=[f"{y_:04d}-{m_:02d}" for y_, m_ in months])
        parts, rows, warnings = {}, [], []
        if employed:
            for code, qs in (("BONUS", lines.filter(line_type=PayslipLine.LineType.BONUS)),
                             ("COMMISSION", lines.filter(line_type=PayslipLine.LineType.ONE_OFF_EARNING,
                                                         component__code__in=("COMMISSION", "PROFIT_SHARE")))):
                amount = qs.aggregate(t=Sum("amount"))["t"] or Decimal("0")
                if amount:
                    parts[code] = (amount / employed).quantize(cent, rounding=ROUND_HALF_UP)
        terms = hr._terms_on(employee, as_of)
        if terms is not None and getattr(terms, "housing_in_kind", False):
            row = hr.param("SA", "eosb.in_kind_housing", as_of, required=False)
            if row is not None and row.status == StatutoryParameter.Status.VERIFIED \
                    and (row.value or {}).get("basic_months_per_year"):
                rows.append(row)
                basic = sum((c.monthly_amount for c in terms.components.select_related("component")
                             if c.component.code == "BASIC"), Decimal("0"))
                parts["HOUSING_IN_KIND"] = (basic * Decimal(row.value["basic_months_per_year"]) / 12).quantize(
                    cent, rounding=ROUND_HALF_UP)
            else:
                warnings.append(f"{employee.employee_code}: in-kind housing is not in the end-of-service wage until "
                                "eosb.in_kind_housing is verified (Labour Law Art. 2; OQ-62) / "
                                f"{employee.employee_code}: لا يدخل السكن العيني في أجر مكافأة نهاية الخدمة حتى يُتحقق من "
                                "المعيار eosb.in_kind_housing")
        return AdditionalWage(amount=sum(parts.values(), Decimal("0.00")), parts={k: str(v) for k, v in parts.items()},
                              rows=tuple(rows), warnings=tuple(warnings))
