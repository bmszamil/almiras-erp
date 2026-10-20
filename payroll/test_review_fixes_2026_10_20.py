"""Payroll review and compliance fixes of 2026-10-20 (payroll §10.9). One negative regression test per fix, with
payroll's fake invoicing port (the real port: invoicing/test_deferred_sale_review.py). Anonymised fixtures; the
payroll clock is fixed; nothing external is called.

Review: 2 (final settlement and bai' ajel sales awaiting reporting), 4 (profit recognition only inside the run
approval), 5 (cost item sources), 6 (one open correction), 7 (WHT on remuneration settlement payments), 8 (retro across
a currency change). Compliance: B2 (WHT base gross), B3 (residents), B5 (reversal rows), C1 (due date of an increase),
C2 (GOSI origin month), D1 (fine estimate), E5 (Egypt debt cap), E6 (Egypt minimum wage, in hr)."""
from datetime import date
from types import SimpleNamespace

import pytest
from django.db import IntegrityError, transaction

from invoicing.models import WhtCategory
from payroll import invoicing_port, services
from payroll.conftest import D, approved, calculated, contract, expat, hired, rate, saudi, slip
from payroll.models import (DeferredSaleContract, FinalSettlement, PayrollAlert, PayrollError, PayrollPayment,
                            SettlementCorrection)
from payroll.test_final_settlement import go_live, leaver, settle, terminate
from payroll.test_slice2_bai_ajel import bai_ajel
from payroll.test_slice2_costs import expense_bill, item, seconded
from payroll.test_slice2_settlement import approved_settlement, correct, submit_c


def bilingual(exc):
    text = " ".join(exc.messages)
    assert " / " in text and any("؀" <= ch <= "ۿ" for ch in text), text
    return text


# --- review 2: a final settlement waits for the leaver's bai' ajel invoice -------------------------------------------

def test_review2_settlement_refused_while_a_sale_awaits_reporting_then_recovers_it(w):
    n1 = leaver(w)
    w.invoicing.posted = False  # the sale invoice is issued, ZATCA has not reported it yet
    c = services.approve_deferred_sale(bai_ajel(w, n1, first="2027-03", undertaking=True), user=w.mp,
                                       sale_date=date(2027, 2, 20))
    assert c.status == "sale_invoiced"
    t = terminate(w, n1)
    go_live(w)
    fs = FinalSettlement.objects.get(termination=t)
    with pytest.raises(PayrollError) as exc:
        services.prepare_final_settlement(fs, user=w.p)
    assert exc.value.code == "sale_not_posted" and c.number in bilingual(exc.value)
    w.invoicing.posted = True  # reported and posted: the settlement refreshes the contract first
    fs = settle(w, t)
    assert DeferredSaleContract.objects.get(pk=c.pk).status == "sold"
    recovery = fs.lines.filter(line_type="deferred_sale_recovery")
    assert recovery.exists() and sum(l.amount for l in recovery) == c.receivable


def test_review2_approval_refused_when_a_sale_of_the_leaver_awaits_reporting(w):
    n1 = leaver(w)
    t = terminate(w, n1)
    go_live(w)
    fs = settle(w, t)
    w.invoicing.posted = False
    services.approve_deferred_sale(bai_ajel(w, n1, first="2027-03"), user=w.mp, sale_date=date(2027, 2, 20))
    with pytest.raises(PayrollError) as exc:
        services.approve_final_settlement(fs, user=w.c)
    assert exc.value.code == "sale_not_posted"
    assert FinalSettlement.objects.get(pk=fs.pk).status == "submitted"


# --- review 4: profit recognition only inside the approval that collected the installment ----------------------------

def test_review4_the_marker_names_the_approval_the_installment_and_the_amount(w):
    s1 = saudi(w)
    c = services.approve_deferred_sale(bai_ajel(w, s1), user=w.mp, sale_date=date(2027, 3, 20))
    approved(w, "2027-03")
    apr = services.approve_run(services.submit_run(calculated(w, "2027-04"), user=w.p), user=w.h)
    inst = c.installments.get(run=apr)
    ref = services._collection_ref(inst)
    problems, _ = invoicing_port.collection_problems(c, source_ref=ref, amount=inst.deducted_amount, user=w.h)
    assert problems  # no marker: outside the approval
    with invoicing_port.collecting(apr, w.h):
        assert invoicing_port.collection_problems(c, source_ref=ref, amount=inst.deducted_amount, user=w.h) == (
            [], "payroll.approve_run")
        assert invoicing_port.collection_problems(c, source_ref=ref, amount=D("1.00"), user=w.h)[0]  # other amount
        assert invoicing_port.collection_problems(c, source_ref=ref, amount=inst.deducted_amount, user=w.c)[0]
        assert invoicing_port.collection_problems(c, source_ref="payroll.deferredsaleinstallment:999999",
                                                  amount=inst.deducted_amount, user=w.h)[0]
    with invoicing_port.collecting(apr, w.h, mode="reverse"):  # a collect call inside a reversal marker
        assert invoicing_port.collection_problems(c, source_ref=ref, amount=inst.deducted_amount, user=w.h)[0]


