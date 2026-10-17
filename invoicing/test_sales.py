"""Sales documents (invoicing.md R5 to R20, R40 to R48, R56, R57, R68 to R74; AT-1 to AT-20, AT-32 to AT-68)."""
from datetime import date
from decimal import Decimal

import pytest

from core import access
from core.access.testing import make_agent, make_user, member
from core.access.models import Membership, Role, RolePermission
from core.services import change_entity_settings
from invoicing import services
from invoicing.conftest import ADDRESS, D, approve, final, invoice, make_entity, usd_rate
from invoicing.models import (
    Customer,
    DocumentSequence,
    InvoicingError,
    NoteReason,
    ReceivableAllocation,
    SalesDocument,
    SalesDocumentLine,
    VatExemptionReason,
)
from ledger import services as ledger
from ledger.models import FiscalPeriod, JournalEntry, LedgerError

pytestmark = pytest.mark.django_db


def je_lines(doc):
    doc.refresh_from_db()
    return sorted(
        (l.account.code, l.debit, l.credit, l.counterparty_entity.code if l.counterparty_entity else None)
        for l in doc.journal_entry.lines.select_related("account", "counterparty_entity")
    )


def messages(exc):
    return " ".join(exc.value.messages)


# --- AT-1, AT-35: VAT per category at document level (R9) ------------------------------------------

def test_at1_invoice_vat_at_document_level_and_posting_on_clearance(w):
    doc = invoice(w, [("rent_commercial", "3", "1000.00"), ("rent_commercial", "1", "333.33")])
    issued = approve(w, doc)
    assert issued.status == "issued" and issued.number == "ALM-INV-2027-000001"
    assert [l.vat_amount for l in issued.lines.order_by("line_no")] == [D("450.00"), D("50.00")]  # display only
    assert (issued.total_net, issued.total_vat, issued.total_gross) == (D("3333.33"), D("500.00"), D("3833.33"))
    assert issued.vat_breakdown == [{"category": "S", "rate": "15.00", "taxable": "3333.33", "vat": "500.00",
                                     "vat_sar": "500.00", "exemption_reasons": []}]
    assert issued.journal_entry is None and issued.zatca_status == "pending"  # nothing posts before ZATCA (R41)
    assert w.zatca.submitted == [(issued.pk, "standard", w.cfo.pk)]
    done = w.zatca.clear(issued)
    assert done.status == "final"
    assert je_lines(done) == [("1200", D("3833.33"), D("0"), None), ("2200", D("0"), D("500.00"), None),
                              ("4100", D("0"), D("3333.33"), None)]  # one income line per account
    entry = done.journal_entry
    assert entry.reference == done.number and entry.status == "posted" and entry.posted_by == w.cfo
    assert entry.document_ref == ("invoicing.salesdocument", done.pk)


def test_at35_category_vat_differs_from_sum_of_line_vat(w):
    doc = invoice(w, [("rent_commercial", "1", "10.03")] * 3)
    done = final(w, doc)
    assert [l.vat_amount for l in done.lines.all()] == [D("1.50")] * 3  # sum 4.50
    assert (done.total_net, done.total_vat, done.total_gross) == (D("30.09"), D("4.51"), D("34.60"))
    assert je_lines(done) == [("1200", D("34.60"), D("0"), None), ("2200", D("0"), D("4.51"), None),
                              ("4100", D("0"), D("30.09"), None)]


def test_at15_exempt_line_needs_a_reason_and_books_no_vat(w):
    doc = services.create_sales_document(w.alm, user=w.clerk, customer=w.customer, issue_date=date(2027, 1, 10),
                                         supply_date=date(2027, 1, 10))
    with pytest.raises(InvoicingError) as exc:
        services.add_sales_line(doc, user=w.clerk, income_type="rent_residential", quantity="1", unit_price="1000",
                                description_ar="إيجار سكني")
    assert "exemption reason" in messages(exc) and "سبب إعفاء" in messages(exc)
    with pytest.raises(InvoicingError):  # a zero-rated reason is not an exempt reason
        services.add_sales_line(doc, user=w.clerk, income_type="rent_residential", quantity="1", unit_price="1000",
                                description_ar="إيجار سكني", vat_exemption_reason="VATEX-SA-32")
    services.add_sales_line(doc, user=w.clerk, income_type="rent_residential", quantity="1", unit_price="1000",
                            description_ar="إيجار سكني", vat_exemption_reason="VATEX-SA-30")
    done = final(w, services.submit_sales_document(doc, user=w.clerk))
    assert done.total_vat == D("0.00") and done.vat_breakdown[0]["exemption_reasons"] == ["VATEX-SA-30"]
    assert je_lines(done) == [("1200", D("1000.00"), D("0"), None), ("4120", D("0"), D("1000.00"), None)]  # E3


def test_r10_rate_is_taken_on_the_supply_date(w):
    w.alm.__class__.objects.filter(pk=w.alm.pk).update(vat_effective_date=date(2018, 1, 1))
    doc = invoice(w, supply_date=date(2020, 6, 30))  # the 5% rate was in force then
    assert doc.lines.get().vat_rate.rate == D("5.00")
    services.return_to_draft(doc, user=w.clerk)
    services.update_sales_document(doc, user=w.clerk, supply_date=date(2027, 1, 10))
    services.submit_sales_document(doc, user=w.clerk)
    assert approve(w, doc).total_vat == D("150.00")  # priced again at issue (R10)


