"""Compliance C-B7 (2026-10-19): the full GOSI share is booked to 2340 every month; the employee share the net pay
cannot cover is a receivable from the employee (new law, recovered with notice) or employer cost (legacy cohort);
a GOSI payment may follow the recorded GOSI invoice, the difference flagged."""
from datetime import date

import pytest

from hr import services as hr
from payroll import services
from payroll.conftest import D, amount, approved, entry_lines, expat, saudi, slip
from payroll.models import GosiCatchUpNotice, PayrollError

S5 = {"BASIC": "10000.00", "HOUSING": "2500.00"}


def unpaid_march(w, emp):
    req = hr.request_leave(emp, "UNPAID", user=w.h, start_date=date(2027, 3, 1), end_date=date(2027, 3, 31),
                           reason_ar="سبب")
    hr.approve_leave(req, user=w.h2)


def test_c_b7_new_law_share_not_covered_is_booked_in_full_and_recovered_with_notice(w):
    s5 = saudi(w, cohort="new_m273", components=dict(S5), gosi_first_coverage_date=date(2025, 2, 1))
    unpaid_march(w, s5)  # GOSI continues on the full registered wage (Regs Art. 20(3)(c)); the net pay is zero
    mar = approved(w, "2027-03")
    ps = slip(mar, s5)
    assert ps.gross == 0 and amount(mar, s5, "employee_gosi") == 0
    assert amount(mar, s5, "gosi_uncollected") == D("1343.75")
    e = entry_lines(mar)
    assert e["2340"] == D("-2937.50") and e["1230"] == D("1343.75") and e["5110"] == D("1593.75")
    assert services._gosi_open(w.alm, "2027-03") == D("2937.50")
    apr = approved(w, "2027-04")
    rec = slip(apr, s5).lines.filter(line_type="gosi_catch_up", origin_period="2027-03")
    assert sum(l.amount for l in rec) == D("1343.75") and {l.account.code for l in rec} == {"1230"}
    assert entry_lines(apr)["1230"] == D("-1343.75")
    notice = GosiCatchUpNotice.objects.get(employee=s5, run=apr)
    assert notice.amount == D("1343.75") and notice.period_of_origin == "2027-03" and notice.text_ar
    may = approved(w, "2027-05")
    assert amount(may, s5, "gosi_catch_up") == 0  # recovered once


def test_c_b7_legacy_share_not_covered_is_employer_cost_never_recovered(w):
    s1 = saudi(w, components=dict(S5))  # legacy_m33
    unpaid_march(w, s1)
    mar = approved(w, "2027-03")
    e = entry_lines(mar)
    assert e["2340"] == D("-2687.50") and "1230" not in e and e["5110"] == D("2687.50")
    assert any("19(4)" in x for x in slip(mar, s1).warnings)
    apr = approved(w, "2027-04")
    assert amount(apr, s1, "gosi_catch_up") == 0


def test_c_b7_gosi_payment_up_to_the_recorded_invoice_with_the_difference_flagged(w):
    saudi(w)
    expat(w)
    approved(w)
    with pytest.raises(PayrollError):
        services.record_payment(w.alm, user=w.p, kind="gosi", gosi_period="2027-03", payment_date=date(2027, 4, 10),
                                amount="2900.00")
    services.record_gosi_reconciliation(w.alm, "2027-03", user=w.p, gosi_invoice_amount="2900.00", note_ar="فاتورة")
    with pytest.raises(PayrollError):
        services.record_payment(w.alm, user=w.p, kind="gosi", gosi_period="2027-03", payment_date=date(2027, 4, 10),
                                amount="2900.01")
    p = services.record_payment(w.alm, user=w.p, kind="gosi", gosi_period="2027-03", payment_date=date(2027, 4, 10),
                                amount="2900.00")
    assert p.warnings and "62.50" in p.warnings[0] and "C-B7" in p.warnings[0]
    p = services.approve_payment(services.submit_payment(p, user=w.p), user=w.c)
    lines = {l.account.code: (l.debit, l.credit) for l in p.journal_entry.lines.select_related("account")}
    assert lines == {"2340": (D("2900.00"), D("0")), "1100": (D("0"), D("2900.00"))}
