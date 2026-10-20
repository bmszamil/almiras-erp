"""The payroll side of billing R71 (secondment recharge at actual cost or cost plus; owner decision A4: "all actual
cost fully loaded (not only salary but also all benefits and government fees such as medical insurance, Iqama
renewal etc.)"; payroll R35, R63).

Billing reads only the **approved** monthly secondment cost report of the seconding entity: for each secondment
(hr ``Assignment``) and month, the fully loaded cost for the days on secondment (gross pay and benefits through
payroll, employer GOSI, the EOSB accrual and the employee's approved cost items: medical insurance, iqama renewal,
work permit and visa fees, other government fees and benefits). ``None`` means the month is not approved yet:
billing keeps the period ``held`` ("cost report not yet approved", en/ar, billing R71). A later correction is a new
approved version; billing bills the difference as an adjustment (billing R42 to R48).

Billing's ``recharge_basis`` (``actual_cost`` / ``cost_plus``) is not built yet, so nothing calls this port today;
the billing builder wires it (no billing code is changed here).
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class SecondmentCost:
    total: Decimal  # seconding entity's functional currency, 2 dp
    currency: str
    days: int
    days_in_month: int
    report_id: int
    version: int
    approved_at: datetime


class PayrollCostPort:
    """Default port (``BILLING_PAYROLL_COST_PORT`` may name another for tests)."""

    def approved_cost(self, assignment, period):
        from .services import _approved_secondment_cost

        row = _approved_secondment_cost(assignment, period)
        if row is None:
            return None
        return SecondmentCost(total=row.total, currency=row.currency, days=row.days, days_in_month=row.days_in_month,
                              report_id=row.report_id, version=row.version, approved_at=row.approved_at)
