"""Payroll slice 2 (§10.8 item 4): carried maternity days settled off-cycle once GOSI decides; retro pay and GOSI
catch-up for remuneration staff; hourly-paid GOSI (Regs Art. 18(3), parameter unverified); the leave liability report
(hr OQ-6 default: nothing posted)."""
from datetime import date

import pytest

from hr import services as hr
from hr.models import StatutoryParameter
from payroll import services
from payroll.conftest import D, amount, approved, calculated, contract, hired, rate, saudi, slip
from payroll.models import GosiCatchUpNotice, PayrollAlert, PayrollError
from payroll.test_maternity import mother


def approve(w, run):
    return services.approve_run(services.submit_run(run, user=w.p), user=w.h)


# --- carried maternity days (R61(b) -> (a) / (c)) --------------------------------------------------------------

def test_r61_refused_case_alerts_and_settles_the_carried_month_off_cycle_once(w):
    e, req = mother(w, date(2027, 3, 1), date(2027, 5, 23), date(2027, 3, 5))
    mar = approved(w, "2027-03")
    assert slip(mar, e).pending_gosi_amount == D("15500.00")
    case = services.record_maternity_case(req, user=w.h, status="pending_gosi")
    with pytest.raises(PayrollError) as exc:
        services.prepare_maternity_settlement(case, user=w.p)
    assert exc.value.code == "maternity_pending"
    case = services.record_maternity_case(req, user=w.h, status="gosi_refused", refusal_reference="GOSI-REF-1")
    alert = PayrollAlert.objects.get(kind="maternity_decided", recipient=w.p)
    assert "2027-03" in alert.text_en and e.employee_code in alert.text_en and "2027-03" in alert.text_ar
    [off] = services.prepare_maternity_settlement(case, user=w.p)
    assert off.kind == "off_cycle" and off.period == "2027-03" and list(off.off_cycle_employees.all()) == [e]
    ps = slip(off, e)
    assert sum(l.amount for l in ps.lines.filter(source_ref="release:maternity_suspended")) == D("15500.00")
    with pytest.raises(PayrollError):  # prepared and waiting for approval: not prepared twice
        services.prepare_maternity_settlement(case, user=w.p)
    approve(w, off)
    with pytest.raises(PayrollError) as exc:
        services.prepare_maternity_settlement(case, user=w.p)
    assert exc.value.code == "maternity_nothing"


def test_r61_confirmed_case_pays_the_top_up_of_the_carried_month(w):
    e, req = mother(w, date(2027, 3, 1), date(2027, 5, 23), date(2027, 3, 5))
    approved(w, "2027-03")
    case = services.record_maternity_case(req, user=w.h, status="gosi_confirmed", gosi_reference="GOSI-M-1",
                                          confirmed_from=date(2027, 3, 1), confirmed_to=date(2027, 5, 31),
                                          monthly_amount="12500.00")
    [off] = services.prepare_maternity_settlement(case, user=w.p)
    assert amount(off, e, "maternity_top_up") == D("3000.00") and slip(off, e).gross == D("3000.00")
    approve(w, off)
    with pytest.raises(PayrollError):
        services.prepare_maternity_settlement(case, user=w.p)


# --- remuneration staff: retro and GOSI catch-up -------------------------------------------------------------------

def remuneration(w, **fields):
    return hired(w, basis="remuneration", **fields)


SAUDI_SUB = dict(nationality="SA", id_type="national_id", country_of_residence="AE", gosi_cohort="new_m273",
                 gosi_first_coverage_date=date(2025, 1, 1), gosi_employment_relationship="subordinate",
                 gosi_relationship_ref="AGREEMENT-1")


def test_r14_remuneration_retro_in_the_contract_currency_at_the_runs_rate(w):
    r1 = remuneration(w)
    rate("USD", "SAR", date(2027, 3, 31), "3.7500")
    rate("USD", "SAR", date(2027, 4, 30), "3.7600")
    approved(w, "2027-03", payroll_group="out_of_country")
    contract(w, r1, {"REMUNERATION": "6000.00"}, effective_date=date(2027, 3, 1), start_date=date(2027, 1, 1))
    apr = calculated(w, "2027-04", payroll_group="out_of_country")
    ps = slip(apr, r1)
    retro = ps.lines.get(line_type="retro", origin_period="2027-03")
    assert (retro.currency_id, retro.amount_txn, retro.amount) == ("USD", D("1000.000"), D("3760.00"))
    assert ps.gross_txn == D("7000.000") and not ps.refused
    apr = approve(w, apr)
    rate("USD", "SAR", date(2027, 5, 31), "3.7500")
    may = calculated(w, "2027-05", payroll_group="out_of_country")
    assert not slip(may, r1).lines.filter(origin_period="2027-03").exists()  # paid once


