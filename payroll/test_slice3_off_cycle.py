"""Payroll slice 3, item 1 (docs/specs/payroll.md §11): off-cycle runs of the out-of-country group (remuneration
staff) and of Egypt (R69 to R74; E16, E17; AT-73 to AT-78). Anonymised fixtures; nothing external is called."""
from datetime import date, datetime

import pytest
from django.core.exceptions import PermissionDenied

from hr import services as hr
from invoicing.models import WhtCategory
from ledger import services as ledger
from ledger.models import FiscalPeriod
from payroll import services
from payroll.conftest import D, amount, approved, entry_lines, hired, rate, saudi, slip
from payroll.models import PayrollError, PayrollInput, PayrollRun
from payroll.test_slice2_egypt import egyptian, eg  # noqa: F401  (the Egypt fixture)

SAUDI_ABROAD = dict(nationality="SA", id_type="national_id", country_of_residence="AE", gosi_cohort="new_m273",
                    gosi_first_coverage_date=date(2025, 1, 1))


def remuneration(w, **fields):
    return hired(w, basis="remuneration", **fields)


def off_cycle(w, period, employees, entity=None, user=None, group="out_of_country", **kw):
    return services.create_run(entity or w.alm, user=user or w.p, period=period, kind="off_cycle",
                               payroll_group=group, reason_ar="صرف المستحق المتبقي", employees=employees, **kw)


def through(w, run, approver=None, user=None):
    run = services.calculate_run(run, user=user or w.p)
    run = services.submit_run(run, user=user or w.p)
    return services.approve_run(run, user=approver or w.h)


def march_with_r2_refused(w):
    """AT-73 given: the March out-of-country run approved with R1 paid and R2 refused (relationship cleared), then
    R2's relationship recorded ``subordinate``."""
    rate("USD", "SAR", date(2027, 3, 31), "3.7500")
    r1 = remuneration(w)
    r2 = remuneration(w, id_number="1888888811", gosi_employment_relationship="subordinate",
                      gosi_relationship_ref="AGREEMENT-R2", **SAUDI_ABROAD)
    hr.update_employee(r2, user=w.h, gosi_employment_relationship="", gosi_relationship_ref="")
    run = services.calculate_run(services.create_run(w.alm, user=w.p, period="2027-03",
                                                     payroll_group="out_of_country"), user=w.p)
    assert slip(run, r2).refused and "relationship" in slip(run, r2).refusal
    run = services.approve_run(services.submit_run(run, user=w.p, exclude_refused=True), user=w.h)
    hr.update_employee(r2, user=w.h, gosi_employment_relationship="subordinate", gosi_relationship_ref="AGREEMENT-R2")
    return r1, r2, run


# --- R69 to R72: out-of-country --------------------------------------------------------------------------------

def test_at73_e16_off_cycle_remuneration_pays_the_refused_payslip_with_annuities_only(w):
    r1, r2, regular = march_with_r2_refused(w)
    run = through(w, off_cycle(w, "2027-03", [r2]))
    assert {p.employee_id for p in run.payslips.all()} == {r2.pk}  # R1 is not in the run
    assert run.journal_entry.date == date(2027, 3, 31) and run.entry_date_reason == ""
    assert entry_lines(run) == {"5100": D("18750.00"), "5110": D("1875.00"), "2340": D("-3750.00"),
                                "2330": D("-16875.00")}  # exactly E16
    ps = slip(run, r2)
    assert ps.currency_id == "USD" and ps.net_txn == D("4500.000") and ps.gosi_wage == D("18750.00")
    assert set(ps.lines.exclude(gosi_branch="").values_list("gosi_branch", flat=True)) == {"annuities"}  # no OH/SANED
    assert not ps.lines.filter(line_type__in=("eosb_accrual", "loan_installment")).exists()
    assert run.number == "ALM-PAY-2027-000002" and regular.number == "ALM-PAY-2027-000001"  # the PAY sequence


