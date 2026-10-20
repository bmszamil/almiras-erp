"""Bai' ajel review and compliance fixes of 2026-10-20 (invoicing R124 to R128; payroll R47 to R49).

Each fix has a negative regression test: the asset's purchase bill is never reversed or credited on 1250 while a sale
or contract relies on it (review 1); 1240, 1241 and 4610 refuse manual journals (review 3); the employee's name is not
copied into invoicing (review 10); real estate (A3) and future sale dates (A6) are refused; a credit note on a bai'
ajel invoice exists only as a payroll-approved ibra' (A8); the invoice is delivered after reporting and the delivery
is logged (A1). Payroll's fixtures (anonymised), invoicing's fake ZATCA and a fixed Riyadh clock; nothing external.
"""
from datetime import date

import pytest
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, connection, transaction

from core.access.models import AccessLog
from core.access.testing import make_user, member
from invoicing import services as inv
from invoicing.conftest import zatca  # noqa: F401  (invoicing's fake ZATCA port, a fixture)
from invoicing.models import DeferredSaleAdjustment, InvoicingError, PurchaseDocument, SalesDocument
from invoicing.test_deferred_sale import (  # noqa: F401  (the real port and its world)
    approve, balance, bilingual, bw, collect, contract, direct, entry_by_account, invoicing, sale_doc, sold)
from ledger import services as ledger
from ledger.models import LedgerError
from payroll import services as payroll
from payroll.conftest import D, asset_bill, clock, pclock, saudi, w  # noqa: F401  (payroll's fixtures)
from payroll.models import DeferredSaleContract, PayrollError


def credit_note(w, bill, account="1250", amount="100.00", number="CN-1"):
    """A vendor credit note on ``bill`` (test data), submitted."""
    doc = inv.create_purchase_document(w.alm, user=w.ap, vendor=w.vendor, vendor_document_number=number,
                                       vendor_document_date=date(2027, 3, 1), accounting_date=date(2027, 3, 1),
                                       doc_type="vendor_credit_note", original_document=bill)
    inv.add_purchase_line(doc, user=w.ap, account=account, quantity="1", unit_price=amount, description_ar="خصم")
    doc.refresh_from_db()
    _, net, charged, _, _, _, _ = inv._purchase_amounts(doc, list(doc.lines.select_related("vat_rate")))
    inv.update_purchase_document(doc, user=w.ap, vendor_total_vat=charged, vendor_total_gross=net + charged)
    return inv.submit_purchase_document(doc, user=w.ap)


def accountant(w):
    if not hasattr(w, "acct"):
        w.acct = make_user("acct_rev")
        member(w.acct, w.alm, "accountant")
    return w.acct


# --- review 1 (R124): the asset's purchase bill stays bought ---------------------------------------------------------

def test_review1_bill_of_an_invoiced_sale_is_never_reversed(bw, django_capture_on_commit_callbacks):
    c = approve(bw, contract(bw, saudi(bw)), django_capture_on_commit_callbacks)
    assert DeferredSaleContract.objects.get(pk=c.pk).status == "sale_invoiced"  # issued, not reported yet
    bill = PurchaseDocument.objects.get(pk=c.purchase_bill_id)
    with pytest.raises(InvoicingError) as exc:
        inv.reverse_document(bill, user=accountant(bw), reason="wrong bill")
    assert exc.value.code == "bai_ajel_asset_bill" and bill.number in bilingual(exc.value)
    assert PurchaseDocument.objects.get(pk=bill.pk).status == "posted"


def test_review1_bill_of_a_reported_sale_is_never_reversed(sold):
    w, s1, c, doc = sold
    bill = PurchaseDocument.objects.get(pk=c.purchase_bill_id)
    with pytest.raises(InvoicingError) as exc:
        inv.reverse_document(bill, user=accountant(w), reason="wrong bill")
    assert exc.value.code == "bai_ajel_asset_bill"
    assert balance(w.alm, "1250") == D("0.00")  # the asset left at its cost with the sale (E28) and stays sold


def test_review1_bill_recorded_as_a_contracts_acquisition_is_never_reversed_or_credited_on_1250(bw):
    c = contract(bw, saudi(bw))  # asset acquired, no sale yet
    bill = PurchaseDocument.objects.get(pk=c.purchase_bill_id)
    with pytest.raises(InvoicingError) as exc:
        inv.reverse_document(bill, user=accountant(bw), reason="wrong bill")
    assert exc.value.code == "bai_ajel_asset_bill"
    with pytest.raises(InvoicingError) as exc:  # a vendor credit note crediting 1250 on that bill
        credit_note(bw, bill)
    assert "1250" in bilingual(exc.value)
    assert credit_note(bw, bill, account="5900", number="CN-2").status == "submitted"  # other lines stay possible


