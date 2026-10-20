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
    ytd = reg.taxable_income + D("2000.00")
    expected = (_eg_tax(ytd * 12 - D("20000")) / 12).quantize(D("0.01")) - withheld
    assert types["salary_tax"] == expected and expected > 0
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
