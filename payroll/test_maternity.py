"""Maternity and GOSI compensation (R61, P19(a), P20(d), (e); E15; AT-53, AT-54, AT-62, AT-63)."""
from datetime import date

import pytest

from hr import services as hr
from payroll import services
from payroll.conftest import D, amount, approved, calculated, entry_lines, saudi, slip
from payroll.models import PayrollAlert, PayrollError

E2 = {"BASIC": "10000.00", "HOUSING": "2500.00", "TRANSPORT": "3000.00"}


def mother(w, start=date(2027, 4, 1), end=date(2027, 6, 23), birth=date(2027, 4, 10)):
    emp = saudi(w, components=dict(E2), cohort="new_m273", gender="female",
                gosi_first_coverage_date=date(2025, 1, 1))
    req = hr.request_leave(emp, "MATERNITY", user=w.h, start_date=start, end_date=end, event_date=birth,
                           document_ref="BIRTH-1")
    return emp, hr.approve_leave(req, user=w.h2)


def confirm(w, req, **kw):
    values = dict(status="gosi_confirmed", gosi_reference="GOSI-M-1", confirmed_from=date(2027, 4, 1),
                  confirmed_to=date(2027, 6, 30), monthly_amount="12500.00")
    values.update(kw)
    return services.record_maternity_case(req, user=w.h, **values)


def test_at53_e15_confirmed_compensation_top_up_and_gosi_continue(w):
    e2, req = mother(w)
    confirm(w, req)
    run = approved(w, "2027-04")
    ps = slip(run, e2)
    assert ps.maternity_status == "gosi_confirmed" and ps.maternity_top_up == D("3000.00")
    assert ps.gross == D("3000.00") and ps.net == D("1656.25")
    assert amount(run, e2, "employee_gosi") == D("1343.75") and amount(run, e2, "employer_gosi") == D("1593.75")
    assert amount(run, e2, "unpaid_deduction") == 0 and ps.eosb_accrual > 0
    e = entry_lines(run)
    assert e["5100"] == D("3000.00") and e["5110"] == D("1593.75") and e["2340"] == D("-2937.50")
    assert e["2330"] == D("-1656.25")


def test_at62_gosi_refused_means_full_salary(w):
    e2, req = mother(w)
    with pytest.raises(PayrollError):  # the refusal reference is required (P20(d))
        services.record_maternity_case(req, user=w.h, status="gosi_refused")
    services.record_maternity_case(req, user=w.h, status="gosi_refused", refusal_reference="GOSI-REF-9")
    run = calculated(w, "2027-04")
    ps = slip(run, e2)
    assert ps.gross == D("15500.00") and ps.net == D("14156.25") and amount(run, e2, "maternity_top_up") == 0


def test_at54_pending_case_carried_not_unpaid_and_escalated(w):
    e3, req = mother(w, start=date(2027, 4, 16), end=date(2027, 7, 8), birth=date(2027, 4, 25))
    run = calculated(w, "2027-04")
    ps = slip(run, e3)
    assert ps.maternity_status == "pending_gosi" and ps.pending_gosi_amount == D("7750.00")  # days 16 to 30
    assert ps.gross == D("7750.00") and amount(run, e3, "unpaid_deduction") == 0
    assert PayrollAlert.objects.filter(kind="maternity_pending", recipient=w.h).exists()
    assert PayrollAlert.objects.filter(kind="maternity_pending", recipient=w.mp).exists()


def test_at54_support_needs_clearance_and_full_pay_next_to_gosi_needs_written_clearance(w):
    e5, req5 = mother(w)
    with pytest.raises(PayrollError):  # R61(b): support needs the clearance reference
        services.create_input(e5, user=w.p, kind="maternity_support", period="2027-04", amount="2000.00",
                              reason_ar="دعم")
    inp = services.create_input(e5, user=w.p, kind="maternity_support", period="2027-04", amount="2000.00",
                                reason_ar="دعم", legal_clearance_ref="LEGAL-1")
    with pytest.raises(Exception):
        services.approve_maternity_support(inp, user=w.p2)  # the payroll officer does not approve it
    services.approve_maternity_support(inp, user=w.c)
    run = calculated(w, "2027-04")
    assert amount(run, e5, "maternity_support") == D("2000.00")  # pending case: support paid
    case = confirm(w, req5)
    run = services.calculate_run(run, user=w.p)
    assert slip(run, e5).refused and "legal clearance" in slip(run, e5).refusal  # R61(d)
    with pytest.raises(Exception):
        services.approve_maternity_clearance(case, user=w.h, legal_clearance_ref="LEGAL-2")  # recorder (A)
    services.approve_maternity_clearance(case, user=w.c, legal_clearance_ref="LEGAL-2")
    run = services.calculate_run(run, user=w.p)
    ps = slip(run, e5)
    assert not ps.refused and ps.legal_clearance_ref == "LEGAL-2" and ps.gross == D("17500.00")


def test_r61_no_recorded_case_counts_as_pending_never_unpaid(w):
    e9, _req = mother(w)
    run = calculated(w, "2027-04")
    ps = slip(run, e9)
    assert ps.maternity_status == "pending_gosi" and amount(run, e9, "unpaid_deduction") == 0