# --- review 5: cost item sources -------------------------------------------------------------------------------------

def accountant(w):
    from core.access.testing import make_user, member
    if not hasattr(w, "acct"):
        w.acct = make_user("acct_ci")
        member(w.acct, w.alm, "accountant")
    return w.acct


def bill_on(w, account, amount, number):
    """A posted ALM bill with one line on ``account`` (test data)."""
    from invoicing import services as inv
    if not hasattr(w, "ap"):
        expense_bill(w, "1.00", number=f"SEED-{number}")  # sets up the vendor and the payables clerk
    doc = inv.create_purchase_document(w.alm, user=w.ap, vendor=w.vendor, vendor_document_number=number,
                                       vendor_document_date=date(2027, 3, 10), accounting_date=date(2027, 3, 10))
    inv.add_purchase_line(doc, user=w.ap, account=account, quantity="1", unit_price=amount, description_ar="بند")
    doc.refresh_from_db()
    _, net, charged, _, _, _, _ = inv._purchase_amounts(doc, list(doc.lines.select_related("vat_rate")))
    inv.update_purchase_document(doc, user=w.ap, vendor_total_vat=charged, vendor_total_gross=net + charged)
    return inv.approve_purchase_document(inv.submit_purchase_document(doc, user=w.ap), user=w.c)


def test_review5_reversed_bills_reversal_entries_and_reversed_entries_are_no_source(w):
    from invoicing import services as inv
    s1, a = seconded(w)
    bill = expense_bill(w, "500.00")
    reversed_bill = inv.reverse_document(bill, user=accountant(w), reason="duplicate")
    for kwargs in ({"bill": reversed_bill}, {"entry": reversed_bill.journal_entry},
                   {"entry": reversed_bill.reversal_entry}):
        with pytest.raises(PayrollError) as exc:
            item(w, s1, "visa", "10.00", **kwargs)
        assert exc.value.code == "cost_item_source" and bilingual(exc.value)


def test_review5_only_expense_and_prepaid_debits_back_cost_items(w):
    s1, a = seconded(w)
    asset = bill_on(w, "1520", "5000.00", "VEH-1")  # a vehicle bought (balance sheet): no employee cost
    with pytest.raises(PayrollError) as exc:
        item(w, s1, "other_benefit", "0.01", bill=asset)
    assert exc.value.code == "cost_item_exceeds_source"
    prepaid = bill_on(w, "1320", "1200.00", "PRE-1")  # a year of medical insurance paid in advance
    assert item(w, s1, "medical_insurance", "1200.00", bill=prepaid).status == "draft"


def test_review5_source_rechecked_at_approval_and_in_the_cost_report(w):
    from invoicing import services as inv
    s1, a = seconded(w)
    bill = expense_bill(w, "900.00")
    other = expense_bill(w, "300.00")
    i1 = item(w, s1, "medical_insurance", "900.00", bill=bill)
    i2 = services.approve_cost_item(item(w, s1, "visa", "300.00", bill=other), user=w.c)
    inv.reverse_document(bill, user=accountant(w), reason="wrong vendor")
    with pytest.raises(PayrollError) as exc:  # reversed after the item was drafted
        services.approve_cost_item(i1, user=w.c)
    assert exc.value.code == "cost_item_source"
    approved(w, "2027-03")
    inv.reverse_document(other, user=accountant(w), reason="wrong vendor")  # reversed after the approval
    report = services.prepare_cost_report(w.alm, "2027-03", user=w.p)
    line = report.lines.get()
    assert line.cost_items_total == D("0.00") and str(i2.pk) in " ".join(line.warnings)


# --- review 6: one open correction per settlement ---------------------------------------------------------------------

