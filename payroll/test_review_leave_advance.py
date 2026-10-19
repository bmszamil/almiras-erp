"""Compliance C-A10 (2026-10-19): annual leave starting before the period's pay date is paid in advance (Labour Law
Art. 109) by an off-cycle run before the leave starts; the regular run pays the rest of the month net of it."""
from datetime import date

import pytest

from hr import services as hr
from payroll import services
from payroll.conftest import D, amount, approved, entry_lines, saudi, slip
from payroll.models import PayrollAlert, PayrollError


def on_leave(w, start, end):
    emp = saudi(w, hire_date=date(2025, 1, 1))
    adj = hr.record_opening_leave_balance(emp, "ANNUAL", "25", date(2027, 1, 1), user=w.h)
    hr.approve_leave_adjustment(adj, user=w.c)
    req = hr.request_leave(emp, "ANNUAL", user=w.h, start_date=start, end_date=end)
    return emp, hr.approve_leave(req, user=w.h2)


def test_c_a10_leave_pay_in_advance_before_the_leave_and_the_rest_in_the_regular_run(w):
    s1, req = on_leave(w, date(2027, 3, 20), date(2027, 4, 5))
    assert services.send_alerts(today=date(2027, 3, 10), entity=w.alm) >= 1
    assert PayrollAlert.objects.filter(kind="leave_advance", recipient=w.p).count() == 1
    mar_adv, apr_adv = services.prepare_leave_advance(req, user=w.p)
    assert (mar_adv.period, apr_adv.period) == ("2027-03", "2027-04")
    assert mar_adv.kind == "off_cycle" and mar_adv.purpose == "leave_advance" and mar_adv.pay_date == date(2027, 3, 19)
    # March 20 to 30 carry 11/30 (day 31 carries nothing on the 30-day basis); April 1 to 5 carry 5/30.
    assert slip(mar_adv, s1).gross == D("4950.00") and slip(apr_adv, s1).gross == D("2250.00")
    assert amount(mar_adv, s1, "employee_gosi") == 0 and amount(mar_adv, s1, "eosb_accrual") == 0
    with pytest.raises(PayrollError):  # prepared once
        services.prepare_leave_advance(req, user=w.p)
    mar_adv = services.approve_run(services.submit_run(mar_adv, user=w.p), user=w.h)
    assert entry_lines(mar_adv) == {"5100": D("4950.00"), "2330": D("-4950.00")}
    services.send_alerts(today=date(2027, 3, 11), entity=w.alm)
    assert PayrollAlert.objects.filter(kind="leave_advance", recipient=w.p).count() == 1  # no new alert
    mar = approved(w, "2027-03")
    ps = slip(mar, s1)
    assert ps.gross == D("13500.00") - D("4950.00")
    assert amount(mar, s1, "employee_gosi") == D("1218.75")  # GOSI on the full month, in the regular run
    assert ps.net == D("13500.00") - D("4950.00") - D("1218.75")


def test_c_a10_only_annual_leave_of_ksa_employees(w):
    emp = saudi(w)
    req = hr.approve_leave(hr.request_leave(emp, "UNPAID", user=w.h, start_date=date(2027, 3, 20),
                                            end_date=date(2027, 3, 21), reason_ar="سبب"), user=w.h2)
    with pytest.raises(PayrollError) as exc:
        services.prepare_leave_advance(req, user=w.p)
    assert exc.value.code == "leave_advance"