# --- R57 / AT-51: workflow and segregation ---------------------------------------------------------

def test_at51_creator_and_submitter_cannot_approve(w):
    member(w.clerk, w.alm, "approver")  # staff U who also holds the approver role
    doc = invoice(w)
    with pytest.raises(access.AccessDenied) as exc:
        approve(w, doc, w.clerk)
    assert exc.value.code == "self_approval"
    assert SalesDocument.objects.get(pk=doc.pk).status == "submitted"
    issued = approve(w, doc)
    assert issued.status == "issued" and issued.approved_by == w.cfo
    assert w.zatca.clear(issued).journal_entry.posted_by == w.cfo


def test_r57_segregation_switch_does_not_let_a_creator_approve_a_posting_document(w):
    member(w.clerk, w.alm, "approver")
    services.update_settings(w.alm, user=w.cfo, segregation_required=False)
    doc = invoice(w)
    with pytest.raises(access.AccessDenied) as exc:  # access OQ-19: postings are always segregated
        approve(w, doc, w.clerk)
    assert exc.value.code == "self_approval"


def test_r57_transitions(w):
    doc = invoice(w)
    with pytest.raises(InvoicingError):
        services.submit_sales_document(doc, user=w.clerk)  # already submitted
    services.return_to_draft(doc, user=w.clerk)
    assert SalesDocument.objects.get(pk=doc.pk).status == "draft"
    with pytest.raises(InvoicingError):
        approve(w, doc)  # a draft is not approved
    services.submit_sales_document(doc, user=w.clerk)
    done = final(w, doc)
    with pytest.raises(InvoicingError):
        services.return_to_draft(done, user=w.clerk)


def test_at2_status_never_set_directly(w):
    done = final(w, invoice(w))
    for status in ("draft", "issued"):
        done.status = status
        with pytest.raises(InvoicingError):
            done.save()
        with pytest.raises(InvoicingError):
            SalesDocument.objects.filter(pk=done.pk).update(status=status)
    assert SalesDocument.objects.get(pk=done.pk).status == "final"
    with pytest.raises(InvoicingError):  # a new record cannot start issued either
        SalesDocument(entity=w.alm, doc_type="invoice", customer=w.customer, issue_date=date(2027, 1, 1),
                      currency_id="SAR", created_by=w.clerk, status="final").save()


def test_at5_issued_documents_are_immutable(w):
    issued = approve(w, invoice(w))
    line = issued.lines.get()
    line.quantity = D("2")
    with pytest.raises(InvoicingError):
        line.save()
    with pytest.raises(InvoicingError):
        SalesDocumentLine.objects.filter(pk=line.pk).update(quantity=D("2"))
    with pytest.raises(InvoicingError):
        line.delete()
    with pytest.raises(InvoicingError):
        issued.delete()
    with pytest.raises(InvoicingError):
        SalesDocument.objects.filter(pk=issued.pk).delete()
    with pytest.raises(InvoicingError):
        services.add_sales_line(issued, user=w.clerk, income_type="rent_commercial", quantity="1", unit_price="1",
                                description_ar="بند")
    issued.notes_en = "changed"
    with pytest.raises(InvoicingError):
        issued.save()


def test_at9_no_cancel_or_reversal_of_sales_documents(w):
    done = final(w, invoice(w))
    assert not hasattr(services, "cancel_sales_document")
    with pytest.raises(InvoicingError) as exc:
        services.reverse_document(done, user=w.accountant)
    assert exc.value.code == "sales_not_reversible"
    with pytest.raises(access.AccessDenied) as denied:  # the ledger refuses invoicing.reverse_document for sales too
        ledger.reverse_entry(done.journal_entry, access.require(w.accountant, w.alm, "invoicing.reverse_document",
                                                                 document=done))
    assert denied.value.code == "grant_wrong_permission"
    assert JournalEntry.objects.get(pk=done.journal_entry_id).status == "posted"


# --- numbering (R14, AT-6, AT-17, AT-19) ---------------------------------------------------------

def test_at6_numbers_are_consecutive_and_failures_use_none(w):
    first = approve(w, invoice(w))
    bad = invoice(w)
    w.customer.__class__.objects.filter(pk=w.customer.pk).update(street_en="", street_ar="")  # breaks R16
    with pytest.raises(InvoicingError):
        approve(w, bad)
    assert SalesDocument.objects.get(pk=bad.pk).number == ""
    w.customer.__class__.objects.filter(pk=w.customer.pk).update(street_en="Buyer Street")
    second = approve(w, bad)
    third = approve(w, invoice(w))
    assert [first.number, second.number, third.number] == [
        "ALM-INV-2027-000001", "ALM-INV-2027-000002", "ALM-INV-2027-000003"]
    assert DocumentSequence.objects.get(entity=w.alm, doc_type="INV", year=2027).last_number == 3


def test_numbers_are_per_entity_type_and_year(w):
    a = approve(w, invoice(w, issue_date=date(2027, 12, 31)))
    create_periods_2028(w.alm)
    b = approve(w, invoice(w, issue_date=date(2028, 1, 1)))
    assert (a.number, b.number) == ("ALM-INV-2027-000001", "ALM-INV-2028-000001")


def create_periods_2028(entity):
    from ledger.services_setup import create_monthly_periods

    create_monthly_periods(entity, 2028)


