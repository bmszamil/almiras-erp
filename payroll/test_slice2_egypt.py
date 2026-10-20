"""Payroll slice 2 (§10.8 item 7): Egypt payroll (jurisdiction EG, EGP) from the seeded research rows of
docs/compliance/egypt-payroll-2026-10-18.md: social insurance 11% / 18.75% within the year's limits, salary tax with
the personal exemption and the high-income band exclusion, the Martyrs' Fund, Universal Health Insurance where the
work governorate is live, no EOSB accrual, the Egypt deduction caps, bilingual payslips, and runs refused while any
row they read is unverified."""
from datetime import date
from types import SimpleNamespace

import pytest

from core.access.testing import make_user, member
from hr import services as hr
from hr.conftest import contract, employee
from hr.models import StatutoryParameter
from ledger.services_setup import create_monthly_periods
from payroll import services
from payroll.conftest import D, entry_lines, saudi, slip
from payroll.models import PayrollError

WAGE = {"BASIC": "10000.00", "HOUSING": "2000.00", "TRANSPORT": "1000.00"}  # gross and insurable 13,000.00


@pytest.fixture
def eg(w):
    create_monthly_periods(w.egy, 2026)
    create_monthly_periods(w.egy, 2027)
    # Egypt is activated from 2026-10-17 (jurisdiction.active) and its 2026 insurance limits are seeded: the runs are
    # for October and November 2026, with the payroll clock in October 2026.
    from datetime import datetime
    w.pclock.set(datetime(2026, 10, 20, 12, 0))
    w.eg_dept = hr.create_department(w.egy, user=w.he, code="OPS", name_en="Operations", name_ar="العمليات")
    w.eg_grade = hr.create_grade(w.egy, user=w.he, code="G1", name_en="Staff", name_ar="موظف", overtime_eligible=False)
    return w


def egyptian(w, components=None, hire=date(2026, 1, 1), **fields):
    emp = employee(w, w.egy, basis="local_entity_employee", hire_date=hire, iban="EG380019000500000000263180002",
                   **fields)
    contract(w, emp, dict(components or WAGE), department=w.eg_dept, grade=w.eg_grade, contract_type="fixed",
             end_date=date(2028, 12, 31), notice_days_employer=90, notice_days_employee=90, qiwa_contract_ref="",
             currency="EGP")
    return emp


def verify_egypt():
    mp = make_user("eg-verifier")
    from core.models import Entity
    member(mp, Entity.objects.get(code="ALM"), "managing_partner")
    for row in StatutoryParameter.objects.filter(jurisdiction="EG", status="unverified"):
        hr.verify_statutory_parameter(row, user=mp)


def run_eg(w, period="2026-10"):
    return services.calculate_run(services.create_run(w.egy, user=w.pe, period=period), user=w.pe)


def test_eg_payslip_social_insurance_martyrs_fund_and_salary_tax_from_egypt_rows_only(eg):
    w = eg
    e1 = egyptian(w)
    run = run_eg(w)
    ps = slip(run, e1)
    assert not ps.refused and ps.currency_id == "EGP" and ps.gross == D("13000.00")
    amounts = {l.line_type: l.amount for l in ps.lines.all()}
    assert amounts["si_employee"] == D("1430.00") and amounts["si_employer"] == D("2437.50")  # 11% / 18.75%
    assert amounts["martyrs_fund"] == D("6.50")  # 0.05% of the gross
    # Taxable 11,570.00 a month x 12 = 138,840.00 - 20,000.00 exemption = 118,840.00: 15,000 x 10% + 15,000 x 15%
    # + 48,840 x 20% = 13,518.00 a year, 1,126.50 a month.
    assert amounts["salary_tax"] == D("1126.50") and ps.taxable_income == D("11570.00")
    assert ps.net == D("13000.00") - D("1430.00") - D("6.50") - D("1126.50")
    assert ps.eosb_accrual == 0 and "eosb_accrual" not in amounts  # no statutory gratuity in Egypt
    assert ps.gosi_wage == D("13000.00")
    assert {StatutoryParameter.objects.get(pk=pk).jurisdiction for pk in run.parameter_ids} == {"EG"}  # never SA (R51)
    assert any("Universal Health Insurance not computed" in x for x in ps.warnings)
    assert any("unverified research parameter" in x for x in run.warnings)


