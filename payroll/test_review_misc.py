"""Review fixes (2026-10-19): bai' ajel purchase and possession first (review 11), GOSI report detail only with
payroll.view_payslip (review 13, D1), monthly caps of court and damage deductions (C-A4), Art. 106(1) inventory days
(C-A7), the Arabic-first payslip (C-A13) and the multi-employer note (C-B4)."""
from datetime import date

import pytest

from core.access.models import AccessLog
from payroll import services
from payroll.conftest import D, approved, asset_bill, calculated, expat, saudi, slip
from payroll.models import PayrollError


def terms(w, emp, cost="30000.00"):
    c = services.create_deferred_sale(emp, user=w.p, asset_description_ar="سيارة", cost=cost, profit_amount="3600.00",
                                      term_months=24, first_period="2027-03", consent_ref="SIGNED-BAJ-1")
    return services.approve_deferred_sale_terms(services.submit_deferred_sale_terms(c, user=w.p), user=w.mp)


def test_review11_bai_ajel_sale_refused_before_purchase_and_possession(w):
    from invoicing import services as inv
    s1 = saudi(w)
    c = terms(w, s1)
    with pytest.raises(PayrollError) as exc:
        services.approve_deferred_sale(c, user=w.mp)
    assert exc.value.code == "asset_not_acquired"
    bill = asset_bill(w, "30000.00")
    wrong = asset_bill(w, "29000.00")
    draft = inv.create_purchase_document(w.alm, user=w.ap, vendor=w.vendor, vendor_document_number="CAR-DRAFT",
                                         vendor_document_date=date(2027, 2, 15), accounting_date=date(2027, 2, 15))
    for purchase, when, evidence in ((draft, date(2027, 2, 16), "DELIVERY-1"), (wrong, date(2027, 2, 16), "DELIVERY-1"),
                                     (bill, date(2027, 2, 16), ""), (bill, date(2027, 2, 14), "DELIVERY-1")):
        with pytest.raises(PayrollError) as exc:
            services.record_asset_acquisition(c, user=w.p, purchase_bill=purchase, possession_date=when,
                                              possession_evidence_ref=evidence)
        assert exc.value.code == "acquisition_invalid"
    c = services.record_asset_acquisition(c, user=w.p, purchase_bill=bill, possession_date=date(2027, 3, 25),
                                          possession_evidence_ref="DELIVERY-1")
    assert c.status == "asset_acquired"
    with pytest.raises(PayrollError) as exc:  # payroll clock 2027-03-20: before possession
        services.approve_deferred_sale(c, user=w.mp)
    assert exc.value.code == "sale_before_possession"
    c = services.approve_deferred_sale(c, user=w.mp, sale_date=date(2027, 3, 25))
    assert c.status == "sold" and c.sale_date == date(2027, 3, 25) and c.purchase_bill_id == bill.pk


def test_review13_gosi_report_rows_only_with_view_payslip_and_logged(w):
    s1 = saudi(w)
    expat(w)
    approved(w)
    agg = services.gosi_report(w.alm, "2027-03", user=w.a)  # accountant: cost reports, no payslips
    assert agg["rows"] == [] and agg["total"] == D("2837.50")
    assert {b["branch"] for b in agg["branches"]} == {"annuities", "saned", "occupational_hazards"}
    assert not AccessLog.objects.filter(details__action="gosi_report").exists()
    full = services.gosi_report(w.alm, "2027-03", user=w.c)  # approver: also payroll.view_payslip
    assert s1.employee_code in {r["employee_code"] for r in full["rows"]} and full["total"] == agg["total"]
    assert AccessLog.objects.filter(details__action="gosi_report", actor=w.c).exists()


def _deduction(w, emp, basis, amount_, ref=""):
    return services.create_input(emp, user=w.p, kind="one_off_deduction", period="2027-03", amount=amount_,
                                 reason_ar="استقطاع", deduction_basis=basis, reference=ref)


@pytest.mark.parametrize("basis,first,second", [("court_order", "1500.00", "600.00"),  # quarter of 8,000.00 = 2,000.00
                                                ("damage_art91", "1000.00", "400.00")])  # 5 days = 1,333.33
def test_c_a4_court_and_damage_caps_are_per_month_for_all_inputs_together(w, basis, first, second):
    n1 = expat(w)
    services.approve_input(_deduction(w, n1, basis, first, "A"), user=w.p2)
    with pytest.raises(PayrollError) as exc:
        _deduction(w, n1, basis, second, "B")
    assert "C-A4" in exc.value.messages[0] and " / " in exc.value.messages[0]


def test_c_a4_monthly_cap_counts_what_the_periods_runs_already_deducted(w):
    n1 = expat(w)
    services.approve_input(_deduction(w, n1, "damage_art91", "1000.00", "A"), user=w.p2)
    approved(w)
    late = services.create_input(n1, user=w.p, kind="one_off_deduction", period="2027-02", amount="400.00",
                                 reason_ar="استقطاع", deduction_basis="damage_art91", reference="B")
    services.approve_input(late, user=w.p2)  # entered for February, paid in a March off-cycle run
    off = services.create_run(w.alm, user=w.p, period="2027-03", kind="off_cycle", reason_ar="تصحيح", employees=[n1])
    off = services.calculate_run(off, user=w.p)
    assert slip(off, n1).refused and "C-A4" in slip(off, n1).refusal


def test_c_a7_art106_inventory_and_closing_overtime_at_most_30_days_a_year(w):
    from hr.models import StatutoryParameter
    s1 = saudi(w)
    row = StatutoryParameter.objects.get(code="overtime.art106_inventory_max_days")
    assert row.value == {"days": "30"} and row.status == "unverified"
    for day in range(1, 31):
        services.create_input(s1, user=w.p, kind="overtime_hours", period="2027-03", quantity="1",
                              overtime_date=date(2027, 3, day), art106_exceptional=True,
                              art106_case="inventory_closing", reason_ar="جرد", reference=f"INV-{day}")
    with pytest.raises(PayrollError) as exc:  # day 31 of inventory work in the year
        services.create_input(s1, user=w.p, kind="overtime_hours", period="2027-03", quantity="1",
                              overtime_date=date(2027, 3, 31), art106_exceptional=True, art106_case="inventory_closing",
                              reason_ar="جرد", reference="INV-31")
    assert "106(1)" in exc.value.messages[0] and " / " in exc.value.messages[0]
    # Another Art. 106 case is not limited by the 106(1) days.
    services.create_input(s1, user=w.p, kind="overtime_hours", period="2027-03", quantity="1",
                          overtime_date=date(2027, 3, 31), art106_exceptional=True, art106_case="other",
                          reason_ar="طارئ", reference="OTH-1")


def test_c_a13_payslip_is_arabic_first_with_paid_days_pay_date_and_gosi_wage(w):
    s1 = saudi(w)
    run = approved(w)
    html = services.render_payslip(slip(run, s1), user=w.p)
    assert '<html lang="ar" dir="rtl">' in html and "النص العربي هو المعتمد" in html and "The Arabic text prevails" in html
    assert "2027-03-27" in html and "12500.00" in html and "الأيام المدفوعة" in html


def test_c_b4_multi_employer_note_never_shows_the_other_employers_amounts(w):
    s1 = saudi(w, components={"BASIC": "40000.00", "HOUSING": "10000.00"}, gosi_other_employers_wage="20000.00")
    ps = slip(calculated(w), s1)
    note = next(n for n in ps.notes if "pro rata" in n)
    assert "20000" not in note and "Regs Art. 19" in note