def test_at17_closed_period_issue_rejected_without_number_or_entry(w):
    FiscalPeriod.objects.filter(entity=w.alm, name="2027-01").update(status="closed")
    doc = invoice(w)
    with pytest.raises(InvoicingError) as exc:
        approve(w, doc)
    assert "2027-01 is closed" in messages(exc) and "مقفلة" in messages(exc)
    stored = SalesDocument.objects.get(pk=doc.pk)
    assert (stored.status, stored.number) == ("submitted", "")
    assert not DocumentSequence.objects.filter(entity=w.alm, doc_type="INV").exclude(last_number=0).exists()
    assert not JournalEntry.objects.exists()


def test_at19_zatca_rejection_keeps_the_number_and_books_nothing(w):
    issued = approve(w, invoice(w))
    tb_before = ledger.trial_balance(w.alm, date(2027, 12, 31), user=w.cfo)
    rejected = w.zatca.reject(issued)
    assert (rejected.status, rejected.zatca_status, rejected.number) == ("rejected", "rejected", "ALM-INV-2027-000001")
    assert rejected.zatca_messages[0]["text_ar"] and rejected.journal_entry is None
    assert not JournalEntry.objects.exists()
    assert ledger.trial_balance(w.alm, date(2027, 12, 31), user=w.cfo) == tb_before
    corrected = approve(w, invoice(w))  # D12: corrected and re-issued under a new number
    assert corrected.number == "ALM-INV-2027-000002"
    with pytest.raises(InvoicingError):  # a rejected document never posts later
        w.zatca.clear(rejected)


def test_at18_pending_document_cannot_be_allocated_and_posts_on_cleared_with_warnings(w):
    issued = approve(w, invoice(w))
    receipt = services.create_receipt(w.alm, user=w.clerk, customer=w.customer, date=date(2027, 1, 20),
                                      amount="1150.00")
    with pytest.raises(InvoicingError) as exc:
        services.add_receipt_application(receipt, issued, "1150.00", user=w.clerk)
    assert "final" in messages(exc)
    assert not w.zatca.is_issued(issued)
    done = w.zatca.clear(issued, status="cleared_with_warnings")
    assert done.status == "final" and done.journal_entry.status == "posted" and w.zatca.is_issued(done)


def test_r41_simplified_document_posts_on_reporting(w):
    person = services.create_customer(w.alm, user=w.clerk, code="P1", name_ar="عميل فرد", customer_kind="individual")
    doc = invoice(w, customer=person)
    issued = approve(w, doc)
    assert issued.zatca_profile == "simplified" and issued.title_ar == "فاتورة ضريبية مبسطة"
    done = w.zatca.report(issued, status="reported_with_warnings")
    assert done.status == "final" and done.journal_entry.posted_by == w.cfo


def test_r41_default_port_never_posts(w, settings):
    from invoicing import zatca_port

    zatca_port._override = None  # the configured default port: the zatca module is not built
    issued = approve(w, invoice(w))
    stored = SalesDocument.objects.get(pk=issued.pk)
    assert (stored.status, stored.zatca_status, stored.journal_entry_id) == ("issued", "pending", None)
    assert not zatca_port.get_port().is_issued(stored)


def test_r41_only_the_zatca_queue_records_results_and_posts(w):
    issued = approve(w, invoice(w))
    for user in (w.cfo, w.accountant):
        with pytest.raises(access.AccessDenied):
            services.on_zatca_result(issued, services.zatca_port.ZatcaResult(status="cleared"), user=user)
    with pytest.raises(access.AccessDenied):
        services.post_cleared_document(issued, user=w.cfo)
    assert SalesDocument.objects.get(pk=issued.pk).status == "issued"


def test_r41_document_lines_are_built_only_through_invoicing(w):
    issued = approve(w, invoice(w))
    w.zatca.respond(issued, "pending")
    grant = access.require(w.queue, w.alm, "invoicing.post_on_clearance", document=issued)
    with pytest.raises(LedgerError) as exc:  # a caller cannot pass its own lines (build_lines hook)
        ledger.create_entry(w.alm, issued.issue_date, "x", [{"account": "1200", "debit": 1}, {"account": "4100", "credit": 1}],
                            user=w.queue, post=True, grant=grant, posted_by=w.cfo, document=issued)
    assert exc.value.code == "document_builds_lines"
    with pytest.raises(InvoicingError) as exc2:  # and the hook works only inside invoicing.services
        ledger.create_entry(w.alm, issued.issue_date, "x", None, user=w.queue, post=True, grant=grant,
                            posted_by=w.cfo, document=issued)
    assert exc2.value.code == "post_through_services"
    assert not JournalEntry.objects.exists()


def test_at32_period_with_documents_awaiting_zatca_cannot_close(w):
    issued = approve(w, invoice(w, issue_date=date(2027, 3, 10)))
    period = FiscalPeriod.objects.get(entity=w.alm, name="2027-03")
    with pytest.raises(LedgerError) as exc:
        ledger.close_period(period, user=w.cfo)
    assert exc.value.code == "documents_pending" and issued.number in exc.value.messages[0]
    w.zatca.clear(issued)
    assert ledger.close_period(period, user=w.cfo).status == "closed"


# --- entity separation (R1 to R4; AT-3, AT-4, AT-20, AT-34, AT-50) --------------------------------