def test_review6_one_open_correction_per_settlement_also_in_the_database(w):
    n1, t, fs = approved_settlement(w)
    c = correct(w, fs, [{"line_type": "leave_encashment", "direction": "increase", "amount": "10.00"}])
    with pytest.raises(PayrollError) as exc:
        correct(w, fs, [{"line_type": "notice_pay", "direction": "increase", "amount": "10.00"}])
    assert exc.value.code == "correction_open"
    second = SettlementCorrection(entity=w.alm, settlement=fs, employee=n1, reason_ar="ثان", created_by=w.p)
    second._service_write = True
    with pytest.raises(IntegrityError), transaction.atomic():  # a race past the service check: the constraint
        second.save()
    services.discard_settlement_correction(c, user=w.p)
    correct(w, fs, [{"line_type": "notice_pay", "direction": "increase", "amount": "10.00"}])


# --- review 7, B2, B3, B5: WHT -----------------------------------------------------------------------------------------

def category(code="test-remuneration"):
    return WhtCategory.objects.create(code=code, name_en="Test category", name_ar="فئة اختبار", rate=D("5.00"),
                                      valid_from=date(2020, 1, 1), return_line_code="L-REM")


def remuneration_settlement(w):
    """Test data: an approved final settlement of a remuneration person (today H17 keeps them out of final
    settlements and hr fixes the basis; review 7 makes a payment of such a settlement carry WHT all the same)."""
    from hr import services as hr
    from hr.models import FinalSettlementInput
    r1 = remuneration(w)
    t = hr.record_termination(r1, user=w.h, kind="end_of_remuneration", last_working_day=date(2027, 3, 15),
                              reason_ar="انتهاء المهمة")
    t = hr.approve_termination(hr.submit_termination(t, user=w.h), user=w.c)
    si = FinalSettlementInput.objects.filter(termination=t).order_by("-pk").first()
    fs = FinalSettlement(entity=w.alm, employee=r1, termination=t, settlement_input=si, kind=t.kind,
                         last_working_day=t.last_working_day, period="2027-03", status="approved",
                         gross=D("6000.00"), total_deductions=D("1000.00"), net=D("5000.00"), number="ALM-FS-TEST-1",
                         created_by=w.p, approved_by=w.c)
    fs._service_write = True
    fs.save()
    return r1, fs


def test_review7_remuneration_settlement_payment_needs_wht_and_ties_out(w):
    from invoicing import services as inv
    r1, fs = remuneration_settlement(w)
    with pytest.raises(PayrollError) as exc:
        services.record_payment(w.alm, user=w.p, kind="final_settlement", final_settlement=fs,
                                payment_date=date(2027, 3, 20), amount="5000.00")
    assert exc.value.code == "wht_category_required" and bilingual(exc.value)
    p = services.record_payment(w.alm, user=w.p, kind="final_settlement", final_settlement=fs,
                                payment_date=date(2027, 3, 20), amount="5000.00", wht_category=category())
    p = services.approve_payment(services.submit_payment(p, user=w.p), user=w.c)
    assert p.wht_amount_sar == D("300.00") and p.wht_base_sar == D("6000.00")  # B2: 5% of the gross 6,000.00
    lines = {l.account.code: l.debit - l.credit for l in p.journal_entry.lines.select_related("account")}
    assert lines == {"2330": D("5000.00"), "1100": D("-4700.00"), "2220": D("-300.00")}
    data = inv.wht_return_data(w.alm, date(2027, 3, 1), user=w.a)
    rows = [r for r in data["rows"] if r.get("source") == "payroll"]
    assert [(r["vendor"], r["wht_sar"], r["base_sar"]) for r in rows] == [(r1.employee_code, D("300.00"),
                                                                           D("6000.00"))]
    assert data["ledger_2220_sar"] == D("300.00") and data["difference"] == 0


def test_review7_a_ksa_settlement_payment_needs_no_wht_category(w):
    n1, t, fs = approved_settlement(w)
    p = services.record_payment(w.alm, user=w.p, kind="final_settlement", final_settlement=fs,
                                payment_date=date(2027, 3, 20), amount=str(fs.net))
    assert p.wht_category_id is None


def remuneration(w, **fields):
    return hired(w, basis="remuneration", **fields)