def test_eg_run_refused_until_every_row_it_reads_is_verified_then_posts(eg):
    w = eg
    egyptian(w)
    run = run_eg(w)
    with pytest.raises(PayrollError) as exc:
        services.submit_run(run, user=w.pe)
    assert exc.value.code == "parameter_unverified" and "EG si.rates" in exc.value.messages[0]
    verify_egypt()
    run = services.calculate_run(run, user=w.pe)
    run = services.submit_run(run, user=w.pe)
    run = services.approve_run(run, user=w.he)
    assert run.number == "EGY-PAY-2026-000001" and run.journal_entry.date == date(2026, 10, 31)
    assert entry_lines(run) == {"5100": D("13000.00"), "5110": D("2437.50"), "2340": D("-3867.50"),
                                "2220": D("-1126.50"), "2300": D("-6.50"), "2330": D("-10437.00")}
    assert all(l.currency_id == "EGP" for l in run.journal_entry.lines.all())


def test_eg_a_new_unverified_row_blocks_the_run_again(eg):
    w = eg
    egyptian(w)
    verify_egypt()
    run = services.submit_run(run_eg(w), user=w.pe)
    row = StatutoryParameter.objects.get(jurisdiction="EG", code="levy.martyrs_fund")
    hr.add_statutory_parameter(user=w.h, jurisdiction="EG", code="levy.martyrs_fund",
                               value={"rate": "0.0005", "base": "gross_monthly_salary"}, valid_from=date(2026, 10, 1),
                               source_url=row.source_url, source_reference="re-entered by the adviser")
    with pytest.raises(PayrollError) as exc:  # the recomputation reads the new row
        services.approve_run(run, user=w.he)
    assert exc.value.code == "recalculate"
    run = services.calculate_run(services.return_run(run, user=w.he, reason_ar="معيار جديد"), user=w.pe)
    with pytest.raises(PayrollError) as exc:
        services.submit_run(run, user=w.pe)
    assert exc.value.code == "parameter_unverified" and "levy.martyrs_fund (2026-10-01)" in exc.value.messages[0]


def test_eg_cumulative_withholding_second_month_and_wage_limit_years(eg):
    w = eg
    e1 = egyptian(w, {"BASIC": "20000.00"})  # insurable capped at 16,700.00 (2026)
    verify_egypt()
    oct_ = services.approve_run(services.submit_run(run_eg(w), user=w.pe), user=w.he)
    ps = slip(oct_, e1)
    assert ps.gosi_wage == D("16700.00") and {l.line_type: l.amount for l in ps.lines.all()}["si_employee"] == D("1837.00")
    nov = run_eg(w, "2026-11")
    tax_oct = sum(l.amount for l in ps.lines.filter(line_type="salary_tax"))
    tax_nov = sum(l.amount for l in slip(nov, e1).lines.filter(line_type="salary_tax"))
    assert tax_nov == tax_oct  # same pay: the year-to-date share less what October withheld
    jan27 = run_eg(w, "2027-01")  # NOSI's 2027 limits are not seeded: refused, never the 2026 row
    assert slip(jan27, e1).refused and "2027" in slip(jan27, e1).refusal


def test_eg_high_income_band_exclusion_columns_and_residents_only_zero_band(eg):
    ctx = SimpleNamespace(use=hr.ParamUse(), end=date(2026, 10, 31))
    tax = services._eg_annual_tax
    assert tax(ctx, D("600000"), True) == 124750  # column up to 600,000: 0% first band
    assert tax(ctx, D("700000"), True) == 153750  # 600,001-700,000: the 0% band is lost (10%)
    assert tax(ctx, D("1300000"), True) == 327500  # above 1.2m: 25% on everything up to 1.2m, then 27.5%
    assert tax(ctx, D("100000"), True) == 9750 and tax(ctx, D("100000"), False) == 13750  # residents only
    assert tax(ctx, D("0"), True) == 0