def test_review1_bill_is_not_reversed_behind_invoicings_back(bw):
    c = contract(bw, saudi(bw))
    bill = PurchaseDocument.objects.get(pk=c.purchase_bill_id)
    from core import access

    grant = access.require(accountant(bw), bw.alm, "invoicing.reverse_document", document=bill)
    with pytest.raises(PermissionDenied):  # registered reverse_guard: only invoicing.reverse_document reverses it
        ledger.reverse_entry(bill.journal_entry, grant, reason="direct")


def test_review1_an_unrelated_bill_is_still_reversed(bw):
    other = asset_bill(bw, "500.00")
    assert inv.reverse_document(other, user=accountant(bw), reason="duplicate").status == "reversed"


@pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL triggers")
def test_pg_review1_the_database_refuses_reversing_or_crediting_a_bai_ajel_bill(sold):
    w, s1, c, doc = sold
    other = contract(w, s1, cost="400.00", profit="40.00", term=2)  # acquired only: the payroll trigger
    for bill_id in (c.purchase_bill_id, other.purchase_bill_id):
        with pytest.raises(DatabaseError, match="bai' ajel"), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("UPDATE invoicing_purchasedocument SET status = 'reversed' WHERE id = %s", [bill_id])
    assert PurchaseDocument.objects.get(pk=c.purchase_bill_id).status == "posted"


# --- review 3 (R127): 1240, 1241 and 4610 are restricted -------------------------------------------------------------

@pytest.mark.parametrize("code", ["1240", "1241", "4610"])
def test_review3_manual_journals_never_touch_the_bai_ajel_accounts(bw, code):
    with pytest.raises(LedgerError) as exc:
        ledger.create_entry(bw.alm, date(2027, 3, 20), "Manual / يدوي",
                            [{"account": code, "debit": D("10.00")}, {"account": "1100", "credit": D("10.00")}],
                            user=accountant(bw))
    assert exc.value.code == "restricted_account" and code in " ".join(exc.value.messages)
    from ledger.models import JournalEntry

    assert not JournalEntry.objects.filter(entity=bw.alm, lines__account__code=code, status="draft").exists()


def test_review3_the_sale_and_its_profit_still_post_on_their_accounts(sold):
    w, s1, c, doc = sold  # the sale invoice booked 1240 and 1241 (allowed source)
    collect(w, c, "1610.00", on=date(2027, 4, 25))  # the profit recognition books 1241 and 4610
    assert balance(w.alm, "1240") == D("38640.00") and balance(w.alm, "4610") == D("-150.00")
    from ledger import restricted

    assert {"1240", "1241", "4610"} <= restricted.codes()


# --- review 10 (PDPL, OQ-74): the employee's name is not copied into invoicing --------------------------------------

def test_review10_employee_name_not_copied_and_shown_only_unmasked_with_a_right_and_logged(sold):
    w, s1, c, doc = sold
    customer = doc.customer
    assert customer.name_ar == customer.name_en == customer.code == f"EMP-{s1.employee_code}"
    assert s1.full_name_ar not in (doc.buyer_name_ar or "") and s1.full_name_en not in (doc.buyer_name_en or "")
    masked = inv.view_customer(customer, user=w.v)  # viewer: invoicing.view without personal data
    assert masked["masked"] and masked["name_ar"] == inv.MASK
    before = AccessLog.objects.filter(event="personal_data_viewed").count()
    shown = inv.view_customer(customer, user=w.p)  # the payroll officer holds payroll.view_payslip
    assert not shown["masked"] and shown["name_ar"] == s1.full_name_ar and shown["name_en"] == s1.full_name_en
    assert AccessLog.objects.filter(event="personal_data_viewed").count() == before + 1
    with pytest.raises(PermissionDenied):  # SUB's officer: no right in ALM at all
        inv.view_customer(customer, user=w.ps)


# --- A3 (R125): no real estate; A6 (R126): no future sale date -------------------------------------------------------

