"""Payroll slice 2 (§10.8 item 3; R35 as amended, R63; billing R71, AT-59): employee cost items with their source
documents and the approved, fully loaded secondment cost report that billing reads through payroll.billing_port."""
from datetime import date

import pytest
from django.core.exceptions import PermissionDenied

from core.access.testing import make_user, member
from hr import services as hr
from payroll import services
from payroll.billing_port import PayrollCostPort
from payroll.conftest import D, approved, saudi, slip
from payroll.models import PayrollError, SecondmentCostReport


def expense_bill(w, amount, on=date(2027, 3, 10), number=None):
    """A posted vendor bill of ALM for an employee cost (e.g. medical insurance), test data."""
    from invoicing import services as inv
    if not hasattr(w, "ap"):
        w.ap = make_user("ap")
        member(w.ap, w.alm, "payables_clerk")
        w.vendor = inv.create_vendor(w.alm, user=w.ap, vendor_kind="business", code="V-INS", name_en="Test Insurer",
                                     name_ar="شركة تأمين تجريبية", is_vat_registered=True, vat_number="320000000000003")
        w.bills = 0
    w.bills += 1
    doc = inv.create_purchase_document(w.alm, user=w.ap, vendor=w.vendor, vendor_document_number=number or f"INS-{w.bills}",
                                       vendor_document_date=on, accounting_date=on)
    inv.add_purchase_line(doc, user=w.ap, account="5900", quantity="1", unit_price=amount, description_ar="تكلفة موظف")
    doc.refresh_from_db()
    _, net, charged, _, _, _, _ = inv._purchase_amounts(doc, list(doc.lines.select_related("vat_rate")))
    inv.update_purchase_document(doc, user=w.ap, vendor_total_vat=charged, vendor_total_gross=net + charged)
    return inv.approve_purchase_document(inv.submit_purchase_document(doc, user=w.ap), user=w.c)


def seconded(w, start=date(2027, 3, 1), end=date(2027, 3, 31)):
    s1 = saudi(w)
    a = hr.create_assignment(s1, user=w.h, kind="secondment", receiving_entity=w.sub, role_title_en="Analyst",
                             role_title_ar="محلل", start_date=start, end_date=end)
    hr.approve_assignment(hr.submit_assignment(a, user=w.h), user=w.c)
    return s1, a


def item(w, emp, kind, amount, bill=None, entry=None, **kw):
    return services.create_cost_item(emp, user=w.p, kind=kind, amount=amount, description_ar="تكلفة", source_bill=bill,
                                     source_entry=entry, **kw)


def test_r63_cost_items_need_a_posted_source_of_the_entity_and_never_exceed_it(w):
    s1, a = seconded(w)
    bill = expense_bill(w, "2000.00")
    with pytest.raises(PayrollError) as exc:
        item(w, s1, "medical_insurance", "1200.00")  # AT-59: an item without a source document
    assert exc.value.code == "cost_item_source"
    with pytest.raises(PayrollError) as exc:
        item(w, s1, "medical_insurance", "1200.00", bill=bill, month="2027-04")  # the source's month governs
    assert exc.value.code == "cost_item_month"
    i1 = item(w, s1, "medical_insurance", "1200.00", bill=bill)
    assert i1.month == "2027-03" and i1.status == "draft"
    i2 = item(w, s1, "iqama_renewal", "800.00", bill=bill)
    with pytest.raises(PayrollError) as exc:
        item(w, s1, "visa", "0.01", bill=bill)  # the bill booked 2,000.00 of cost (net of input VAT)
    assert exc.value.code == "cost_item_exceeds_source"
    with pytest.raises(PayrollError) as exc:
        item(w, s1, "visa", 10.5, bill=bill)
    assert exc.value.code == "float_rejected"
    with pytest.raises(PermissionDenied):
        services.approve_cost_item(i1, user=w.p)  # never its creator (A)
    assert services.approve_cost_item(i1, user=w.c).status == "approved"
    assert services.reject_cost_item(i2, user=w.c).status == "rejected"
    item(w, s1, "visa", "800.00", bill=bill)  # a rejected item frees its part of the source


def test_r63_source_of_another_entity_or_an_unposted_bill_is_refused(w):
    s1, a = seconded(w)
    run = approved(w, "2027-03")
    item(w, s1, "other_government_fee", "100.00", entry=run.journal_entry)  # a posted entry of the entity
    s2 = saudi(w, entity=w.sub)
    with pytest.raises(PayrollError) as exc:
        services.create_cost_item(s2, user=w.ps, kind="visa", amount="10.00", description_ar="تأشيرة",
                                  source_entry=run.journal_entry)
    assert exc.value.code == "cost_item_source"