def test_r69_off_cycle_waits_for_the_regular_run_names_its_group_and_never_an_agent(w):
    rate("USD", "SAR", date(2027, 3, 31), "3.7500")
    r1 = remuneration(w)
    s1 = saudi(w)
    with pytest.raises(PayrollError) as exc:  # no approved regular run of the group for the period
        off_cycle(w, "2027-03", [r1])
    assert exc.value.code == "regular_run_open" and " / " in exc.value.messages[0]
    regular = services.calculate_run(services.create_run(w.alm, user=w.p, period="2027-03",
                                                         payroll_group="out_of_country"), user=w.p)
    services.submit_run(regular, user=w.p)
    with pytest.raises(PayrollError) as exc:  # submitted is not approved
        off_cycle(w, "2027-03", [r1])
    assert exc.value.code == "regular_run_open"
    services.approve_run(PayrollRun.objects.get(pk=regular.pk), user=w.h)
    with pytest.raises(PayrollError) as exc:
        off_cycle(w, "2027-03", [s1])  # a KSA employee is not in the out-of-country group
    assert exc.value.code == "off_cycle_group"
    with pytest.raises(PayrollError) as exc:
        off_cycle(w, "2027-03", [r1], purpose="leave_advance")
    assert exc.value.code == "run_kind"
    with pytest.raises(PermissionDenied):  # R38: agents never prepare payroll runs
        off_cycle(w, "2027-03", [r1], user=w.agent)
    run = services.calculate_run(off_cycle(w, "2027-03", [r1]), user=w.p)
    assert not run.payslips.exists() and any("nothing left to pay" in x for x in run.warnings)  # R1 had nothing due
    with pytest.raises(PayrollError):
        services.submit_run(run, user=w.p)  # nothing to pay


def test_at74_one_off_bonus_off_cycle_no_gosi_input_taken_once_no_wps_and_wht_at_payment(w):
    r1, r2, regular = march_with_r2_refused(w)
    w.pclock.set(datetime(2027, 4, 8, 10, 0))  # after the 6th: the input's run would be April's
    inp = services.create_input(r1, user=w.p, kind="bonus", period="2027-03", amount="1000.00",
                                reason_ar="مكافأة لمرة واحدة", reason_en="One-off bonus")
    inp = services.approve_input(inp, user=w.p2)
    assert inp.payroll_period == "2027-04"
    run = through(w, off_cycle(w, "2027-03", [r1]))
    assert entry_lines(run) == {"5100": D("3750.00"), "2330": D("-3750.00")}  # USD 1,000.00 at 3.75, no GOSI
    ps = slip(run, r1)
    assert ps.gross_txn == D("1000.000") and not ps.lines.filter(line_type__in=("employee_gosi", "employer_gosi")).exists()
    assert PayrollInput.objects.get(pk=inp.pk).taken_run_id == run.pk
    rate("USD", "SAR", date(2027, 4, 30), "3.7500")
    april = services.calculate_run(services.create_run(w.alm, user=w.p, period="2027-04",
                                                       payroll_group="out_of_country"), user=w.p)
    assert amount(april, r1, "bonus") == 0  # taken once
    with pytest.raises(PayrollError) as exc:  # R72: no WPS for an out-of-country run
        services.generate_wps_file(run, user=w.p, format_code="generic_csv")
    assert exc.value.code == "wps_not_applicable" and " / " in exc.value.messages[0]
    with pytest.raises(PayrollError) as exc:
        services.record_payment(w.alm, user=w.p, kind="net_salaries", run=run, payment_date=date(2027, 4, 8),
                                amount="1000.00", currency="USD")
    assert exc.value.code == "wht_category_required"
    cat = WhtCategory.objects.create(code="test-remuneration", name_en="Test category", name_ar="فئة اختبار",
                                     rate=D("5.00"), valid_from=date(2020, 1, 1))
    rate("USD", "SAR", date(2027, 4, 8), "3.7600")
    p = services.record_payment(w.alm, user=w.p, kind="net_salaries", run=run, payment_date=date(2027, 4, 8),
                                amount="1000.00", currency="USD", wht_category=cat, payslips=[ps])
    p = services.approve_payment(services.submit_payment(p, user=w.p), user=w.c)
    assert p.wht_amount == D("50.000")  # 5% of R1's off-cycle gross (B2: the gross in the share of the net paid)


