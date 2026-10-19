"""E1 / AT-7 / AT-14: the monthly run of ALM for March 2027, posted through the registry by the HR officer."""
from datetime import date

import pytest

from payroll import services
from payroll.conftest import D, amount, approved, entry_lines, expat, saudi, slip


def test_e1_march_run_posts_as_specified(w):
    s1 = saudi(w)
    n1 = expat(w)
    loan = services.create_loan(n1, user=w.p, kind="loan", principal="3000.00", disbursement_date=date(2027, 2, 1),
                                purpose_ar="سلفة", installments=[("2027-03", "500.00"), ("2027-04", "2500.00")])
    services.submit_loan(loan, user=w.p)
    services.approve_loan(loan, user=w.mp)
    run = approved(w)
    assert run.number == "ALM-PAY-2027-000001" and run.journal_entry.date == date(2027, 3, 31)
    assert amount(run, s1, "employee_gosi") == D("1218.75")
    assert amount(run, s1, "employer_gosi") == D("1468.75")
    assert amount(run, n1, "employer_gosi") == D("150.00") and amount(run, n1, "employee_gosi") == 0
    e = entry_lines(run)
    eosb = slip(run, s1).eosb_accrual + slip(run, n1).eosb_accrual
    assert e["5100"] == D("21500.00") and e["5110"] == D("1618.75") and e["2340"] == D("-2837.50")
    assert e["1230"] == D("-500.00") and e["2330"] == D("-19781.25") and e["5120"] == eosb and e["2500"] == -eosb