def test_at59_fully_loaded_report_approved_then_read_by_billing(w):
    s1, a = seconded(w)
    bill = expense_bill(w, "2000.00")
    for kind, amount in (("medical_insurance", "1200.00"), ("iqama_renewal", "800.00")):
        services.approve_cost_item(item(w, s1, kind, amount, bill=bill), user=w.c)
    with pytest.raises(PayrollError) as exc:
        services.prepare_cost_report(w.alm, "2027-03", user=w.p)  # before the month's run is approved
    assert exc.value.code == "cost_report_run_missing"
    run = approved(w, "2027-03")
    ps = slip(run, s1)
    report = services.prepare_cost_report(w.alm, "2027-03", user=w.p)
    line = report.lines.get()
    assert (line.days, line.days_in_month) == (31, 31)
    assert line.gross == ps.gross and line.employer_gosi == ps.employer_gosi and line.eosb_accrual == ps.eosb_accrual
    assert line.cost_items == {"iqama_renewal": "800.00", "medical_insurance": "1200.00"}
    assert line.total == ps.gross + ps.employer_gosi + ps.eosb_accrual + D("2000.00") == report.total
    assert any("OQ-48" in x for x in line.warnings)
    port = PayrollCostPort()
    assert port.approved_cost(a, "2027-03") is None  # billing holds the period until the report is approved
    report = services.submit_cost_report(report, user=w.p)
    with pytest.raises(PermissionDenied):
        services.approve_cost_report(report, user=w.p)
    with pytest.raises(PermissionDenied):
        services.approve_cost_report(report, user=w.h)  # not the matrix row
    report = services.approve_cost_report(report, user=w.c)
    cost = port.approved_cost(a, "2027-03")
    assert cost.total == line.total and cost.currency == "SAR" and cost.version == 1
    data = services.cost_report_data(report, user=w.a)  # the accountant sees codes and totals, no names
    text = str(data)
    assert s1.employee_code in text and s1.full_name_en not in text and s1.full_name_ar not in text
    assert {k for k in data["lines"][0]} >= {"employee_code", "gross", "employer_gosi", "eosb_accrual", "cost_items"}
    with pytest.raises(PermissionDenied):
        services.cost_report_data(report, user=w.ps)  # another entity


def test_r35_partial_secondment_prorates_every_cost_and_a_new_version_supersedes(w):
    s1, a = seconded(w, start=date(2027, 3, 1), end=date(2027, 3, 10))
    bill = expense_bill(w, "3100.00")
    services.approve_cost_item(item(w, s1, "medical_insurance", "3100.00", bill=bill), user=w.c)
    run = approved(w, "2027-03")
    ps = slip(run, s1)
    report = services.approve_cost_report(services.submit_cost_report(
        services.prepare_cost_report(w.alm, "2027-03", user=w.p), user=w.p), user=w.c)
    line = report.lines.get()
    assert line.days == 10 and line.cost_items == {"medical_insurance": "1000.00"}  # 3,100.00 x 10/31
    assert line.gross == (ps.gross * 10 / 31).quantize(D("0.01"))
    # A late cost item for March: a new version, the first stays in force until the second is approved.
    bill2 = expense_bill(w, "310.00", on=date(2027, 3, 20))
    services.approve_cost_item(item(w, s1, "visa", "310.00", bill=bill2), user=w.c)
    v2 = services.prepare_cost_report(w.alm, "2027-03", user=w.p)
    assert v2.version == 2 and PayrollCostPort().approved_cost(a, "2027-03").version == 1
    v2 = services.approve_cost_report(services.submit_cost_report(v2, user=w.p), user=w.c)
    assert SecondmentCostReport.objects.get(pk=report.pk).status == "superseded"
    cost = PayrollCostPort().approved_cost(a, "2027-03")
    assert cost.version == 2 and cost.total == line.total + D("100.00")


def test_r35_report_refused_at_approval_when_a_source_changed(w):
    s1, a = seconded(w)
    approved(w, "2027-03")
    report = services.submit_cost_report(services.prepare_cost_report(w.alm, "2027-03", user=w.p), user=w.p)
    bill = expense_bill(w, "500.00")
    services.approve_cost_item(item(w, s1, "visa", "500.00", bill=bill), user=w.c)
    with pytest.raises(PayrollError) as exc:
        services.approve_cost_report(report, user=w.c)
    assert exc.value.code == "recalculate"
    report = services.return_cost_report(report, user=w.c)
    report = services.prepare_cost_report(w.alm, "2027-03", user=w.p)
    assert report.lines.get().cost_items == {"visa": "500.00"}