def test_regular_remuneration_run_records_the_inputs_it_pays(w):
    """Slice 3 fix: a regular remuneration payslip records the inputs it took, so a later run never pays them
    again."""
    rate("USD", "SAR", date(2027, 3, 31), "3.7500")
    rate("USD", "SAR", date(2027, 4, 30), "3.7500")
    r1 = remuneration(w)
    inp = services.approve_input(services.create_input(r1, user=w.p, kind="bonus", period="2027-03", amount="100.00",
                                                       reason_ar="مكافأة"), user=w.p2)
    march = approved(w, payroll_group="out_of_country")
    assert amount(march, r1, "bonus") == D("375.00")
    assert PayrollInput.objects.get(pk=inp.pk).taken_run_id == march.pk
    april = services.calculate_run(services.create_run(w.alm, user=w.p, period="2027-04",
                                                       payroll_group="out_of_country"), user=w.p)
    assert amount(april, r1, "bonus") == 0


def test_at75_closed_period_moves_the_entry_and_the_rate_and_currency_mix_is_refused(w):
    r1, r2, regular = march_with_r2_refused(w)
    rate("USD", "SAR", date(2027, 4, 30), "3.7500")
    rate("USD", "SAR", date(2027, 4, 1), "3.7600")
    rate("EUR", "SAR", date(2027, 4, 30), "4.1000")
    r3 = remuneration(w, id_number="P7777001")
    april = services.submit_run(services.calculate_run(services.create_run(
        w.alm, user=w.p, period="2027-04", payroll_group="out_of_country"), user=w.p), user=w.p)
    with pytest.raises(PayrollError) as exc:  # the April regular run is submitted, not approved
        off_cycle(w, "2027-04", [r1, r3])
    assert exc.value.code == "regular_run_open"
    services.approve_run(april, user=w.h)
    # R3's contract changes to EUR from 15 April, after an April payslip was approved in USD.
    from hr.conftest import contract
    contract(w, r3, {"REMUNERATION": "4500.00"}, effective_date=date(2027, 4, 15), start_date=r3.hire_date,
             currency="EUR")
    run = services.calculate_run(off_cycle(w, "2027-04", [r1, r3]), user=w.p)
    assert slip(run, r3).refused and "USD" in slip(run, r3).refusal and "EUR" in slip(run, r3).refusal
    assert not run.payslips.filter(employee=r1).exists() and any("nothing left to pay" in x for x in run.warnings)
    # March closed: an off-cycle for March is dated the first day of the next open period at that day's rate.
    ledger.close_period(FiscalPeriod.objects.get(entity=w.alm, start_date=date(2027, 3, 1)), user=w.c)
    inp = services.approve_input(services.create_input(r2, user=w.p, kind="one_off_earning", period="2027-03",
                                                       amount="200.00", reason_ar="بدل"), user=w.p2)
    march = services.calculate_run(off_cycle(w, "2027-03", [r2]), user=w.p)
    assert march.entry_date == date(2027, 4, 1) and march.entry_date_reason == "period_closed"
    assert any("closed" in x and "مقفلة" in x for x in march.warnings)
    ps = slip(march, r2)
    assert ps.fx_rate == D("3.7600") and amount(march, r2, "one_off_earning") == D("752.00")
    march = services.approve_run(services.submit_run(march, user=w.p), user=w.h)
    assert march.journal_entry.date == date(2027, 4, 1)
    assert PayrollInput.objects.get(pk=inp.pk).taken_run_id == march.pk


