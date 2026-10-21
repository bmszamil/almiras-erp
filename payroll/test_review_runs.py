"""Review fixes (2026-10-19): runs, off-cycle runs, day weights, inputs and earlier months (review items 2, 4, 9, 10,
13; compliance C-A2). Ported from the reviewer's probes test_zz_probes.py (P1, P1b, P2, P3, P4, P9) and
test_zz_probe2.py as regression tests."""
from datetime import date, datetime

import pytest

from hr import services as hr
from payroll import services
from payroll.conftest import D, amount, approved, calculated, entry_lines, expat, saudi, slip, submitted
from payroll.models import PayrollError, PayrollInput, PayrollRun

E2 = {"BASIC": "10000.00", "HOUSING": "2500.00", "TRANSPORT": "3000.00"}


def mother(w, start, end, birth):
    emp = saudi(w, components=dict(E2), cohort="new_m273", gender="female",
                gosi_first_coverage_date=date(2025, 1, 1))
    req = hr.request_leave(emp, "MATERNITY", user=w.h, start_date=start, end_date=end, event_date=birth,
                           document_ref="BIRTH-1")
    return emp, hr.approve_leave(req, user=w.h2)


def unpaid_leave(w, emp, start, end):
    req = hr.request_leave(emp, "UNPAID", user=w.h, start_date=start, end_date=end, reason_ar="سبب")
    return hr.approve_leave(req, user=w.h2)


def off_cycle(w, period, employees, reason="تصحيح"):
    return services.create_run(w.alm, user=w.p, period=period, kind="off_cycle", reason_ar=reason, employees=employees)


def approve(w, run, **kw):
    run = services.calculate_run(run, user=w.p)
    return services.approve_run(services.submit_run(run, user=w.p, **kw), user=w.h)


# --- review 4: reductions use the day weights of the earnings ------------------------------------------------------

@pytest.mark.parametrize("start,end,birth,period", [
    (date(2027, 3, 1), date(2027, 5, 23), date(2027, 3, 5), "2027-03"),  # probe P1: a 31-day month
    (date(2027, 2, 1), date(2027, 4, 25), date(2027, 2, 5), "2027-02"),  # probe P1b: February
])
def test_review4_pending_maternity_for_a_whole_month_carries_exactly_the_month(w, start, end, birth, period):
    e, _req = mother(w, start, end, birth)
    ps = slip(calculated(w, period), e)
    assert not ps.refused
    assert ps.pending_gosi_amount == D("15500.00") and ps.gross == 0 and ps.net == 0


def test_review4_unpaid_leave_for_a_whole_month_is_the_whole_month(w):
    """Probe P2: 31 days of unpaid leave deducted 31/30 and refused the payslip; 28 February days deducted 28/30."""
    n1, n2 = expat(w), expat(w)
    unpaid_leave(w, n1, date(2027, 3, 1), date(2027, 3, 31))
    unpaid_leave(w, n2, date(2027, 2, 1), date(2027, 2, 28))
    feb = slip(calculated(w, "2027-02"), n2)
    mar = slip(calculated(w, "2027-03"), n1)
    for ps in (feb, mar):
        assert not ps.refused and ps.gross == 0
        assert sum(l.amount for l in ps.lines.filter(line_type="unpaid_deduction")) == D("8000.00")
        assert set(ps.lines.filter(line_type="unpaid_deduction").values_list("component__code", flat=True)) == {
            "BASIC", "HOUSING", "TRANSPORT"}  # per component (C-C2)


@pytest.mark.parametrize("start,end,period,expected", [
    (date(2027, 3, 31), date(2027, 3, 31), "2027-03", "0"),  # day 31 carries no pay on the 30-day basis
    (date(2027, 3, 30), date(2027, 3, 31), "2027-03", "266.67"),  # 1/30 of 8,000.00
    (date(2027, 2, 28), date(2027, 2, 28), "2027-02", "800.00"),  # the last February day stands for days 28 to 30
])
def test_review4_day_31_and_february_boundaries(w, start, end, period, expected):
    n1 = expat(w)
    unpaid_leave(w, n1, start, end)
    run = calculated(w, period)
    assert amount(run, n1, "unpaid_deduction") == D(expected)
    assert slip(run, n1).gross == D("8000.00") - D(expected)


# --- review 2: an off-cycle run pays only what the period's runs have not paid ---------------------------------------

def test_review2_off_cycle_after_the_regular_run_pays_nothing_again(w):
    """Probe P3: the off-cycle run repaid the whole month."""
    s1 = saudi(w)
    approved(w, "2027-03")
    off = services.calculate_run(off_cycle(w, "2027-03", [s1]), user=w.p)
    assert not off.payslips.exists() and any("nothing left to pay" in x for x in off.warnings)
    with pytest.raises(PayrollError) as exc:
        services.submit_run(off, user=w.p)
    assert exc.value.code == "run_empty"


