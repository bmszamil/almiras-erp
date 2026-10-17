"""Regression tests for the billing code-review findings (reviewer probes P1 to P7 on 9d6189e, and the fixes
that followed). Each test names the finding and the rule it protects."""
from datetime import date
from decimal import Decimal

import pytest

from billing import services
from billing.conftest import approve_note, approved, clear, period, run, version
from billing.models import BillingAdjustment, BillingError, BillingPeriod
from invoicing.models import SalesDocument

D = Decimal


# --- P1 / finding 1: R42, OQ-24 --------------------------------------------------------------------------------

def test_p1_due_days_only_amendment_makes_no_correction_and_full_months_stay_whole(w):
    c = approved(w, lines=(("rent_commercial", "1000.05"),), start=date(2027, 4, 1))
    run(w, date(2027, 4, 1))
    clear(w, period(c, date(2027, 4, 1)).sales_document)
    v = services.approve_version(version(w, c, effective=date(2027, 4, 16), due_days=30), user=w.c)
    assert v.adjustments == [] and not BillingAdjustment.objects.filter(contract=c).exists()
    services.approve_version(version(w, c, effective=date(2027, 6, 16), due_days=45), user=w.c)
    for m in (5, 6):
        run(w, date(2027, m, 1))
    june = period(c, date(2027, 6, 1))
    assert june.invoice_lines[0]["net"] == "1000.05" and june.sales_document.total_net == D("1000.05")
    assert services.recompute_corrections(c, user=w.s) == []