def test_b2_b3_b5_wht_on_gross_for_non_residents_only_and_reversal_flagged(w):
    from core.access.testing import make_user, member
    from invoicing import services as inv
    from django.db import models

    from hr.models import Employee
    r1 = remuneration(w)  # resident in Egypt
    r2 = remuneration(w)
    # Test data: hr refuses a KSA residence for remuneration staff (R52); payroll checks it again at payment (B3).
    models.QuerySet.update(Employee.objects.filter(pk=r2.pk), country_of_residence="SA")
    inp = services.create_input(r1, user=w.p, kind="one_off_deduction", period="2027-03", amount="500.00",
                                deduction_basis="written_consent", consent_ref="SIGNED-R1", reason_ar="استقطاع")
    services.approve_input(inp, user=w.p2)
    rate("USD", "SAR", date(2027, 3, 31), "3.7500")
    rate("USD", "SAR", date(2027, 4, 8), "3.7600")
    run = approved(w, payroll_group="out_of_country")
    assert slip(run, r1).net_txn == D("4500.000") and slip(run, r1).gross_txn == D("5000.000")
    p = services.record_payment(w.alm, user=w.p, kind="net_salaries", run=run, payment_date=date(2027, 4, 8),
                                amount="9500.00", currency="USD", wht_category=category())
    p = services.approve_payment(services.submit_payment(p, user=w.p), user=w.c)
    assert p.wht_amount == D("250.000")  # 5% of r1's gross 5,000.00 (not of the 4,500.00 net); r2 none
    assert p.wht_amount_sar == D("940.00") and p.wht_base_sar == D("18800.00")
    assert [part["employee_code"] for part in p.wht_parts] == [r1.employee_code]
    assert any(r2.employee_code in text and "resident" in text for text in p.warnings)
    assert services.payslip_data(slip(run, r2), user=w.p)["wht"]["wht"] == D("0")
    c3 = make_user("c3")
    member(c3, w.alm, "approver")
    services.reverse_payment(p, user=c3, reason="bank returned the transfer", date=date(2027, 5, 3))
    may = inv.wht_return_data(w.alm, date(2027, 5, 1), user=w.a)
    [row] = [r for r in may["rows"] if r.get("source") == "payroll"]
    assert row["wht_sar"] == D("-940.00") and row["reversal"] and row["amends_return_month"] == "2027-04"
    assert "2027-04" in row["note_en"] and "2027-04" in row["note_ar"] and may["difference"] == 0


# --- review 8: no retro across a currency change -----------------------------------------------------------------------

def test_review8_remuneration_retro_across_a_currency_change_is_refused(w):
    r1 = remuneration(w)
    rate("USD", "SAR", date(2027, 3, 31), "3.7500")
    rate("EUR", "SAR", date(2027, 4, 30), "4.1000")
    approved(w, "2027-03", payroll_group="out_of_country")  # March paid in USD
    contract(w, r1, {"REMUNERATION": "5000.00"}, effective_date=date(2027, 3, 1), start_date=date(2027, 1, 1),
             currency="EUR")  # back-dated to March in EUR
    apr = calculated(w, "2027-04", payroll_group="out_of_country")
    ps = slip(apr, r1)
    assert ps.refused and "2027-03" in ps.refusal and "currency" in ps.refusal and " / " in ps.refusal


def test_review8_a_forward_currency_change_without_difference_is_not_refused(w):
    r1 = remuneration(w)
    rate("USD", "SAR", date(2027, 3, 31), "3.7500")
    rate("EUR", "SAR", date(2027, 4, 30), "4.1000")
    approved(w, "2027-03", payroll_group="out_of_country")
    contract(w, r1, {"REMUNERATION": "4600.00"}, effective_date=date(2027, 4, 1), start_date=date(2027, 4, 1),
             currency="EUR")  # from April only: March stays USD and needs nothing
    ps = slip(calculated(w, "2027-04", payroll_group="out_of_country"), r1)
    assert not ps.refused and ps.currency_id == "EUR"


# --- C1, C2: corrections ---------------------------------------------------------------------------------------------

def test_c1_c2_an_increase_is_due_on_approval_alerts_and_gosi_shows_its_origin_month(w):
    n1, t, fs = approved_settlement(w)
    c = submit_c(w, correct(w, fs, [{"line_type": "leave_encashment", "direction": "increase", "amount": "100.00"},
                                    {"line_type": "employer_gosi", "direction": "increase", "amount": "10.00",
                                     "gosi_branch": "annuities"}]))
    w.pclock.set(date(2027, 4, 5))
    c = services.approve_settlement_correction(c, user=w.c)
    assert c.due_date == date(2027, 4, 5)
    alerts = PayrollAlert.objects.filter(key=f"fsc:{c.pk}:due")
    assert {a.recipient for a in alerts} >= {w.p, w.c} and c.number in alerts.first().text_ar
    services.send_alerts(today=date(2027, 4, 6), entity=w.alm)
    assert PayrollAlert.objects.filter(key=f"fsc:{c.pk}:late", recipient=w.p).exists()
    report = services.gosi_report(w.alm, "2027-04", user=w.c)
    [row] = report["corrections"]
    assert (row["origin_period"], row["amount"], row["employee_code"]) == ("2027-03", D("10.00"), n1.employee_code)
    assert "2027-03" in row["note_ar"]
    decrease = submit_c(w, correct(w, fs, [{"line_type": "leave_encashment", "direction": "decrease",
                                            "amount": "50.00"}]))
    assert services.approve_settlement_correction(decrease, user=w.c).due_date is None  # nothing more is due