def test_at3_line_on_another_entitys_account_rejected(w):
    from ledger.models import Account

    doc = services.create_sales_document(w.alm, user=w.clerk, customer=w.customer, issue_date=date(2027, 1, 10))
    sub_4100 = Account.objects.get(entity=w.sub, code="4100")
    with pytest.raises(InvoicingError) as exc:
        services.add_sales_line(doc, user=w.clerk, income_type="rent_commercial", quantity="1", unit_price="10",
                                description_ar="بند", income_account=sub_4100)
    assert "belongs to another entity" in messages(exc)
    assert not doc.lines.exists() and not DocumentSequence.objects.exists()


def test_at4_users_see_and_create_only_in_their_entities(w):
    sub_customer = services.create_customer(w.sub, user=w.sub_clerk, code="S1", name_ar="عميل التابعة",
                                            is_vat_registered=True, vat_number="330000000000003", **ADDRESS)
    final(w, invoice(w))
    sub_doc = invoice(w, customer=sub_customer, user=w.sub_clerk, entity=w.sub)
    assert list(services.sales_documents(w.clerk)) == list(SalesDocument.objects.filter(entity=w.alm))
    assert sub_doc not in services.sales_documents(w.clerk)
    assert list(services.customers(w.clerk)) == [w.customer]
    with pytest.raises(access.AccessDenied):  # no right in SUB
        services.create_sales_document(w.sub, user=w.clerk, customer=sub_customer, issue_date=date(2027, 1, 10))
    with pytest.raises(InvoicingError) as exc:  # a SUB customer on an ALM document
        services.create_sales_document(w.alm, user=w.clerk, customer=sub_customer, issue_date=date(2027, 1, 10))
    assert exc.value.code == "entity_mismatch"
    with pytest.raises(access.AccessDenied):  # an approver in SUB cannot approve an ALM document
        approve(w, invoice(w), w.sub_cfo)


def test_at20_intercompany_invoice_uses_1210_with_counterparty(w):
    group_customer = services.create_customer(w.alm, user=w.clerk, code="G-SUB", name_ar="التابعة", group_entity=w.sub,
                                              is_vat_registered=True, vat_number="311111111100003")
    done = final(w, invoice(w, customer=group_customer))
    assert done.is_intercompany and done.journal_entry.lines.get(account__code="1210").counterparty_entity == w.sub
    assert je_lines(done)[0] == ("1210", D("1150.00"), D("0"), "SUB")
    assert ("4110", D("0"), D("1000.00"), None) in je_lines(done)  # intercompany rent default (E2)
    balances = ledger.intercompany_balances(date(2027, 12, 31), user=w.cfo, group=w.alm)
    assert balances[("ALM", "SUB")] == D("1150.00")
    with pytest.raises(InvoicingError):
        services.create_customer(w.alm, user=w.clerk, code="G-ALM", name_ar="نفسه", group_entity=w.alm)


def test_at50_group_buyer_snapshot_comes_from_the_buyer_entity_and_nothing_is_written_there(w):
    group_customer = services.create_customer(w.alm, user=w.clerk, code="G-SUB", name_ar="اسم قديم",
                                              group_entity=w.sub, is_vat_registered=True, vat_number="311111111100003")
    done = final(w, invoice(w, customer=group_customer))
    assert (done.zatca_profile, done.buyer_name_ar, done.buyer_vat_number, done.buyer_street_ar) == (
        "standard", w.sub.legal_name_ar, w.sub.vat_number, w.sub.street_ar)
    assert done.total_vat == D("150.00")
    assert not SalesDocument.objects.filter(entity=w.sub).exists()
    assert not JournalEntry.objects.filter(entity=w.sub).exists()  # D23: no mirror bill


def test_at34_new_entity_is_data_only(w):
    new = make_entity("NEW", "full", w.alm, vat="399999999900003", cr="1010000009")
    clerk, cfo = make_user("new_clerk"), make_user("new_cfo")
    member(clerk, new, "receivables_clerk")
    member(cfo, new, "approver")
    member(w.queue, new, "system_zatca_queue")
    customer = services.create_customer(new, user=clerk, code="N1", name_ar="عميل جديد", is_vat_registered=True,
                                        vat_number="310000000000003", street_ar="شارع", building_no="1111",
                                        district_ar="حي", city_ar="مدينة", postal_code="11111")
    doc = invoice(w, [("rent_commercial", "3", "1000.00"), ("rent_commercial", "1", "333.33")], customer=customer,
                  user=clerk, entity=new)
    done = final(w, doc, cfo)
    assert done.number == "NEW-INV-2027-000001" and done.total_vat == D("500.00")


# --- Sharia (R5, R6; AT-12 for sales, AT-13) ---------------------------------------------------

def test_at13_purification_income_posts_and_is_flagged(w):
    done = final(w, invoice(w, [("other", "1", "1200.00", {"income_account": "4900"})]))
    assert done.requires_purification
    assert ledger.purification_amount(w.alm, user=w.cfo) == D("1200.00")


def test_r5_sharia_prohibited_income_account_blocked_in_enforcing_entity(w):
    from ledger.models import Account

    Account.objects.filter(entity=w.alm, code="4600").update(sharia_prohibited=True)  # a fixture-only flag
    doc = services.create_sales_document(w.alm, user=w.clerk, customer=w.customer, issue_date=date(2027, 1, 10))
    with pytest.raises(InvoicingError) as exc:
        services.add_sales_line(doc, user=w.clerk, income_type="other", quantity="1", unit_price="10",
                                description_ar="بند", income_account="4600")
    assert "Sharia-prohibited" in messages(exc) and "محظور شرعًا" in messages(exc)


# --- buyer, seller and threshold rules (R11, R16, R48, R50, R69, R72) -------------------------------