def test_r70_a_period_closed_after_the_calculation_forces_a_recalculation(w):
    r1, r2, regular = march_with_r2_refused(w)
    run = services.submit_run(services.calculate_run(off_cycle(w, "2027-03", [r2]), user=w.p), user=w.p)
    with pytest.raises(Exception) as exc:  # a submitted run keeps the period open (R12)
        ledger.close_period(FiscalPeriod.objects.get(entity=w.alm, start_date=date(2027, 3, 1)), user=w.c)
    assert "payroll run" in str(exc.value)
    run = services.return_run(run, user=w.h, reason_ar="إقفال الفترة")
    ledger.close_period(FiscalPeriod.objects.get(entity=w.alm, start_date=date(2027, 3, 1)), user=w.c)
    run = PayrollRun.objects.get(pk=run.pk)
    assert run.entry_date == date(2027, 3, 31)
    rate("USD", "SAR", date(2027, 4, 1), "3.7500")
    run = services.submit_run(services.calculate_run(run, user=w.p), user=w.p)
    assert run.entry_date == date(2027, 4, 1)
    assert services.approve_run(run, user=w.h).journal_entry.date == date(2027, 4, 1)


# --- R73, R74: Egypt -------------------------------------------------------------------------------------------

def egypt_2027(w):
    """EGY in April 2027: the 2027 insurance limits entered (NOSI publishes them each November) and every EG row
    verified by the MP test user; the payroll clock after the month."""
    hr.add_statutory_parameter(user=w.h, jurisdiction="EG", code="si.wage_limits",
                               value={"min_monthly": "2700.00", "max_monthly": "16700.00"}, valid_from=date(2027, 1, 1),
                               source_url="https://www.businesstodayegypt.com/Article/1/6950/",
                               source_reference="test: 2027 limits as 2026")
    from payroll.test_slice2_egypt import verify_egypt
    verify_egypt()
    w.pclock.set(datetime(2027, 5, 5, 12, 0))


def eg_regular(w, refuse=None, monkeypatch=None):
    """The April 2027 regular EGY run, approved; ``refuse`` (an employee) is refused in it (a refusal later fixed)."""
    if refuse is not None:
        real = services._eg_insurable_wage

        def refusing(ctx, slip_, info):
            if slip_.employee.pk == refuse.pk:
                slip_.refuse("Test refusal, fixed later.", "رفض اختباري يُصحح لاحقًا.")
            return real(ctx, slip_, info)
        monkeypatch.setattr(services, "_eg_insurable_wage", refusing)
    run = services.calculate_run(services.create_run(w.egy, user=w.pe, period="2027-04"), user=w.pe)
    run = services.submit_run(run, user=w.pe, exclude_refused=refuse is not None)
    run = services.approve_run(run, user=w.he)
    if refuse is not None:
        monkeypatch.setattr(services, "_eg_insurable_wage", real)
    return run


def test_at76_e17_egypt_off_cycle_pays_the_refused_payslip(eg, monkeypatch):
    w = eg
    l1 = egyptian(w, {"BASIC": "10000.00"})
    l0 = egyptian(w)
    egypt_2027(w)
    regular = eg_regular(w, refuse=l1, monkeypatch=monkeypatch)
    assert slip(regular, l1).refused and not slip(regular, l0).refused
    run = services.calculate_run(off_cycle(w, "2027-04", [l1], entity=w.egy, user=w.pe, group="in_country"),
                                 user=w.pe)
    run = services.approve_run(services.submit_run(run, user=w.pe), user=w.he)
    assert run.journal_entry.date == date(2027, 4, 30)
    assert entry_lines(run) == {"5100": D("10000.00"), "5110": D("1875.00"), "2340": D("-2975.00"),
                                "2220": D("-592.50"), "2300": D("-5.00"), "2330": D("-8302.50")}  # exactly E17
    ps = slip(run, l1)
    assert ps.currency_id == "EGP" and ps.net == D("8302.50")
    assert not ps.lines.filter(line_type__in=("eosb_accrual", "employee_gosi", "employer_gosi")).exists()
    with pytest.raises(PayrollError) as exc:  # R74: no WPS file
        services.generate_wps_file(run, user=w.pe, format_code="generic_csv")
    assert exc.value.code == "wps_not_applicable"
    with pytest.raises(PayrollError) as exc:  # R74: no Mudad
        services.record_mudad_justification(ps, user=w.pe, kind="non_payment", reason="other",
                                            description_ar="سبب")
    assert exc.value.code == "mudad_not_applicable"


