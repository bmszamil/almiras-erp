"""Payments, remuneration staff and payroll groups (R29, R41 to R44, R62; E2, E3, E8a, E8b; AT-18, AT-25, AT-26,
AT-55)."""
from datetime import date

import pytest
from django.core.exceptions import PermissionDenied

from core.access.testing import make_user, member
from invoicing.models import WhtCategory
from payroll import services
from payroll.conftest import D, amount, approved, calculated, employee, entry_lines, expat, hired, rate, saudi, slip
from payroll.models import PayrollError, PayrollRun


def pay(w, run, amount_, user=None, **kw):
    p = services.record_payment(w.alm, user=user or w.p, kind="net_salaries", run=run, payment_date=date(2027, 3, 31),
                                amount=amount_, **kw)
    return services.submit_payment(p, user=user or w.p)


def test_at18_salary_payment_posts_e3_and_marks_the_run_paid(w):
    saudi(w)
    expat(w)
    run = approved(w)
    net = sum(p.net for p in run.payslips.all())
    assert net == D("20281.25")  # E1 without the loan installment
    with pytest.raises(PayrollError) as exc:
        pay(w, run, "20281.26")
    assert exc.value.code == "overpayment"
    p = pay(w, run, str(net))
    with pytest.raises(PermissionDenied):
        services.approve_payment(p, user=w.p)  # its recorder (A)
    p = services.approve_payment(p, user=w.c)
    lines = {l.account.code: (l.debit, l.credit) for l in p.journal_entry.lines.select_related("account")}
    assert lines == {"2330": (net, D("0")), "1100": (D("0"), net)}
    assert PayrollRun.objects.get(pk=run.pk).status == "paid"


def test_e2_gosi_payment_within_the_period_payable(w):
    saudi(w)
    expat(w)
    approved(w)
    with pytest.raises(PayrollError):
        services.record_payment(w.alm, user=w.p, kind="gosi", gosi_period="2027-03", payment_date=date(2027, 4, 10),
                                amount="2837.51")
    p = services.record_payment(w.alm, user=w.p, kind="gosi", gosi_period="2027-03", payment_date=date(2027, 4, 10),
                                amount="2837.50")
    p = services.approve_payment(services.submit_payment(p, user=w.p), user=w.c)
    assert {l.account.code for l in p.journal_entry.lines.select_related("account")} == {"2340", "1100"}
    assert services.gosi_report(w.alm, "2027-03", user=w.a)["open"] == 0


def remuneration(w, **fields):
    return hired(w, basis="remuneration", **fields)


def test_at25_payroll_groups_and_remuneration_without_gosi_eosb_overtime(w):
    s1 = saudi(w)
    r1 = remuneration(w)
    rate("USD", "SAR", date(2027, 3, 31), "3.7500")
    inc = calculated(w)
    assert {p.employee_id for p in inc.payslips.all()} == {s1.pk}
    out = calculated(w, payroll_group="out_of_country")
    assert {p.employee_id for p in out.payslips.all()} == {r1.pk}
    ps = slip(out, r1)
    assert not ps.lines.filter(line_type__in=("employee_gosi", "employer_gosi", "eosb_accrual")).exists()
    with pytest.raises(PayrollError):
        services.create_input(r1, user=w.p, kind="overtime_hours", period="2027-03", quantity="2",
                              overtime_date=date(2027, 3, 3), reason_ar="عمل")
    with pytest.raises(PayrollError):
        services.create_input(r1, user=w.p, kind="unpaid_days", period="2027-03", quantity="2", reason_ar="غياب")


def test_at26_e8_remuneration_in_usd_with_wht_at_payment(w):
    r1 = remuneration(w)
    rate("USD", "SAR", date(2027, 3, 31), "3.7500")
    rate("USD", "SAR", date(2027, 4, 8), "3.7600")
    run = approved(w, payroll_group="out_of_country")
    ps = slip(run, r1)
    assert ps.currency_id == "USD" and ps.gross_txn == D("5000.000") and ps.gross == D("18750.00")
    e = entry_lines(run)
    assert e == {"5100": D("18750.00"), "2330": D("-18750.00")}
    cat = WhtCategory.objects.create(code="test-remuneration", name_en="Test category", name_ar="فئة اختبار",
                                     rate=D("5.00"), valid_from=date(2020, 1, 1))
    with pytest.raises(PayrollError):  # 3 decimals in USD (CLAUDE.md rule 7)
        services.record_payment(w.alm, user=w.p, kind="net_salaries", run=run, payment_date=date(2027, 4, 8),
                                amount="5000.005", currency="USD", wht_category=cat)
    with pytest.raises(PayrollError) as exc:
        services.record_payment(w.alm, user=w.p, kind="net_salaries", run=run, payment_date=date(2027, 4, 8),
                                amount="5000.00", currency="USD")
    assert exc.value.code == "wht_category_required"
    p = services.record_payment(w.alm, user=w.p, kind="net_salaries", run=run, payment_date=date(2027, 4, 8),
                                amount="5000.00", currency="USD", wht_category=cat)
    p = services.approve_payment(services.submit_payment(p, user=w.p), user=w.c)
    lines = {}
    for l in p.journal_entry.lines.select_related("account"):
        lines[l.account.code] = l.debit - l.credit
    assert lines == {"2330": D("18750.00"), "1100": D("-17860.00"), "2220": D("-940.00"), "5750": D("50.00")}
    assert p.wht_amount == D("250.000") and p.wht_amount_sar == D("940.00")
    data = services.payslip_data(ps, user=w.p)
    assert data["wht"] == {"rate": D("5.00"), "wht": D("250.00"), "net_of_wht": D("4750.000")}
    rows = services.wht_return_rows(w.alm, date(2027, 4, 1), user=w.a)
    assert rows[0]["wht_sar"] == D("940.00")