def test_at8_standard_invoice_lists_every_missing_buyer_field(w):
    bare = services.create_customer(w.alm, user=w.clerk, code="C2", name_ar="بلا عنوان")
    Customer.objects.filter(pk=bare.pk).update(name_ar="")
    doc = services.create_sales_document(w.alm, user=w.clerk, customer=bare, issue_date=date(2027, 1, 10),
                                         supply_date=date(2027, 1, 10), zatca_profile="standard")
    services.add_sales_line(doc, user=w.clerk, income_type="rent_commercial", quantity="1", unit_price="10",
                            description_ar="بند")
    with pytest.raises(InvoicingError) as exc:
        services.submit_sales_document(doc, user=w.clerk)
    text = messages(exc)
    for missing in ("Buyer name in Arabic", "Buyer street", "Buyer city", "Buyer building number", "Buyer postal code",
                    "Buyer district", "Buyer ID"):
        assert missing in text
    assert "اسم المشتري بالعربية" in text


def test_at36_vat_registered_customer_needs_a_vat_number(w):
    with pytest.raises(InvoicingError) as exc:
        services.create_customer(w.alm, user=w.clerk, code="C3", name_ar="مسجل", is_vat_registered=True)
    assert "VAT number" in messages(exc)
    c = services.create_customer(w.alm, user=w.clerk, code="C3", name_ar="غير مسجل", street_ar="شارع", city_ar="مدينة",
                                 building_no="1111", district_ar="حي", postal_code="11111")
    services.add_customer_identifier(c, "CRN", "1010000003", user=w.clerk)
    done = final(w, invoice(w, customer=c))
    assert (done.buyer_other_id_scheme, done.buyer_other_id, done.buyer_vat_number) == ("CRN", "1010000003", "")
    with pytest.raises(InvoicingError):  # bad format
        services.update_customer(c, user=w.clerk, is_vat_registered=True, vat_number="123")


def test_at61_buyer_id_priority_tin_first(w):
    def customer(code):
        return services.create_customer(w.alm, user=w.clerk, code=code, name_ar="عميل", street_ar="شارع",
                                        city_ar="مدينة", building_no="1111", district_ar="حي", postal_code="11111")

    first, second = customer("T1"), customer("T2")
    services.add_customer_identifier(first, "TIN", "3000000001", user=w.clerk)
    services.add_customer_identifier(second, "CRN", "1010000004", user=w.clerk)
    services.add_customer_identifier(second, "TIN", "3000000002", user=w.clerk)
    assert final(w, invoice(w, customer=first)).buyer_other_id_scheme == "TIN"
    done = final(w, invoice(w, customer=second))
    assert (done.buyer_other_id_scheme, done.buyer_other_id) == ("TIN", "3000000002")


def test_at65_saudi_buyer_needs_district_foreign_buyer_does_not(w):
    sa = services.create_customer(w.alm, user=w.clerk, code="SA1", name_ar="عميل سعودي", is_vat_registered=True,
                                  vat_number="310000000000003", street_ar="شارع", city_ar="مدينة", building_no="1111",
                                  postal_code="11111")
    with pytest.raises(InvoicingError) as exc:
        invoice(w, customer=sa)
    assert "Buyer district" in messages(exc)
    ae = services.create_customer(w.alm, user=w.clerk, code="AE1", name_ar="عميل إماراتي", street_en="Sheikh Zayed Rd",
                                  city_en="Dubai", country="AE")
    services.add_customer_identifier(ae, "OTH", "AE0001", user=w.clerk)
    assert final(w, invoice(w, customer=ae)).status == "final"


def test_at38_at62_simplified_threshold(w):
    business = services.create_customer(w.alm, user=w.clerk, code="B1", name_ar="منشأة")
    government = services.create_customer(w.alm, user=w.clerk, code="G1", name_ar="جهة حكومية", customer_kind="government")
    person = services.create_customer(w.alm, user=w.clerk, code="P1", name_ar="فرد", customer_kind="individual")
    vat_business = w.customer

    def simplified(customer, price):  # price excl. VAT; gross = price x 1.15
        return invoice(w, [("rent_commercial", "1", price)], customer=customer, zatca_profile="simplified")

    for customer in (vat_business, business, government):
        with pytest.raises(InvoicingError) as exc:  # 869.57 + 130.44 = 1,000.01; and 1,000.00 exactly
            approve(w, simplified(customer, "869.57"))
        assert exc.value.code == "simplified_threshold" and "1,000" in messages(exc)
    exact = invoice(w, [("rent_commercial", "1", "1000.00", {"vat_category": "Z",
                                                                "vat_exemption_reason": "VATEX-SA-32"})],
                    customer=business, zatca_profile="simplified")
    with pytest.raises(InvoicingError) as exc_exact:  # 1,000.00 itself must be standard ("less than")
        approve(w, exact)
    assert exc_exact.value.code == "simplified_threshold"
    assert approve(w, simplified(person, "4347.83")).status == "issued"  # 5,000.00 to an individual
    assert approve(w, simplified(business, "869.56")).total_gross == D("999.99")


def test_at42_simplified_document_stores_no_buyer_id(w):
    person = services.create_customer(w.alm, user=w.clerk, code="P1", name_ar="فرد", customer_kind="individual")
    services.add_customer_identifier(person, "NAT", "1000000001", user=w.clerk)
    done = final(w, invoice(w, customer=person))
    assert (done.zatca_profile, done.buyer_other_id_scheme, done.buyer_other_id) == ("simplified", "", "")


