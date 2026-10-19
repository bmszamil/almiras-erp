"""GOSI (R20 to R23, R55 to R58, R60, R64 to R68; AT-14, AT-15, AT-42 to AT-50, AT-56, AT-67 to AT-72)."""
from datetime import date

import pytest
from django.test import override_settings

from hr import services as hr
from hr.models import StatutoryParameter
from payroll import services
from payroll.conftest import D, amount, approved, calculated, expat, saudi, slip
from payroll.models import GosiCatchUpNotice, PayrollError


def gosi(run, emp, line_type, branch=None, **kw):
    f = {"gosi_branch": branch} if branch else {}
    return amount(run, emp, line_type, **f, **kw)


def test_at14_legacy_rates_cap_and_non_saudi(w):
    s1 = saudi(w)
    s3 = saudi(w, components={"BASIC": "50000.00", "HOUSING": "12500.00"})
    n1 = expat(w)
    run = calculated(w)
    assert gosi(run, s1, "employee_gosi", origin_period="") == D("1218.75")  # 1,125.00 + 93.75
    assert gosi(run, s1, "employer_gosi", origin_period="") == D("1468.75")  # + 250.00 OH
    assert slip(run, s3).gosi_wage == D("45000.00")  # cap (SIL Art. 8(2))
    assert gosi(run, n1, "employer_gosi") == D("150.00") and gosi(run, n1, "employee_gosi") == 0


def test_at42_new_law_steps_by_calendar_month(w):
    s5 = saudi(w, cohort="new_m273", components={"BASIC": "10000.00", "HOUSING": "2500.00"},
               gosi_first_coverage_date=date(2025, 2, 1))
    run = calculated(w)
    assert gosi(run, s5, "employee_gosi") == D("1343.75") and gosi(run, s5, "employer_gosi") == D("1593.75")


def test_at42_july_step_month_reads_the_unverified_parameter(w):
    s5 = saudi(w, cohort="new_m273", components={"BASIC": "10000.00", "HOUSING": "2500.00"},
               gosi_first_coverage_date=date(2025, 2, 1), hire_date=date(2027, 6, 1))
    run = calculated(w, "2027-07")
    assert any("gosi.annuities_step_start" in x for x in slip(run, s5).warnings)
    assert gosi(run, s5, "employee_gosi", branch="annuities") == D("1250.00")  # still 20% (August reading)
    services.submit_run(run, user=w.p)
    with override_settings(HR_REQUIRE_VERIFIED_PARAMETERS=True):
        with pytest.raises(Exception) as exc:
            services.approve_run(run, user=w.h)
    assert "unverified" in str(exc.value)


def test_at43_cohort_from_the_record_missing_cohort_and_gcc_refused(w):
    s6 = saudi(w, cohort="legacy_m33", hire_date=date(2026, 5, 1))
    s7 = saudi(w, cohort="")
    g1 = expat(w, nationality="KW", id_type="gcc_id")
    n1 = expat(w)
    run = calculated(w)
    assert gosi(run, s6, "employee_gosi") == D("1218.75")  # legacy rows, not derived from the hire date
    assert "cohort" in slip(run, s7).refusal and "GCC" in slip(run, g1).refusal
    assert not slip(run, n1).refused


def test_at44_new_law_age_limits(w):
    a1 = saudi(w, cohort="new_m273", date_of_birth=date(1955, 1, 1), gosi_first_coverage_date=date(2025, 1, 1))
    a2 = saudi(w, cohort="new_m273", date_of_birth=date(1961, 1, 1), gosi_first_coverage_date=date(2025, 3, 1))
    a3 = saudi(w, cohort="new_m273", date_of_birth=date(1962, 3, 10), gosi_first_coverage_date=date(2002, 1, 1))
    feb = calculated(w, "2027-02")
    assert gosi(feb, a1, "employee_gosi") == 0 and gosi(feb, a1, "employer_gosi", branch="occupational_hazards") > 0
    assert gosi(feb, a2, "employee_gosi", branch="annuities") > 0 and gosi(feb, a2, "employee_gosi", branch="saned") == 0
    assert gosi(feb, a3, "employee_gosi", branch="saned") > 0
    mar = calculated(w, "2027-03")
    assert gosi(mar, a3, "employee_gosi", branch="saned") > 0 and any("SANED stops" in x for x in slip(mar, a3).warnings)
    apr = calculated(w, "2027-04")
    assert gosi(apr, a3, "employee_gosi", branch="saned") == 0 and gosi(apr, a3, "employee_gosi", branch="annuities") > 0