def _eg_tax(income):
    """The seeded Egypt brackets (first column, residents) on an annual net taxable income."""
    bands = [(D("40000"), D("0")), (D("55000"), D("0.10")), (D("70000"), D("0.15")), (D("200000"), D("0.20")),
             (D("400000"), D("0.225"))]
    tax, lower = D("0"), D("0")
    for upper, r in bands:
        if income <= lower:
            break
        tax += (min(income, upper) - lower) * r
        lower = upper
    return tax


def test_at77_egypt_month_at_the_maximum_gets_no_insurance_and_tax_is_cumulative(eg):
    w = eg
    l2 = egyptian(w, {"BASIC": "20000.00"})
    egypt_2027(w)
    regular = eg_regular(w)
    reg = slip(regular, l2)
    assert reg.gosi_wage == D("16700.00")  # the year's maximum insurable wage
    w.pclock.set(datetime(2027, 5, 6, 12, 0))
    inp = services.approve_input(services.create_input(l2, user=w.pe, kind="bonus", period="2027-04", amount="2000.00",
                                                       reason_ar="مكافأة"), user=services_user(w))
    run = services.calculate_run(off_cycle(w, "2027-04", [l2], entity=w.egy, user=w.pe, group="in_country"),
                                 user=w.pe)
    ps = slip(run, l2)
    types = {l.line_type: l.amount for l in ps.lines.all()}
    assert "si_employee" not in types and "si_employer" not in types and "uhi_employee" not in types
    assert types["martyrs_fund"] == D("1.00")  # 0.05% of this run's gross
    withheld = amount(regular, l2, "salary_tax")
    # Compliance C1: the regular pay is annualised, the bonus added once and its tax due in full now.
    annual = reg.taxable_income * 12
    t_reg, t_all = _eg_tax(annual - D("20000")), _eg_tax(annual + D("2000.00") - D("20000"))
    expected = (t_reg / 12 + t_all - t_reg).quantize(D("0.01")) - withheld
    assert types["salary_tax"] == expected == D("400.00")  # the bonus at the 20% band
    assert ps.taxable_irregular == D("2000.00")
    assert ps.input_ids == [inp.pk]
    # Variant: one EG row back to unverified: calculated with a warning, refused at submission naming the row.
    row = hr.add_statutory_parameter(user=w.h, jurisdiction="EG", code="levy.martyrs_fund",
                                     value={"rate": "0.0005", "base": "gross_monthly_salary"},
                                     valid_from=date(2027, 4, 1), source_url="https://example.invalid/test",
                                     source_reference="test: re-entered")
    run = services.calculate_run(run, user=w.pe)
    assert any("unverified research parameter" in x for x in run.warnings)
    with pytest.raises(PayrollError) as exc:
        services.submit_run(run, user=w.pe)
    assert exc.value.code == "parameter_unverified" and "levy.martyrs_fund (2027-04-01)" in exc.value.messages[0]
    assert row.status == "unverified"


def services_user(w):
    """A second EGY payroll officer: inputs are approved by someone other than their creator."""
    from core.access.testing import make_user, member
    if not hasattr(w, "pe2"):
        w.pe2 = make_user("pe2")
        member(w.pe2, w.egy, "payroll_officer")
    return w.pe2


def test_at78_egypt_leaver_overtime_and_agent_are_refused(eg):
    w = eg
    egyptian(w)
    # L3's fixed-term contract ends on 2027-04-15 (in the period): never paid off-cycle (R74).
    from hr.conftest import contract, employee
    l3 = employee(w, w.egy, basis="local_entity_employee", hire_date=date(2026, 1, 1),
                  iban="EG380019000500000000263180002")
    contract(w, l3, {"BASIC": "9000.00"}, department=w.eg_dept, grade=w.eg_grade, contract_type="fixed",
             end_date=date(2027, 4, 15), notice_days_employer=90, notice_days_employee=90, qiwa_contract_ref="",
             currency="EGP")
    l4 = egyptian(w)
    egypt_2027(w)
    eg_regular(w)
    with pytest.raises(PayrollError) as exc:
        off_cycle(w, "2027-04", [l3], entity=w.egy, user=w.pe, group="in_country")
    assert exc.value.code == "eg_final_settlement_not_built" and " / " in exc.value.messages[0]
    with pytest.raises(PayrollError):  # overtime inputs stay refused in Egypt (R74)
        services.create_input(l4, user=w.pe, kind="overtime_hours", period="2027-04", quantity="2",
                              overtime_date=date(2027, 4, 4), reason_ar="عمل إضافي")
    with pytest.raises(PermissionDenied):  # R38
        off_cycle(w, "2027-04", [l4], entity=w.egy, user=w.agent, group="in_country")
    with pytest.raises(PayrollError) as exc:  # Egypt runs are in-country runs
        off_cycle(w, "2027-04", [l4], entity=w.egy, user=w.pe, group="out_of_country")
    assert exc.value.code == "run_kind"