def test_r16_flags_checked_br_ksa_07_and_31(w):
    with pytest.raises(InvoicingError) as exc:
        invoice(w, flag_export=True, flag_self_billed=True)
    assert "self-billed invoice cannot be an export" in messages(exc)
    person = services.create_customer(w.alm, user=w.clerk, code="P1", name_ar="فرد", customer_kind="individual")
    with pytest.raises(InvoicingError) as exc2:
        invoice(w, customer=person, flag_export=True)
    assert "simplified invoice cannot be export" in messages(exc2)


def test_r16_export_invoice_carries_no_buyer_vat_number(w):
    done = final(w, invoice(w, [("rent_commercial", "1", "100", {"vat_category": "Z",
                                                                 "vat_exemption_reason": "VATEX-SA-33"})],
                            flag_export=True))
    assert done.buyer_vat_number == ""


def test_at16_entity_without_vat_number_cannot_issue_s_lines(w):
    Customer.objects.none()
    w.alm.__class__.objects.filter(pk=w.alm.pk).update(vat_number="")
    w.alm.refresh_from_db()
    with pytest.raises(InvoicingError) as exc:
        invoice(w)
    assert "vat_number" in messages(exc)
    doc = invoice(w, [("rent_commercial", "1", "100", {"vat_category": "Z", "vat_exemption_reason": "VATEX-SA-33"})])
    assert approve(w, doc).status == "issued"  # OQ-17: Z/E/O allowed


def test_oq32_standard_rated_supply_before_vat_registration_rejected(w):
    with pytest.raises(InvoicingError) as exc:
        invoice(w, issue_date=date(2027, 1, 10), supply_date=date(2023, 12, 31))
    assert "VAT registration date" in messages(exc)


def test_at48_seller_fields_required_and_snapshot_holds_the_national_address(w):
    w.alm.__class__.objects.filter(pk=w.alm.pk).update(building_no="", street_ar="")
    with pytest.raises(InvoicingError) as exc:
        invoice(w)
    assert "building_no" in messages(exc) and "street_ar" in messages(exc)
    w.alm.__class__.objects.filter(pk=w.alm.pk).update(building_no="1234", street_ar="شارع الاختبار")
    done = final(w, invoice(w))
    assert (done.seller_building_no, done.seller_street_ar, done.seller_city_ar, done.seller_postal_code) == (
        "1234", "شارع الاختبار", "الرياض", "12345")


def test_at66_r72_snapshot_holds_cr_and_unified_number(w):
    done = final(w, invoice(w))
    assert (done.seller_cr_number, done.seller_unified_number, done.seller_vat_number) == (
        "1010000001", "7000000001", "300000000000003")
    assert done.seller_legal_name_ar == "شركة ALM للاختبار"


def test_at7_snapshot_frozen_after_issue(w):
    done = final(w, invoice(w))
    services.update_customer(w.customer, user=w.clerk, street_ar="شارع جديد", name_ar="اسم جديد")
    change = make_user("mp")
    member(change, w.alm, "managing_partner")
    w.alm.__class__.objects.filter(pk=w.alm.pk).update(street_ar="شارع آخر")
    stored = SalesDocument.objects.get(pk=done.pk)
    assert (stored.buyer_street_ar, stored.buyer_name_ar, stored.seller_street_ar) == (
        "شارع المشتري", "شركة المشتري التجريبية", "شارع الاختبار")


# --- credit and debit notes (R17 to R19, R37; AT-10, AT-11, AT-63, AT-64) -------------------------

def note(w, original, doc_type="credit_note", lines=(("rent_commercial", "1", "4000.00"),), **fields):
    fields.setdefault("payment_means_code", "30")
    fields.setdefault("reason_ar", "تخفيض متفق عليه")
    return invoice(w, list(lines), doc_type=doc_type, original_document=original, **fields)


def test_at10_credit_note_posts_e4_auto_allocates_and_is_capped(w):
    original = final(w, invoice(w, [("rent_commercial", "1", "10000.00")]))
    assert original.total_gross == D("11500.00")
    cn = final(w, note(w, original))
    assert cn.number == "ALM-CN-2027-000001" and cn.total_gross == D("4600.00")
    assert je_lines(cn) == [("1200", D("0"), D("4600.00"), None), ("2200", D("600.00"), D("0"), None),
                            ("4100", D("4000.00"), D("0"), None)]
    assert services.open_amount(original, user=w.clerk) == D("6900.00")
    assert services.payment_state(original, user=w.clerk) == "partial"
    alloc = ReceivableAllocation.objects.get(target=original)
    assert (alloc.credit_note, alloc.amount) == (cn, D("4600.00"))
    with pytest.raises(InvoicingError) as exc:  # 7,000.00 gross exceeds the 6,900.00 creditable
        approve(w, note(w, original, lines=[("rent_commercial", "1", "6086.96")]))
    assert "exceeds" in messages(exc)