def test_at26_ksa_contract_in_usd_is_refused_upstream(w):
    with pytest.raises(Exception):
        saudi(w, contract_kwargs={"currency": "USD"})


def test_at55_saudi_remuneration_staff_gosi_by_relationship(w):
    rate("USD", "SAR", date(2027, 3, 31), "3.7500")
    saudi_fields = dict(nationality="SA", id_type="national_id", country_of_residence="AE", gosi_cohort="new_m273",
                        gosi_first_coverage_date=date(2025, 1, 1))
    r2 = remuneration(w, id_number="1888888801", gosi_employment_relationship="subordinate",
                      gosi_relationship_ref="AGREEMENT-1", **saudi_fields)
    b1 = remuneration(w, id_number="1888888802", gosi_employment_relationship="non_subordinate",
                      gosi_relationship_ref="BOARD-RES-1", **saudi_fields)
    # hr R69 (slice 2): the contract of a Saudi remuneration person is refused without the relationship, so R3 had it
    # when the contract was approved and it was cleared afterwards; payroll still refuses the payslip (R56(c)).
    r3 = remuneration(w, id_number="1888888803", gosi_employment_relationship="subordinate",
                      gosi_relationship_ref="AGREEMENT-3", **saudi_fields)
    from hr import services as hr_services
    hr_services.update_employee(r3, user=w.h, gosi_employment_relationship="", gosi_relationship_ref="")
    r1 = remuneration(w)
    run = calculated(w, payroll_group="out_of_country")
    assert slip(run, r2).gosi_wage == D("18750.00")
    assert amount(run, r2, "employee_gosi") == D("1875.00") and amount(run, r2, "employer_gosi") == D("1875.00")
    assert not slip(run, r2).lines.filter(gosi_branch__in=("saned", "occupational_hazards")).exists()
    assert slip(run, r2).net_txn == D("4500.000")
    assert not slip(run, b1).lines.filter(line_type="employee_gosi").exists() and slip(run, b1).notes
    assert slip(run, r3).refused and "relationship" in slip(run, r3).refusal
    assert not slip(run, r1).lines.filter(line_type="employee_gosi").exists()
    with pytest.raises(PayrollError):
        services.submit_run(run, user=w.p)
    from hr import services as hr
    hr.update_employee(r3, user=w.h, gosi_employment_relationship="non_subordinate", gosi_relationship_ref="BOARD-2")
    run = services.approve_run(services.submit_run(services.calculate_run(run, user=w.p), user=w.p), user=w.h)
    e = {}
    for l in run.journal_entry.lines.select_related("account"):
        e[l.account.code] = e.get(l.account.code, D("0")) + l.debit - l.credit
    assert e["2340"] == D("-3750.00") and e["5110"] == D("1875.00")


def test_oq30_r43_remuneration_wht_is_in_the_invoicing_wht_return_and_its_2220_tie_out(w):
    """Slice 2 (OQ-30): payroll's remuneration WHT rows go into ``invoicing.wht_return_data`` (payee by employee code
    only) and the 2220 tie-out counts the payroll payment entries; a reversed payment shows as a negative row in the
    month of the reversal."""
    from invoicing import services as inv
    r1 = remuneration(w)
    r2 = remuneration(w)
    rate("USD", "SAR", date(2027, 3, 31), "3.7500")
    rate("USD", "SAR", date(2027, 4, 8), "3.7600")
    run = approved(w, payroll_group="out_of_country")
    cat = WhtCategory.objects.create(code="test-remuneration", name_en="Test category", name_ar="فئة اختبار",
                                     rate=D("5.00"), valid_from=date(2020, 1, 1), return_line_code="L-REM")
    p = services.record_payment(w.alm, user=w.p, kind="net_salaries", run=run, payment_date=date(2027, 4, 8),
                                amount="10000.00", currency="USD", wht_category=cat)
    p = services.approve_payment(services.submit_payment(p, user=w.p), user=w.c)
    assert p.wht_base_sar == D("37600.00") and p.wht_amount_sar == D("1880.00")
    data = inv.wht_return_data(w.alm, date(2027, 4, 1), user=w.a)
    rows = [r for r in data["rows"] if r.get("source") == "payroll"]
    assert sorted(r["vendor"] for r in rows) == sorted([r1.employee_code, r2.employee_code])
    assert sum(r["wht_sar"] for r in rows) == D("1880.00") and sum(r["base_sar"] for r in rows) == D("37600.00")
    assert all(r["category"] == "test-remuneration" and r["return_line_code"] == "L-REM" and r["country"] == "EG"
               and r["payee_kind"] == "employee" for r in rows)
    assert data["ledger_2220_sar"] == D("1880.00") and data["difference"] == 0
    assert r1.full_name_en not in str(data)  # codes only
    c3 = make_user("c3")
    member(c3, w.alm, "approver")
    services.reverse_payment(p, user=c3, reason="bank returned the transfer", date=date(2027, 5, 3))
    may = inv.wht_return_data(w.alm, date(2027, 5, 1), user=w.a)
    assert sum(r["wht_sar"] for r in may["rows"] if r.get("source") == "payroll") == D("-1880.00")
    assert may["difference"] == 0 and may["ledger_2220_sar"] == D("-1880.00")
    assert inv.wht_return_data(w.alm, date(2027, 4, 1), user=w.a)["difference"] == 0  # April unchanged