def test_r74_egypt_exit_found_after_preparation_refuses_the_payslip(eg, monkeypatch):
    w = eg
    l1 = egyptian(w)
    egyptian(w)  # paid in the regular run
    egypt_2027(w)
    eg_regular(w, refuse=l1, monkeypatch=monkeypatch)
    run = off_cycle(w, "2027-04", [l1], entity=w.egy, user=w.pe, group="in_country")
    monkeypatch.setattr(services, "_eg_exit_problem", lambda emp, versions, end: ("Exit test.", "اختبار إنهاء."))
    run = services.calculate_run(run, user=w.pe)
    assert slip(run, l1).refused and "Exit test." in slip(run, l1).refusal


# --- slice 3 review and compliance fixes (2026-10-21) -----------------------------------------------------------

def test_review1_a_regular_run_is_not_reversed_after_its_off_cycle_and_never_pays_the_month_twice(w, monkeypatch):
    r1, r2, regular = march_with_r2_refused(w)
    off = through(w, off_cycle(w, "2027-03", [r2]))
    with pytest.raises(PayrollError) as exc:
        services.reverse_run(PayrollRun.objects.get(pk=regular.pk), user=w.c, reason="test reversal")
    assert exc.value.code == "off_cycle_approved" and " / " in exc.value.messages[0]
    # Defence in depth: even if the regular run were reversed, a new one nets the month against the off-cycle run.
    with monkeypatch.context() as m:
        m.setattr(services, "_slice3_off_cycle", lambda *a: False)
        services.reverse_run(PayrollRun.objects.get(pk=regular.pk), user=w.c, reason="test reversal")
    again = services.calculate_run(services.create_run(w.alm, user=w.p, period="2027-03",
                                                       payroll_group="out_of_country"), user=w.p)
    assert amount(again, r2, "earning") == 0 and amount(again, r2, "employer_gosi") == 0
    assert not again.payslips.filter(employee=r2).exists() or slip(again, r2).net_txn == 0
    assert amount(again, r1, "earning") == D("18750.00")  # R1's month was reversed with the regular run
    assert off.status == "approved"


def test_review2_c5_no_gosi_on_an_fx_difference_in_a_closed_period(w):
    rate("USD", "SAR", date(2027, 3, 31), "3.7500")
    r2 = remuneration(w, id_number="1888888811", gosi_employment_relationship="subordinate",
                      gosi_relationship_ref="AGREEMENT-R2", **SAUDI_ABROAD)
    reg = services.calculate_run(services.create_run(w.alm, user=w.p, period="2027-03",
                                                     payroll_group="out_of_country"), user=w.p)
    reg = services.approve_run(services.submit_run(reg, user=w.p), user=w.h)
    assert slip(reg, r2).gosi_wage == D("18750.00")
    ledger.close_period(FiscalPeriod.objects.get(entity=w.alm, start_date=date(2027, 3, 1)), user=w.c)
    rate("USD", "SAR", date(2027, 4, 1), "3.7600")
    services.approve_input(services.create_input(r2, user=w.p, kind="one_off_earning", period="2027-03",
                                                  amount="200.00", reason_ar="بدل"), user=w.p2)
    run = services.calculate_run(off_cycle(w, "2027-03", [r2]), user=w.p)
    ps = slip(run, r2)
    assert not ps.lines.exclude(gosi_branch="").exists() and ps.gosi_wage == 0  # REMUNERATION fully paid
    assert amount(run, r2, "one_off_earning") == D("752.00") and amount(run, r2, "earning") == 0