def test_at11_note_reason_rules(w):
    original = final(w, invoice(w))
    with pytest.raises(InvoicingError) as exc:
        note(w, original, lines=[("rent_commercial", "1", "100")], reason_ar="")
    assert "reason in Arabic" in messages(exc)
    with pytest.raises(InvoicingError):
        note(w, original, lines=[("rent_commercial", "1", "100")], reason_ar="", reason_en="English only")
    other = services.create_customer(w.alm, user=w.clerk, code="C9", name_ar="عميل آخر", is_vat_registered=True,
                                     vat_number="310000000000003", street_ar="شارع", city_ar="مدينة",
                                     building_no="1111", district_ar="حي", postal_code="11111")
    with pytest.raises(InvoicingError) as exc2:
        note(w, original, lines=[("rent_commercial", "1", "100")], customer=other)
    assert "another entity or customer" in messages(exc2)
    assert approve(w, note(w, original, lines=[("rent_commercial", "1", "100")])).status == "issued"


def test_at63_payment_means_required_and_reason_prefilled(w):
    original = final(w, invoice(w))
    with pytest.raises(InvoicingError) as exc:
        note(w, original, lines=[("rent_commercial", "1", "100")], payment_means_code="", reason_ar=None,
             reason_code="value_amendment")
    assert "Payment means code required" in messages(exc) and "وسيلة الدفع" in messages(exc)
    reason = NoteReason.objects.get(code="value_amendment")
    doc = services.create_sales_document(w.alm, user=w.clerk, customer=w.customer, issue_date=date(2027, 1, 10),
                                         supply_date=date(2027, 1, 10), doc_type="credit_note",
                                         original_document=original, reason_code="value_amendment",
                                         payment_means_code="30")
    assert (doc.reason_ar, doc.reason_en) == (reason.name_ar, reason.name_en)
    services.add_sales_line(doc, user=w.clerk, income_type="rent_commercial", quantity="1", unit_price="100",
                            description_ar="تخفيض")
    issued = approve(w, services.submit_sales_document(doc, user=w.clerk))
    assert issued.original_document.number == original.number  # BT-25 is the original's number


def test_debit_note_posts_like_an_invoice(w):
    original = final(w, invoice(w))
    dn = final(w, note(w, original, doc_type="debit_note", lines=[("rent_commercial", "1", "200.00")]))
    assert dn.number == "ALM-DN-2027-000001"
    assert je_lines(dn) == [("1200", D("230.00"), D("0"), None), ("2200", D("0"), D("30.00"), None),
                            ("4100", D("0"), D("200.00"), None)]


def test_oq43_one_original_invoice_per_note(w):
    a, b = final(w, invoice(w)), final(w, invoice(w))
    with pytest.raises(InvoicingError) as exc:
        services.create_sales_document(w.alm, user=w.clerk, customer=w.customer, issue_date=date(2027, 1, 10),
                                       doc_type="credit_note", original_document=[a, b])
    assert exc.value.code == "one_original"
    with pytest.raises(InvoicingError):
        services.create_sales_document(w.alm, user=w.clerk, customer=w.customer, issue_date=date(2027, 1, 10),
                                       doc_type="credit_note")


def test_r18_note_needs_a_final_original(w):
    pending = approve(w, invoice(w))
    with pytest.raises(InvoicingError) as exc:
        note(w, pending, lines=[("rent_commercial", "1", "100")])
    assert "not final" in messages(exc)


def test_at64_late_note_warns_but_issues(w):
    original = final(w, invoice(w, issue_date=date(2027, 3, 1)))
    on_time = approve(w, note(w, original, lines=[("rent_commercial", "1", "100")], issue_date=date(2027, 4, 15),
                              adjustment_event_date=date(2027, 3, 2)))
    late = approve(w, note(w, original, lines=[("rent_commercial", "1", "100")], issue_date=date(2027, 4, 16),
                           adjustment_event_date=date(2027, 3, 2)))
    assert on_time.warnings == [] and on_time.status == "issued"
    assert late.status == "issued" and "2027-04-15" in late.warnings[0] and "الخامس عشر" in late.warnings[0]


# --- money and currency (R56, R74; AT-46, AT-47, AT-68) ---------------------------------------------

def test_at46_float_input_rejected(w):
    doc = services.create_sales_document(w.alm, user=w.clerk, customer=w.customer, issue_date=date(2027, 1, 10))
    with pytest.raises(InvoicingError) as exc:
        services.add_sales_line(doc, user=w.clerk, income_type="rent_commercial", quantity="1", unit_price=0.1,
                                description_ar="بند")
    assert exc.value.code == "float_rejected"
    line = SalesDocumentLine(entity=w.alm, document=doc, line_no=1, quantity=D("1"), unit_price=0.1,
                             income_type_id="1", vat_category="S", income_account_id=1)
    with pytest.raises(InvoicingError):
        line.save()


def test_at47_usd_invoice_with_sar_vat_totals(w):
    usd_rate(w, "3.75")
    done = final(w, invoice(w, [("rent_commercial", "1", "1000.00")], currency="USD"))
    assert (done.total_net, done.total_vat, done.total_gross) == (D("1000.00"), D("150.00"), D("1150.00"))
    assert (done.total_vat_sar, done.fx_rate, done.vat_breakdown[0]["vat_sar"]) == (D("562.50"), D("3.75"), "562.50")
    vat_line = done.journal_entry.lines.get(account__code="2200")
    assert (vat_line.credit, vat_line.currency_id, vat_line.txn_amount) == (D("562.50"), "USD", D("150.000"))
    assert done.journal_entry.lines.get(account__code="1200").debit == D("4312.50")


def test_at47_foreign_invoice_without_a_rate_rejected(w):
    doc = invoice(w, currency="EUR")
    with pytest.raises(InvoicingError) as exc:
        approve(w, doc)
    assert exc.value.code == "rate_required" and "rate is required" in messages(exc) and "سعر الصرف" in messages(exc)
    assert SalesDocument.objects.get(pk=doc.pk).number == ""