def test_r58_r62_saudi_remuneration_gosi_on_retro_wage_and_missed_share_caught_up(w):
    r2 = remuneration(w, id_number="1888888811", **SAUDI_SUB)
    rate("USD", "SAR", date(2027, 3, 31), "3.7500")
    rate("USD", "SAR", date(2027, 4, 30), "3.7500")
    mar = approved(w, "2027-03", payroll_group="out_of_country")
    assert amount(mar, r2, "employee_gosi") == D("1875.00")  # 10% of SAR 18,750.00
    # A later rate row for March (e.g. a corrected parameter) leaves a missed share; a raise effective March leaves
    # a retro wage.
    row = StatutoryParameter.objects.filter(code="gosi.rates.annuities").order_by("-valid_from").first()
    hr.add_statutory_parameter(user=w.h, jurisdiction="SA", code="gosi.rates.annuities", valid_from=date(2027, 3, 1),
                               value={"new_m273": {"employer": "11", "employee": "11"},
                                      "legacy_m33": {"employer": "9", "employee": "9"}},
                               source_url=row.source_url, source_reference="test: corrected step")
    contract(w, r2, {"REMUNERATION": "6000.00"}, effective_date=date(2027, 3, 1), start_date=date(2027, 1, 1))
    apr = calculated(w, "2027-04", payroll_group="out_of_country")
    ps = slip(apr, r2)
    assert not ps.refused, ps.refusal
    march = ps.lines.filter(origin_period="2027-03")
    # Retro wage 3,750.00 (USD 1,000 x 3.75): both shares 11% = 412.50; missed 1% on the covered 18,750.00 = 187.50.
    assert sum(l.amount for l in march.filter(line_type="employer_gosi")) == D("600.00")
    assert sum(l.amount for l in march.filter(line_type="employee_gosi")) == D("412.50")
    catch = march.get(line_type="gosi_catch_up")
    assert (catch.amount, catch.amount_txn, catch.account.code) == (D("187.50"), D("50.000"), "2340")
    apr = approve(w, apr)
    notice = GosiCatchUpNotice.objects.get(run=apr, employee=r2)
    assert notice.period_of_origin == "2027-03" and notice.amount == D("187.50") and notice.text_ar
    e = {}
    for l in apr.journal_entry.lines.select_related("account"):
        e[l.account.code] = e.get(l.account.code, D("0")) + l.debit - l.credit
    assert sum(e.values()) == 0


def test_r58_legacy_saudi_remuneration_missed_share_is_borne_by_the_employer(w):
    fields = dict(SAUDI_SUB, gosi_cohort="legacy_m33")
    r2 = remuneration(w, id_number="1888888812", **fields)
    rate("USD", "SAR", date(2027, 3, 31), "3.7500")
    rate("USD", "SAR", date(2027, 4, 30), "3.7500")
    approved(w, "2027-03", payroll_group="out_of_country")
    row = StatutoryParameter.objects.filter(code="gosi.rates.annuities").order_by("-valid_from").first()
    hr.add_statutory_parameter(user=w.h, jurisdiction="SA", code="gosi.rates.annuities", valid_from=date(2027, 3, 1),
                               value={"new_m273": {"employer": "10", "employee": "10"},
                                      "legacy_m33": {"employer": "10", "employee": "10"}},
                               source_url=row.source_url, source_reference="test: corrected legacy rate")
    ps = slip(calculated(w, "2027-04", payroll_group="out_of_country"), r2)
    borne = ps.lines.get(origin_period="2027-03", source_ref="borne:annuities")
    assert borne.line_type == "employer_gosi" and borne.amount == D("187.50")
    assert not ps.lines.filter(line_type="gosi_catch_up").exists()
    assert any("19(4)" in x for x in ps.warnings)


# --- hourly-paid GOSI (Regs Art. 18(3)) ------------------------------------------------------------------------------

def test_regs_18_3_hourly_contract_gosi_refused_until_the_rule_is_configured(w):
    s1 = saudi(w, contract_kwargs=dict(pay_rate_basis="hourly", paid_monthly=False, notice_days_employer=30,
                                       notice_days_employee=30))
    d1 = saudi(w, id_number="1000000777", contract_kwargs=dict(pay_rate_basis="daily", paid_monthly=False,
                                                               notice_days_employer=30, notice_days_employee=30))
    run = calculated(w)
    assert slip(run, s1).refused and "18(3)" in slip(run, s1).refusal and "المادة 18(3)" in slip(run, s1).refusal
    assert not slip(run, d1).refused and any("18(1)" in n for n in slip(run, d1).notes)
    row = StatutoryParameter.objects.get(code="gosi.variable_wage_basis")
    value = dict(row.value, hourly={"method": "monthly_equivalent"})
    hr.add_statutory_parameter(user=w.h, jurisdiction="SA", code="gosi.variable_wage_basis", value=value,
                               valid_from=date(2027, 3, 1), source_url=row.source_url,
                               source_reference="test: Governor's hourly rule configured")
    run = services.calculate_run(run, user=w.p)
    assert not slip(run, s1).refused and slip(run, s1).gosi_wage == D("12500.00")
    with pytest.raises(Exception):  # an hourly wage is not paid monthly (Art. 75)
        saudi(w, id_number="1000000778", contract_kwargs=dict(pay_rate_basis="hourly"))


# --- leave liability (hr OQ-6 default) --------------------------------------------------------------------------------

def test_hr_oq6_leave_liability_report_posts_nothing_and_shows_rows_only_with_payslip_rights(w):
    # Slice 3 (R75): the report is a stored snapshot generated with payroll.prepare_liability_report and read with
    # liability_report_data (payroll/test_slice3_reports.py).
    s1 = saudi(w)
    w.pclock.set(date(2027, 6, 1))
    stored = services.leave_liability_report(w.alm, date(2027, 6, 1), user=w.a)
    report = services.liability_report_data(stored, user=w.c)
    row = report["lines"][0]
    assert row.employee_code == s1.employee_code and report["posted"] is False
    assert row.amount == (row.untaken_days * D("13500.00") / 30).quantize(D("0.01")) == report["total_amount"]
    viewer = services.liability_report_data(stored, user=w.v)
    assert viewer["lines"] == [] and viewer["total_amount"] == report["total_amount"]
    assert viewer["counts"]["valued"] == 1
    from ledger.models import JournalEntry
    assert not JournalEntry.objects.filter(entity=w.alm).exists()