def eg_net_month(w, monkeypatch, governorate=None):
    if governorate:
        services.configure_settings(w.egy, user=w.ce, work_governorate=governorate)
    l1 = egyptian(w, {"BASIC": "10000.00"})
    egyptian(w)
    egypt_2027(w)
    regular = eg_regular(w, refuse=l1, monkeypatch=monkeypatch)
    off = services.calculate_run(off_cycle(w, "2027-04", [l1], entity=w.egy, user=w.pe, group="in_country"),
                                 user=w.pe)
    off = services.approve_run(services.submit_run(off, user=w.pe), user=w.he)
    return l1, regular, off


def test_review1_egypt_regular_after_reversal_nets_salary_si_and_the_uhi_minimum(eg, monkeypatch):
    w = eg
    l1, regular, off = eg_net_month(w, monkeypatch, governorate="port_said")
    types = {l.line_type: l.amount for l in slip(off, l1).lines.all()}
    assert types["uhi_employer"] == D("400.00") and types["uhi_employee"] == D("100.00")  # 4% and 1% of 10,000.00
    with pytest.raises(PayrollError) as exc:
        services.reverse_run(PayrollRun.objects.get(pk=regular.pk), user=w.ce, reason="test")
    assert exc.value.code == "off_cycle_approved"
    with monkeypatch.context() as m:
        m.setattr(services, "_slice3_off_cycle", lambda *a: False)
        services.reverse_run(PayrollRun.objects.get(pk=regular.pk), user=w.ce, reason="test")
    again = services.calculate_run(services.create_run(w.egy, user=w.pe, period="2027-04"), user=w.pe)
    for t in ("earning", "si_employee", "si_employer", "uhi_employee", "uhi_employer", "salary_tax"):
        assert amount(again, l1, t) == 0, t  # nothing left to pay: salary, SI and UHI booked by the off-cycle run


def test_r73_uhi_employer_minimum_applies_once_per_month(eg, monkeypatch):
    """Review 9: a low wage in a UHI-live governorate: the regular run booked the EGP 50 employer minimum; an
    off-cycle bonus in the same month adds no second minimum."""
    w = eg
    services.configure_settings(w.egy, user=w.ce, work_governorate="port_said")
    low = egyptian(w, {"BASIC": "3000.00"})  # 4% = 120.00
    # With the seeded limits 4% never falls below EGP 50, so the test sets a higher minimum (EGP 200) to see it bind.
    hr.add_statutory_parameter(user=w.h, jurisdiction="EG", code="uhi.rates",
                               value={"employee": "0.01", "employer": "0.04", "employer_min_monthly": "200.00",
                                      "spouse_not_working": "0.03", "per_dependent_child": "0.01",
                                      "base": "si_contribution_wage"},
                               valid_from=date(2027, 1, 1), source_url="https://example.invalid/uhi",
                               source_reference="test: higher employer minimum")
    egypt_2027(w)
    regular = eg_regular(w)
    assert amount(regular, low, "uhi_employer") == D("200.00")  # the monthly minimum
    w.pclock.set(datetime(2027, 5, 6, 12, 0))
    services.approve_input(services.create_input(low, user=w.pe, kind="bonus", period="2027-04", amount="500.00",
                                                 reason_ar="مكافأة"), user=services_user(w))
    off = services.calculate_run(off_cycle(w, "2027-04", [low], entity=w.egy, user=w.pe, group="in_country"),
                                 user=w.pe)
    assert amount(off, low, "uhi_employer") == 0 and amount(off, low, "si_employer") == 0