def test_eg_uhi_where_the_work_governorate_is_live(eg):
    w = eg
    e1 = egyptian(w)
    services.configure_settings(w.egy, user=w.ce, work_governorate="port_said")
    ps = slip(run_eg(w), e1)
    amounts = {l.line_type: l.amount for l in ps.lines.all()}
    assert amounts["uhi_employee"] == D("130.00") and amounts["uhi_employer"] == D("520.00")
    assert any("Family UHI" in x for x in ps.warnings)
    services.configure_settings(w.egy, user=w.ce, work_governorate="cairo")  # not live: none
    ps = slip(run_eg(w, "2026-11"), e1)
    assert not ps.lines.filter(line_type__in=("uhi_employee", "uhi_employer")).exists()


def test_eg_deduction_caps_nafaqa_first_debts_a_quarter_never_the_saudi_caps(eg):
    w = eg
    e1 = egyptian(w)
    # wage after tax and insurance: 13,000.00 - 1,430.00 - 6.50 - 1,126.50 = 10,437.00; 50% 5,218.50; 25% 2,609.25
    ok = services.create_input(e1, user=w.pe, kind="one_off_deduction", period="2026-10", amount="5000.00",
                               reason_ar="نفقة", deduction_basis="nafaqa", reference="N1")
    services.approve_input(ok, user=make_payroll_approver(w))
    ps = slip(run_eg(w), e1)
    assert not ps.refused  # 5,000.00 nafaqa within 50% (the Saudi 50% total would be on 13,000.00)
    debt = services.create_input(e1, user=w.pe, kind="one_off_deduction", period="2026-10", amount="300.00",
                                 reason_ar="دين", deduction_basis="written_consent", consent_ref="C-1", reference="D1")
    services.approve_input(debt, user=make_payroll_approver(w))
    ps = slip(run_eg(w, "2026-11"), e1)
    assert ps.refused and "Art. 113" in ps.refusal  # with the nafaqa, debts may use only 218.50 of the 50%


def make_payroll_approver(w):
    if not hasattr(w, "pe2"):
        w.pe2 = make_user("pe2")
        member(w.pe2, w.egy, "payroll_officer")
    return w.pe2


def test_eg_inputs_overtime_and_saudi_bases_refused(eg):
    w = eg
    e1 = egyptian(w)
    with pytest.raises(PayrollError):
        services.create_input(e1, user=w.pe, kind="overtime_hours", period="2026-10", quantity="2",
                              overtime_date=date(2026, 10, 3), reason_ar="عمل")
    with pytest.raises(PayrollError):
        services.create_input(e1, user=w.pe, kind="one_off_deduction", period="2026-10", amount="100.00",
                              reason_ar="إتلاف", deduction_basis="damage_art91")
    s1 = saudi(w)
    with pytest.raises(PayrollError):  # nafaqa is an Egypt basis
        services.create_input(s1, user=w.p, kind="one_off_deduction", period="2027-03", amount="100.00",
                              reason_ar="نفقة", deduction_basis="nafaqa")


def test_eg_payslip_is_bilingual_with_the_insurable_wage_label(eg):
    w = eg
    e1 = egyptian(w)
    verify_egypt()
    run = services.approve_run(services.submit_run(run_eg(w), user=w.pe), user=w.he)
    html = services.render_payslip(slip(run, e1), user=w.pe)
    assert 'lang="ar" dir="rtl"' in html and "الأجر التأميني" in html and "Insurable wage" in html
    assert "ضريبة كسب العمل" in html and "Salary tax withheld" in html and "EGP" in html


def test_eg_no_mudad_or_gosi_alerts(eg):
    w = eg
    egyptian(w)
    verify_egypt()
    run = services.approve_run(services.submit_run(run_eg(w), user=w.pe), user=w.he)
    from datetime import timedelta
    from payroll.models import PayrollAlert
    services.send_alerts(today=run.pay_date + timedelta(days=7), entity=w.egy)
    assert not PayrollAlert.objects.filter(entity=w.egy).exists()