# --- D1: GOSI fine estimate --------------------------------------------------------------------------------------------

def test_d1_fine_estimate_on_contributions_paid_late_capped_and_waiver_noted(w):
    saudi(w)
    approved(w, "2027-03")
    booked = services._gosi_booked(w.alm, "2027-03")
    half = (booked / 2).quantize(D("0.01"))
    p = services.record_payment(w.alm, user=w.p, kind="gosi", gosi_period="2027-03", payment_date=date(2027, 4, 15),
                                amount=str(half))  # paid on the due day
    services.approve_payment(services.submit_payment(p, user=w.p), user=w.c)
    est, months = services._gosi_fine_estimate(w.alm, "2027-03", date(2027, 4, 20))
    assert months == 1 and est == ((booked - half) * D("0.02")).quantize(D("0.01"))  # only the part paid late
    est, months = services._gosi_fine_estimate(w.alm, "2027-03", date(2032, 4, 20))
    assert months > 50 and est == booked - half  # never more than 100% of the contributions paid late
    fine = services.record_gosi_fine(w.alm, user=w.p, period="2027-03", amount="1.00", fine_date=date(2027, 4, 30),
                                     gosi_invoice_ref="GOSI-INV-D1", paid_on=date(2027, 4, 20))
    assert "100%" in fine.note_en and "10 days" in fine.note_en and "100%" in fine.note_ar
    report = services.gosi_report(w.alm, "2027-03", user=w.c)
    assert "100%" in report["late_fine_en"] and "paid late" in report["late_fine_en"]


# --- E5: Egypt debt cap after the employer's loan instalments ----------------------------------------------------------

def test_e5_egypt_debt_cap_is_after_tax_insurance_and_loan_instalments(monkeypatch):
    rows = {"deduction.debt_max_pct": {"debt": "0.25", "nafaqa": "0.50"}, "deduction.loan_max_pct": {"pct": "0.10"}}
    monkeypatch.setattr(services, "_eg", lambda ctx, code, on=None: rows[code])
    LT = services.LT

    def slip_with(debt):
        s = services.Slip(employee=SimpleNamespace(employee_code="E-1"), basis="local_entity_employee",
                          nationality_class="", cohort="", currency=None)
        s.lines = [services.Line(LT.EARNING, D("10000.00"), None, "Basic", "أساسي"),
                   services.Line(LT.SI_EMPLOYEE, D("1100.00"), None, "SI", "تأمينات"),
                   services.Line(LT.LOAN_INSTALLMENT, D("800.00"), None, "Loan", "قرض"),
                   services.Line(LT.DEDUCTION, D(debt), None, "Debt", "دين", basis="court_order")]
        return s

    services._eg_caps(None, slip_with("2025.00"), None)  # (8,900 - 800) x 25% = 2,025.00: allowed
    s = slip_with("2100.00")  # within 25% of 8,900 (2,225.00) but not after the loan instalments
    with pytest.raises(services._Refused):
        services._eg_caps(None, s, None)
    assert "loan" in s.refusal and " / " in s.refusal


# --- E6: Egypt minimum wage (hr) ---------------------------------------------------------------------------------------

def test_e6_egypt_contract_below_the_minimum_wage_is_a_warning(w):
    from hr import services as hr
    from hr.conftest import contract as hr_contract, employee

    emp = employee(w, w.egy, by=w.he, basis="local_entity_employee")
    dept = hr.create_department(w.egy, user=w.he, code="OPS-E6", name_en="Operations", name_ar="العمليات")
    grade = hr.create_grade(w.egy, user=w.he, code="G-E6", name_en="Staff", name_ar="موظف", overtime_eligible=False)
    fields = dict(department=dept, grade=grade, contract_type="fixed", end_date=date(2028, 12, 31),
                  notice_days_employer=90, notice_days_employee=90, qiwa_contract_ref="")
    c = hr_contract(w, emp, {"BASIC": "6500.00"}, drafter=w.he, approver=w.ce, **fields)
    assert c.status == "approved" and any("minimum wage" in text and "7000.00" in text for text in c.warnings)
    emp2 = employee(w, w.egy, by=w.he, basis="local_entity_employee")
    c2 = hr_contract(w, emp2, {"BASIC": "9000.00"}, drafter=w.he, approver=w.ce, **fields)
    assert not any("minimum wage" in text for text in c2.warnings)