def test_r73_partial_si_netting_on_a_backdated_raise(eg):
    w = eg
    l1 = egyptian(w, {"BASIC": "10000.00"})
    egypt_2027(w)
    eg_regular(w)
    from hr.conftest import contract
    contract(w, l1, {"BASIC": "12000.00"}, department=w.eg_dept, grade=w.eg_grade, contract_type="fixed",
             end_date=date(2028, 12, 31), notice_days_employer=90, notice_days_employee=90, qiwa_contract_ref="",
             currency="EGP", effective_date=date(2027, 4, 1), start_date=l1.hire_date)
    off = services.calculate_run(off_cycle(w, "2027-04", [l1], entity=w.egy, user=w.pe, group="in_country"),
                                 user=w.pe)
    assert amount(off, l1, "earning") == D("2000.00")
    assert amount(off, l1, "si_employee") == D("220.00") and amount(off, l1, "si_employer") == D("375.00")
    assert slip(off, l1).gosi_wage == D("2000.00")


def test_c1_egypt_january_bonus_is_taxed_once_not_annualised(eg):
    w = eg
    l1 = egyptian(w, {"BASIC": "10000.00"})
    egypt_2027(w)
    w.pclock.set(datetime(2027, 1, 20, 12, 0))
    services.approve_input(services.create_input(l1, user=w.pe, kind="bonus", period="2027-01", amount="12000.00",
                                                 reason_ar="مكافأة سنوية"), user=services_user(w))
    run = services.calculate_run(services.create_run(w.egy, user=w.pe, period="2027-01"), user=w.pe)
    ps = slip(run, l1)
    # Regular 8,900.00 a month annualised: 86,800.00 taxable, 7,110.00 a year; with the bonus 98,800.00: 9,510.00.
    # January: 7,110.00 / 12 + (9,510.00 - 7,110.00) = 592.50 + 2,400.00.
    assert amount(run, l1, "salary_tax") == D("2992.50") and ps.taxable_irregular == D("12000.00")


def test_c10_egypt_si_on_the_january_wage_when_configured(eg):
    w = eg
    l1 = egyptian(w, {"BASIC": "10000.00"})
    from hr.conftest import contract
    contract(w, l1, {"BASIC": "12000.00"}, department=w.eg_dept, grade=w.eg_grade, contract_type="fixed",
             end_date=date(2028, 12, 31), notice_days_employer=90, notice_days_employee=90, qiwa_contract_ref="",
             currency="EGP", effective_date=date(2027, 3, 1), start_date=l1.hire_date)
    hr.add_statutory_parameter(user=w.h, jurisdiction="EG", code="si.wage_reference", value={"reference": "january"},
                               valid_from=date(2027, 1, 1), source_url="https://example.invalid/eg",
                               source_reference="test: January reference")
    egypt_2027(w)
    run = services.calculate_run(services.create_run(w.egy, user=w.pe, period="2027-04"), user=w.pe)
    assert slip(run, l1).gosi_wage == D("10000.00") and amount(run, l1, "si_employee") == D("1100.00")


def test_at78_egypt_approved_termination_is_never_paid_off_cycle(eg, monkeypatch):
    """hr cannot approve an Egypt termination yet (its Egypt rows, e.g. leave.sick, are not configured, OQ-26), so the
    approved termination is stood in for at payroll's own lookup."""
    w = eg
    egyptian(w)
    l3 = egyptian(w)
    egypt_2027(w)
    eg_regular(w)
    from types import SimpleNamespace
    real = services._termination
    monkeypatch.setattr(services, "_termination", lambda emp: SimpleNamespace(
        pk=999, last_working_day=date(2027, 4, 20), kind="employer_termination") if emp.pk == l3.pk else real(emp))
    with pytest.raises(PayrollError) as exc:
        off_cycle(w, "2027-04", [l3], entity=w.egy, user=w.pe, group="in_country")
    assert exc.value.code == "eg_final_settlement_not_built"


def test_c9_remuneration_contracts_carry_remuneration_only(w):
    """Compliance C9: commission or profit share on a remuneration contract would need the R65 prior-year average in
    the GOSI base; hr refuses such a contract (R52)."""
    with pytest.raises(Exception) as exc:
        remuneration(w, components={"REMUNERATION": "5000.00", "COMMISSION": "500.00"})
    assert "REMUNERATION" in str(exc.value)