def test_a3_real_estate_is_refused_in_payroll_and_invoicing(bw):
    s1 = saudi(bw)
    c = contract(bw, s1, acquire=False)
    for asset_type, code in (("real_estate", "real_estate_not_allowed"), ("", "asset_type"), (None, "asset_type")):
        with pytest.raises(PayrollError) as exc:
            payroll.record_asset_acquisition(c, user=bw.p, purchase_bill=asset_bill(bw, "30000.00"),
                                             possession_date=date(2027, 2, 16), possession_evidence_ref="DEED-1",
                                             asset_type=asset_type)
        assert exc.value.code == code and bilingual(exc.value)
    assert DeferredSaleContract.objects.get(pk=c.pk).status == "terms_approved"
    acquired = contract(bw, s1)
    for asset_type, code in (("real_estate", "real_estate_not_allowed"), ("", "asset_type_required")):
        with pytest.raises(InvoicingError) as exc:
            direct(bw, acquired, asset_type=asset_type)
        assert exc.value.code == code and bilingual(exc.value)
    assert "RETT" in " ".join(inv.REAL_ESTATE_REFUSED)


def test_a6_a_sale_dated_after_today_is_refused(bw):
    c = contract(bw, saudi(bw))
    with pytest.raises(PayrollError) as exc:  # payroll's clock: 2027-03-20
        payroll.approve_deferred_sale(c, user=bw.mp, sale_date=date(2027, 3, 21))
    assert exc.value.code == "sale_date_future" and bilingual(exc.value)
    with pytest.raises(InvoicingError) as exc:  # invoicing's Riyadh clock: 2027-03-20
        direct(bw, c, sale_date=date(2027, 3, 21))
    assert exc.value.code == "sale_date_future" and bilingual(exc.value)
    assert not SalesDocument.objects.filter(deferred_sale__isnull=False).exists()


# --- A8 (R128): a credit note on a bai' ajel invoice only as a payroll-approved ibra' --------------------------------

def rebate(w, c, amount="600.00"):
    return payroll.prepare_deferred_sale_rebate(c, user=w.p, amount=amount, reason_ar="سداد مبكر",
                                                request_ref="REQ-EARLY-1")