def test_review2_off_cycle_takes_only_new_inputs(w):
    s1 = saudi(w)
    approved(w, "2027-03")
    inp = services.create_input(s1, user=w.p, kind="one_off_earning", period="2027-03", amount="700.00",
                                reason_ar="مكافأة")
    services.approve_input(inp, user=w.p2)
    off = approve(w, off_cycle(w, "2027-03", [s1]))
    ps = slip(off, s1)
    assert ps.gross == D("700.00") and amount(off, s1, "earning") == 0 and amount(off, s1, "employee_gosi") == 0
    assert entry_lines(off) == {"5100": D("700.00"), "2330": D("-700.00")}
    assert PayrollInput.objects.get(pk=inp.pk).taken_run_id == off.pk
    apr = calculated(w, "2027-04")
    assert amount(apr, s1, "one_off_earning") == 0  # taken once


def test_review2_off_cycle_runs_for_remuneration_wait_for_the_regular_run(w):
    """Slice 3 (R69) builds them: refused until the period's regular out-of-country run is approved
    (payroll/test_slice3_off_cycle.py)."""
    with pytest.raises(PayrollError) as exc:
        services.create_run(w.alm, user=w.p, period="2027-03", kind="off_cycle", reason_ar="سبب",
                            payroll_group="out_of_country", employees=[saudi(w)])
    assert exc.value.code == "regular_run_open"


def test_c_a2_refused_payslip_does_not_block_the_run_and_is_paid_off_cycle(w):
    s1 = saudi(w)
    s7 = saudi(w, cohort="")
    run = calculated(w, "2027-03")
    with pytest.raises(PayrollError) as exc:
        services.submit_run(run, user=w.p)
    assert exc.value.code == "payslips_refused"
    run = services.approve_run(services.submit_run(run, user=w.p, exclude_refused=True), user=w.h)
    assert any("without 1 refused payslip" in x for x in run.warnings)
    assert entry_lines(run)["2330"] == -slip(run, s1).net  # nothing for S7
    hr.update_employee(s7, user=w.h, gosi_cohort="legacy_m33")
    off = approve(w, off_cycle(w, "2027-03", [s7]))
    ps = slip(off, s7)
    assert ps.gross == D("13500.00") and amount(off, s7, "employee_gosi") == D("1218.75")
    apr = calculated(w, "2027-04")
    assert amount(apr, s7, "retro") == 0
    assert not slip(apr, s7).lines.filter(origin_period="2027-03").exists()  # March is not paid or charged again


@pytest.mark.parametrize("decision", ["refused", "confirmed"])
def test_review2_off_cycle_settles_carried_maternity_days_once(w, decision):
    e, req = mother(w, date(2027, 3, 1), date(2027, 5, 23), date(2027, 3, 5))
    mar = approved(w, "2027-03")
    assert slip(mar, e).pending_gosi_amount == D("15500.00")
    if decision == "refused":
        services.record_maternity_case(req, user=w.h, status="gosi_refused", refusal_reference="GOSI-REF-1")
    else:
        services.record_maternity_case(req, user=w.h, status="gosi_confirmed", gosi_reference="GOSI-M-1",
                                       confirmed_from=date(2027, 3, 1), confirmed_to=date(2027, 5, 31),
                                       monthly_amount="12500.00")
    off = approve(w, off_cycle(w, "2027-03", [e]))
    ps = slip(off, e)
    released = sum(l.amount for l in ps.lines.filter(line_type="retro", source_ref="release:maternity_suspended"))
    if decision == "refused":
        assert released == D("15500.00") and ps.gross == D("15500.00")
    else:
        assert released == 0 and amount(off, e, "maternity_top_up") == D("3000.00") and ps.gross == D("3000.00")
    assert amount(off, e, "employer_gosi") == 0  # March GOSI was already booked on the full wage
    apr = calculated(w, "2027-04")
    assert not slip(apr, e).lines.filter(line_type="retro", origin_period="2027-03").exists()
    assert amount(apr, e, "maternity_top_up", origin_period="2027-03") == 0


# --- review 10: a month never paid is re-examined -------------------------------------------------------------------

def test_review10_late_approved_new_hire_gets_the_earlier_month_as_retro(w):
    """Probe P4: the February contract was approved after the February run; March paid nothing for February."""
    saudi(w)
    feb = approved(w, "2027-02")
    late = expat(w, hire_date=date(2027, 2, 1))
    assert not feb.payslips.filter(employee=late).exists()
    mar = calculated(w, "2027-03")
    retro = slip(mar, late).lines.filter(line_type="retro", origin_period="2027-02")
    assert sum(l.amount for l in retro) == D("8000.00")
    assert amount(mar, late, "employer_gosi", origin_period="2027-02") == D("150.00")  # OH on the retro wage 7,500