def test_at68_three_decimal_currency_rejected(w):
    with pytest.raises(InvoicingError) as exc:
        services.create_sales_document(w.alm, user=w.clerk, customer=w.customer, issue_date=date(2027, 1, 10),
                                       currency="KWD")
    assert exc.value.code == "currency_decimals" and "OQ-48" in messages(exc) and "غير مدعومة" in messages(exc)
    with pytest.raises(InvoicingError):
        services.create_sales_document(w.alm, user=w.clerk, customer=w.customer, issue_date=date(2027, 1, 10),
                                       currency="JPY")


def test_oq18_arabic_line_description_and_notes_required(w):
    doc = services.create_sales_document(w.alm, user=w.clerk, customer=w.customer, issue_date=date(2027, 1, 10),
                                         supply_date=date(2027, 1, 10), notes_en="English note only")
    with pytest.raises(InvoicingError) as exc:
        services.add_sales_line(doc, user=w.clerk, income_type="rent_commercial", quantity="1", unit_price="10",
                                description_ar="", description_en="English only")
    assert exc.value.code == "description_ar"
    services.add_sales_line(doc, user=w.clerk, income_type="rent_commercial", quantity="1", unit_price="10",
                            description_ar="بند")
    with pytest.raises(InvoicingError) as exc2:
        services.submit_sales_document(doc, user=w.clerk)
    assert "Arabic text" in messages(exc2)


def test_r47_titles(w):
    doc = invoice(w)
    assert (doc.title_en, doc.title_ar, doc.zatca_type_code) == ("Tax Invoice", "فاتورة ضريبية", "388")


def test_r52_retention_dates(w):
    done = final(w, invoice(w, issue_date=date(2027, 2, 10)))  # quarterly: tax period ends 2027-03-31
    assert (done.retention_class, done.retention_until) == ("capital_asset_real_estate", date(2042, 3, 31))
    fee = final(w, invoice(w, [("management_fee", "1", "100")], issue_date=date(2027, 2, 10)))
    assert (fee.retention_class, fee.retention_until) == ("standard", date(2033, 3, 31))


# --- agents (D8, R22) ------------------------------------------------------------------------------

@pytest.fixture
def agent(w):
    role = Role.objects.create(code="agent_sales_drafter", name_en="Agent: sales drafter", name_ar="وكيل المسودات",
                               scope="entity", kind="agent")
    for codename in ("invoicing.create_sales_document", "invoicing.create_bill", "invoicing.view"):
        RolePermission.objects.create(role=role, codename=codename)
    user = make_agent("drafter")
    Membership.objects.create(user=user, entity=w.alm, role=role, valid_from=access.services.now()
                              - __import__("datetime").timedelta(hours=1), granted_by=w.cfo, reason="pilot")
    return user


def test_agent_can_only_draft_sales_documents(w, agent):
    from core.access.models import AccessLog

    doc = services.create_sales_document(w.alm, user=agent, customer=w.customer, issue_date=date(2027, 1, 10),
                                         supply_date=date(2027, 1, 10))
    assert doc.source == "agent:drafter"
    assert AccessLog.objects.filter(actor=agent, details__action="create_draft").exists()
    services.add_sales_line(doc, user=agent, income_type="rent_commercial", quantity="1", unit_price="100",
                            description_ar="بند")
    with pytest.raises(access.AccessDenied):
        services.submit_sales_document(doc, user=agent)
    services.submit_sales_document(doc, user=w.clerk)  # a human submits it
    with pytest.raises(access.AccessDenied):
        approve(w, doc, agent)
    assert approve(w, doc).status == "issued"


def test_r57_approver_who_returns_a_document_may_still_approve_it(w):
    doc = invoice(w)
    services.return_to_draft(doc, user=w.cfo)
    services.submit_sales_document(doc, user=w.clerk)
    assert approve(w, doc).approved_by == w.cfo


def test_r41_failed_posting_on_clearance_keeps_the_result_and_can_be_retried(w):
    from ledger.models import Account

    issued = approve(w, invoice(w))
    Account.objects.filter(entity=w.alm, code="4100").update(is_active=False)
    with pytest.raises(LedgerError):
        w.zatca.clear(issued)
    stored = SalesDocument.objects.get(pk=issued.pk)
    assert (stored.status, stored.zatca_status, stored.journal_entry_id) == ("issued", "cleared", None)
    Account.objects.filter(entity=w.alm, code="4100").update(is_active=True)
    done = services.post_cleared_document(stored, user=w.queue)
    assert done.status == "final" and done.journal_entry.posted_by == w.cfo


def test_r45_zatca_fields_are_written_only_by_the_hand_off(w):
    doc = services.create_sales_document(w.alm, user=w.clerk, customer=w.customer, issue_date=date(2027, 1, 10))
    doc.zatca_hash = "forged"
    with pytest.raises(InvoicingError) as exc:
        doc.save()
    assert exc.value.code == "zatca_fields"
    with pytest.raises(InvoicingError):
        SalesDocument(entity=w.alm, doc_type="invoice", customer=w.customer, issue_date=date(2027, 1, 1),
                      currency_id="SAR", created_by=w.clerk, zatca_status="cleared").save()
    with pytest.raises(InvoicingError):
        SalesDocument.objects.filter(pk=doc.pk).update(zatca_status="cleared")