def test_at48_legacy_age_rules(w):
    k1 = saudi(w, date_of_birth=date(1965, 1, 1), gosi_first_coverage_date=date(2027, 1, 1))  # 62, returning
    k2 = saudi(w, date_of_birth=date(1967, 3, 5), gosi_first_coverage_date=date(2010, 1, 1))  # 60 in March 2027
    k3 = saudi(w, date_of_birth=date(1950, 1, 1), gosi_first_coverage_date=date(2010, 1, 1))  # 60 when first covered
    feb = calculated(w, "2027-02")
    assert gosi(feb, k1, "employee_gosi", branch="annuities") > 0  # no entry-age test for legacy (Art. 4(2)(b))
    assert gosi(feb, k2, "employee_gosi", branch="saned") > 0 and gosi(feb, k3, "employee_gosi", branch="saned") == 0
    apr = calculated(w, "2027-04")
    assert gosi(apr, k2, "employee_gosi", branch="saned") == 0


def test_at45_oh_override_bounds_reference_and_dates(w):
    with pytest.raises(PayrollError):
        services.configure_settings(w.alm, user=w.c, gosi_oh_rate_override="5.00", gosi_oh_override_ref="X")
    with pytest.raises(PayrollError):
        services.configure_settings(w.alm, user=w.c, gosi_oh_rate_override="4.00")
    services.configure_settings(w.alm, user=w.c, gosi_oh_rate_override="4.00", gosi_oh_override_ref="GOSI-DEC-1",
                                gosi_oh_override_from=date(2027, 3, 1), gosi_oh_override_to=date(2027, 3, 31))
    from core.access.models import AccessLog
    assert AccessLog.objects.filter(details__action="gosi_oh_override").exists()
    n1 = expat(w)
    assert gosi(calculated(w), n1, "employer_gosi") == D("300.00")
    assert gosi(calculated(w, "2027-04"), n1, "employer_gosi") == D("150.00")  # outside the decision's dates


def test_at47_floors_in_kind_housing_and_commission_average(w):
    s8 = saudi(w, components={"BASIC": "12000.00"}, contract_kwargs={"housing_in_kind": True})
    s9 = saudi(w, components={"BASIC": "1200.00"})
    n3 = expat(w, components={"BASIC": "350.00"})
    sc = saudi(w, components={"BASIC": "10000.00", "HOUSING": "2500.00", "COMMISSION": "2400.00"})
    run = calculated(w)
    assert slip(run, s8).gosi_wage == D("14000.00")  # 12,000 + 12,000 x 2 / 12 (Regs Art. 17(c))
    assert gosi(run, s8, "employee_gosi") == D("1365.00")
    assert slip(run, s9).gosi_wage == D("1500.00") and gosi(run, s9, "employee_gosi") == D("146.25")
    assert slip(run, n3).gosi_wage == D("400.00") and gosi(run, n3, "employer_gosi") == D("8.00")
    assert "Art. 18(2)" in slip(run, sc).refusal  # AT-71: no prior-year average entered
    services.record_variable_wage_base(sc, user=w.p, year=2027, amount="1000.00", method="prior_year_average",
                                       source_note="2026 commission paid / 12")
    run = services.calculate_run(run, user=w.p)
    assert slip(run, sc).gosi_wage == D("13500.00")  # not 14,900.00 (March actual)


def test_at15_reconciliation_reports_the_difference(w):
    saudi(w)
    expat(w)
    approved(w)
    rec = services.record_gosi_reconciliation(w.alm, "2027-03", user=w.p, gosi_invoice_amount="2900.00")
    assert rec.erp_amount == D("2837.50") and rec.difference == D("62.50")


def test_at56_catch_up_new_law_deducted_with_notice_legacy_borne(w):
    s1 = saudi(w)
    s5 = saudi(w, cohort="new_m273", components={"BASIC": "10000.00", "HOUSING": "2500.00"},
               gosi_first_coverage_date=date(2025, 2, 1))
    # March is approved without SANED (a zero row dated 2027-03-01: test fixture), then that row is superseded.
    original = StatutoryParameter.objects.get(code="gosi.rates.saned")
    zero = StatutoryParameter.objects.create(
        jurisdiction="SA", code="gosi.rates.saned", valid_from=date(2027, 3, 1), source_url="https://example.test",
        source_reference="test: missing SANED row",
        value={k: {"employer": "0", "employee": "0"} for k in original.value})
    run = approved(w)
    assert gosi(run, s1, "employee_gosi", branch="saned") == 0
    zero.refresh_from_db()
    zero.status = "superseded"
    zero.save()
    apr = calculated(w, "2027-04")
    catch = slip(apr, s5).lines.filter(line_type="gosi_catch_up", origin_period="2027-03")
    assert sum(l.amount for l in catch) == D("93.75")
    borne = slip(apr, s1).lines.filter(line_type="employer_gosi", origin_period="2027-03", source_ref="borne:saned")
    assert sum(l.amount for l in borne) == D("93.75")
    assert any("19(4)" in x for x in slip(apr, s1).warnings)
    apr = services.approve_run(services.submit_run(apr, user=w.p), user=w.h)
    notice = GosiCatchUpNotice.objects.get(employee=s5)
    assert notice.amount == D("93.75") and notice.text_ar
    services.record_catch_up_notification(notice, user=w.p, notified_on=date(2027, 4, 28), channel="letter")
    may = calculated(w, "2027-05")  # nothing is caught up twice
    assert not slip(may, s5).lines.filter(line_type="gosi_catch_up").exists()