def approve_rebate(w, r, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        return payroll.approve_deferred_sale_rebate(r, user=w.mp)


@pytest.fixture
def sold_contract(sold):
    w, s1, c, doc = sold
    payroll._refresh_deferred_sales(w.alm)
    return w, s1, DeferredSaleContract.objects.get(pk=c.pk), doc


def test_a8_ibra_issues_a_simplified_credit_note_and_reduces_the_instalments(sold_contract,
                                                                           django_capture_on_commit_callbacks):
    w, s1, c, doc = sold_contract
    r = approve_rebate(w, rebate(w, c), django_capture_on_commit_callbacks)
    adj = DeferredSaleAdjustment.objects.get(source_ref=f"payroll.deferredsalerebate:{r.pk}")
    note = adj.credit_note
    assert (note.doc_type, note.zatca_profile, note.original_document_id, note.source) == (
        "credit_note", "simplified", doc.pk, "system:deferred_sale")
    assert note.created_by == w.system and note.approved_by == w.mp and note.status == "issued"
    assert (note.total_net, note.total_vat, note.total_gross) == (D("600.00"), D("90.00"), D("690.00"))
    assert list(note.lines.values_list("income_account__code", flat=True)) == ["1241"]
    assert (r.status, r.vat_amount, r.credit_note_ref) == ("approved", D("90.00"), note.number)
    amounts = [i.amount for i in c.installments.order_by("period")]
    assert sum(amounts) == D("38640.00") - D("690.00") and amounts[-1] == D("920.00")  # 1,610 - 690 from the last
    final = w.zatca.report(note)
    assert entry_by_account(final.journal_entry) == {"1241": D("600.00"), "2200": D("90.00"), "1240": D("-690.00")}
    assert final.journal_entry.posted_by == w.mp
    rows = inv.reconcile_subledger(w.alm, date(2027, 3, 31), user=accountant(w))  # 1200 never sees it (R39)
    assert all(r["difference"] == 0 for r in rows)
    for _ in range(24):  # every remaining instalment collected: profit 3,600 - 600, recognised exactly
        collect(w, c, str(c.installments.filter(status="planned").order_by("period", "pk").first().amount))
    assert balance(w.alm, "4610") == D("-3000.00") and balance(w.alm, "1241") == D("0.00")
    assert balance(w.alm, "1240") == D("38640.00") - D("690.00")  # payroll's runs credit 1240 themselves (E11)


def test_a8_refusals(sold_contract, django_capture_on_commit_callbacks):
    w, s1, c, doc = sold_contract
    clerk = make_user("ar_clerk_a8")
    member(clerk, w.alm, "receivables_clerk")
    with pytest.raises(InvoicingError) as exc:  # never by hand
        inv.create_sales_document(w.alm, user=clerk, customer=doc.customer, issue_date=date(2027, 3, 21),
                                  doc_type="credit_note", original_document=doc)
    assert exc.value.code == "deferred_sale_note"
    with pytest.raises(PayrollError) as exc:
        payroll.prepare_deferred_sale_rebate(c, user=w.p, amount="10.00", reason_ar="", request_ref="REQ")
    assert exc.value.code == "rebate_invalid"
    r = rebate(w, c, amount="3600.01")  # more than the unearned profit
    with pytest.raises(PermissionDenied):  # the preparer never approves (and holds no sale approval)
        payroll.approve_deferred_sale_rebate(r, user=w.p)
    with pytest.raises(InvoicingError) as exc:
        approve_rebate(w, r, django_capture_on_commit_callbacks)
    assert exc.value.code == "rebate_too_large" and bilingual(exc.value)
    assert not SalesDocument.objects.filter(doc_type="credit_note").exists()
    with pytest.raises(PayrollError) as exc:  # one open rebate per contract
        rebate(w, c)
    assert exc.value.code == "rebate_open"
    payroll.discard_deferred_sale_rebate(r, user=w.p)
    collect(w, c, "1610.00")  # 150.00 recognised: at most 3,450.00 may still be waived
    r2 = rebate(w, c, amount="3450.01")
    with pytest.raises(InvoicingError) as exc:
        approve_rebate(w, r2, django_capture_on_commit_callbacks)
    assert exc.value.code == "rebate_too_large"


def test_a8_a_rejected_credit_note_is_replaced_only_after_the_rejection(sold_contract,
                                                                       django_capture_on_commit_callbacks):
    w, s1, c, doc = sold_contract
    r = approve_rebate(w, rebate(w, c), django_capture_on_commit_callbacks)
    with pytest.raises(PayrollError) as exc:  # live: not approved twice
        approve_rebate(w, r, django_capture_on_commit_callbacks)
    assert exc.value.code == "rebate_status"
    first = SalesDocument.objects.get(doc_type="credit_note")
    w.zatca.reject(first)
    total = sum(i.amount for i in c.installments.all())
    r = approve_rebate(w, r, django_capture_on_commit_callbacks)
    new = DeferredSaleAdjustment.objects.get(active=True).credit_note
    assert new.pk != first.pk and r.credit_note_ref == new.number
    assert sum(i.amount for i in c.installments.all()) == total  # the instalments were reduced once


def test_a8_a_contract_not_sold_takes_no_rebate(bw, django_capture_on_commit_callbacks):
    c = approve(bw, contract(bw, saudi(bw)), django_capture_on_commit_callbacks)  # sale_invoiced
    with pytest.raises(PayrollError) as exc:
        rebate(bw, c)
    assert exc.value.code == "sale_status"


@pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL triggers")
def test_pg_a8_adjustment_rows_are_locked(sold_contract, django_capture_on_commit_callbacks):
    w, s1, c, doc = sold_contract
    approve_rebate(w, rebate(w, c), django_capture_on_commit_callbacks)
    adj = DeferredSaleAdjustment.objects.get()
    for sql in ("UPDATE invoicing_deferredsaleadjustment SET net_amount = 1 WHERE id = %s",
                "UPDATE invoicing_deferredsaleadjustment SET active = false WHERE id = %s",
                "DELETE FROM invoicing_deferredsaleadjustment WHERE id = %s",
                "DELETE FROM payroll_deferredsalerebate WHERE contract_id = %s"):
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(sql, [adj.pk if "adjustment" in sql else c.pk])


# --- A1: the invoice reaches the employee after reporting, and the delivery is logged ---------------------------------

def test_a1_the_sale_invoice_is_delivered_after_reporting_and_logged(bw, django_capture_on_commit_callbacks):
    c = approve(bw, contract(bw, saudi(bw)), django_capture_on_commit_callbacks)
    with pytest.raises(PayrollError) as exc:  # not reported yet
        payroll.record_sale_invoice_delivery(c, user=bw.p, channel="printed")
    assert exc.value.code == "sale_not_posted" and bilingual(exc.value)
    bw.zatca.report(sale_doc(c))
    payroll._refresh_deferred_sales(bw.alm)
    with pytest.raises(PayrollError) as exc:
        payroll.record_sale_invoice_delivery(c, user=bw.p, channel="carrier pigeon")
    assert exc.value.code == "channel"
    with pytest.raises(PermissionDenied):
        payroll.record_sale_invoice_delivery(c, user=bw.v, channel="printed")
    row = payroll.record_sale_invoice_delivery(c, user=bw.p, channel="electronic", reference="ESS-INBOX-1")
    assert row.details["action"] == "bai_ajel_invoice_delivered" and row.details["channel"] == "electronic"
    assert row.details["invoice"] == sale_doc(c).number and row.actor == bw.p