# --- review 9: inputs approved after their period's run go to the next run -----------------------------------------

def test_review9_input_approved_after_the_run_before_the_cutoff_is_taken_by_the_next_run(w):
    """Probe probe2: the input kept payroll_period 2027-03 and no run ever took it."""
    n1 = expat(w)
    w.pclock.set(datetime(2027, 3, 20, 12, 0))
    mar = approved(w, "2027-03")
    w.pclock.set(datetime(2027, 3, 22, 12, 0))
    inp = services.approve_input(services.create_input(n1, user=w.p, kind="one_off_earning", period="2027-03",
                                                       amount="1000.00", reason_ar="مكافأة"), user=w.p2)
    assert inp.payroll_period == "2027-03" and amount(mar, n1, "one_off_earning") == 0
    w.pclock.set(datetime(2027, 4, 20, 12, 0))
    apr = approved(w, "2027-04")
    assert amount(apr, n1, "one_off_earning") == D("1000.00")
    assert PayrollInput.objects.get(pk=inp.pk).taken_run_id == apr.pk
    services.reverse_run(apr, user=w.c, reason="error")
    assert PayrollInput.objects.get(pk=inp.pk).taken_run_id is None  # free again for the replacing run
    apr2 = calculated(w, "2027-04")
    assert amount(apr2, n1, "one_off_earning") == D("1000.00")


def test_review9_an_input_taken_meanwhile_forces_a_recalculation(w):
    s1 = saudi(w)
    approved(w, "2027-03")
    inp = services.approve_input(services.create_input(s1, user=w.p, kind="bonus", period="2027-03",
                                                       amount="300.00", reason_ar="مكافأة"), user=w.p2)
    off = services.submit_run(services.calculate_run(off_cycle(w, "2027-03", [s1]), user=w.p), user=w.p)
    apr = approved(w, "2027-04")  # takes the bonus first
    assert PayrollInput.objects.get(pk=inp.pk).taken_run_id == apr.pk
    with pytest.raises(PayrollError) as exc:
        services.approve_run(off, user=w.h)
    assert exc.value.code == "recalculate"


# --- C-A2: pay date more than a month after the previous one ---------------------------------------------------------

def test_c_a2_pay_date_more_than_a_month_after_the_previous_is_a_warning(w):
    saudi(w)
    approved(w, "2027-03")  # pay date 2027-03-27
    late = calculated(w, "2027-04", pay_date=date(2027, 4, 30))
    assert any("Labour Law Art. 90" in x for x in late.warnings)


def test_c_a2_pay_date_on_the_same_day_next_month_is_no_warning(w):
    saudi(w)
    approved(w, "2027-03")  # pay date 2027-03-27
    apr = calculated(w, "2027-04")  # 2027-04-27: 31 days later, the same day of the next month
    assert not any("Art. 90" in x for x in apr.warnings)


# --- review 13: the run shows counts; employee details stay on the payslips ------------------------------------------

def test_review13_run_warnings_are_counts_only(w):
    n1 = expat(w, gosi_number="")  # unregistered: a payslip warning
    leaver = expat(w)
    t = hr.record_termination(leaver, user=w.h, kind="employer_termination", last_working_day=date(2027, 3, 20),
                              notice_given_on=date(2027, 1, 19), reason_ar="إنهاء")
    hr.approve_termination(hr.submit_termination(t, user=w.h), user=w.c)
    run = calculated(w, "2027-03")
    text = " ".join(run.warnings)
    assert n1.employee_code not in text and leaver.employee_code not in text
    assert "1 payslip(s) have warnings" in text and "1 employee(s) with an approved termination" in text
    assert any(leaver.employee_code in x for x in run.employee_notes)
    assert any("31(2)" in x for x in slip(run, n1).warnings)


def test_review4_sick_leave_reduction_on_day_31_carries_nothing(w):
    """Probe P9: a sick day on the 31st was reduced although day 31 carries no pay on the 30-day basis. Sick from
    2027-10-01: days 1 to 30 full pay, 31 to 90 at 75% (to 12-29), 91 to 120 without pay (Art. 117)."""
    n1 = expat(w)
    req = hr.request_leave(n1, "SICK", user=w.h, start_date=date(2027, 10, 1), end_date=date(2027, 12, 31),
                           document_ref="MED-1")
    hr.approve_leave(req, user=w.h2)
    dec = calculated(w, "2027-12")
    # December 1 to 29 at 25% off (29/30), December 30 fully off (1/30), December 31 weight 0.
    assert amount(dec, n1, "sick_deduction") == D("2200.00")
    assert slip(dec, n1).gross == D("5800.00")