def test_at67_joining_month_gosi_on_days_of_service(w):
    n1 = expat(w, hire_date=date(2027, 3, 11))
    run = calculated(w)
    assert slip(run, n1).gosi_wage == D("5080.65") and gosi(run, n1, "employer_gosi") == D("101.61")
    row = StatutoryParameter.objects.get(code="gosi.partial_month_divisor")
    row.value = {"divisor": "thirty"}
    row.save()
    run = services.calculate_run(run, user=w.p)
    assert slip(run, n1).gosi_wage == D("5000.00") and gosi(run, n1, "employer_gosi") == D("100.00")


def test_at68_unpaid_and_sick_days_do_not_reduce_the_gosi_wage(w):
    s1 = saudi(w)
    hr.approve_leave(hr.request_leave(s1, "UNPAID", user=w.h, start_date=date(2027, 3, 15),
                                      end_date=date(2027, 3, 16)), user=w.h2)
    run = calculated(w)
    assert gosi(run, s1, "employee_gosi") == D("1218.75") and slip(run, s1).gosi_wage == D("12500.00")


def test_at69_cap_across_employers_pro_rata(w):
    a = saudi(w, components={"BASIC": "30000.00"}, id_number="1999999901")
    saudi(w, w.sub, components={"BASIC": "30000.00"}, id_number="1999999901")
    run = calculated(w)
    assert slip(run, a).gosi_wage == D("22500.00")
    b = saudi(w, components={"BASIC": "40000.00"}, gosi_other_employers_wage="15000.00")
    run = services.calculate_run(run, user=w.p)
    assert slip(run, b).gosi_wage == D("32727.27")  # 45,000 x 40/55


def test_at70_oh_suspended_abroad_for_personal_reasons_only(w):
    n1 = expat(w)
    services.record_abroad_period(n1, user=w.p, exit_date=date(2027, 3, 20), reentry_date=date(2027, 5, 10),
                                  purpose="personal")
    assert gosi(calculated(w, "2027-03"), n1, "employer_gosi") == D("150.00")
    assert gosi(calculated(w, "2027-04"), n1, "employer_gosi") == 0
    assert gosi(calculated(w, "2027-05"), n1, "employer_gosi") == D("150.00")
    n2 = expat(w)
    services.record_abroad_period(n2, user=w.p, exit_date=date(2027, 3, 20), reentry_date=date(2027, 5, 10),
                                  purpose="work")
    assert gosi(calculated(w, "2027-06"), n2, "employer_gosi") == D("150.00")


def test_at72_secondment_to_a_ksa_group_entity_warns(w):
    s1 = saudi(w)
    a = hr.create_assignment(s1, user=w.h, kind="secondment", receiving_entity=w.sub, role_title_en="Analyst",
                             role_title_ar="محلل", start_date=date(2027, 3, 1), end_date=date(2027, 3, 10))
    hr.approve_assignment(hr.submit_assignment(a, user=w.h), user=w.c)
    run = calculated(w)
    assert any("20(3)(a)" in x for x in slip(run, s1).warnings)


def test_r57_wage_change_warns_to_update_gosi_by_month_end(w, clock):
    s1 = saudi(w)
    approved(w)
    from payroll.conftest import contract
    clock.set(date(2027, 3, 25))
    contract(w, s1, {"BASIC": "10000.00", "HOUSING": "3000.00", "TRANSPORT": "1000.00"},
             effective_date=date(2027, 4, 1), start_date=date(2027, 1, 1), qiwa_contract_ref="QIWA-AMEND-9")
    run = calculated(w, "2027-04")
    assert any("Regs Art. 6(2)(c)" in x for x in slip(run, s1).warnings)


def test_r10_unregistered_warning(w):
    s1 = saudi(w, gosi_number="")
    run = calculated(w)
    assert any("31(2)" in x for x in slip(run, s1).warnings) and not slip(run, s1).refused
