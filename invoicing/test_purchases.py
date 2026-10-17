"""Purchase documents (invoicing.md R22 to R30, R32, R58, R92; AT-12, AT-22 to AT-24, AT-26, AT-95, AT-96, AT-39, AT-40, AT-52, AT-69)."""
from datetime import date, timedelta

import pytest

from core import access
from core.access.models import Membership, Role, RolePermission
from core.access.testing import make_agent
from invoicing import services
from invoicing.conftest import D
from invoicing.models import DocumentSequence, InvoicingError, PurchaseDocument
from ledger import services as ledger
from ledger.models import FiscalPeriod, JournalEntry, LedgerError

pytestmark = pytest.mark.django_db


def bill(w, lines=(("5300", "1", "1000.00"),), *, vendor=None, number="A-17", on=date(2027, 1, 15), user=None,
         vat=None, gross=None, submit=True, **fields):
    """A bill with lines (account, qty, price[, kwargs]); vendor totals default to the computed ones."""
    user = user or w.ap
    doc = services.create_purchase_document(w.alm, user=user, vendor=vendor or w.vendor, vendor_document_number=number,
                                            vendor_document_date=on, accounting_date=on, **fields)
    for account, qty, price, *extra in lines:
        services.add_purchase_line(doc, user=user, account=account, quantity=qty, unit_price=price,
                                   description_ar="بند مشتريات", **(extra[0] if extra else {}))
    if vat is None or gross is None:
        doc.refresh_from_db()
        lines_ = list(doc.lines.select_related("vat_rate"))
        _, net, charged, _, _, _, _ = services._purchase_amounts(doc, lines_)
        vat = charged if vat is None else vat
        gross = net + charged if gross is None else gross
    services.update_purchase_document(doc, user=user, vendor_total_vat=vat, vendor_total_gross=gross)
    return services.submit_purchase_document(doc, user=user) if submit else doc


def je(doc):
    doc.refresh_from_db()
    return sorted((l.account.code, l.debit, l.credit) for l in doc.journal_entry.lines.select_related("account"))


def test_at52_creator_cannot_approve_and_e7_posts(w):
    from core.access.testing import member

    member(w.ap, w.alm, "approver")
    doc = bill(w)
    with pytest.raises(access.AccessDenied) as exc:
        services.approve_purchase_document(doc, user=w.ap)
    assert exc.value.code == "self_approval"
    posted = services.approve_purchase_document(doc, user=w.cfo)
    assert (posted.status, posted.number) == ("posted", "ALM-BILL-2027-000001")
    assert je(posted) == [("1310", D("150.00"), D("0")), ("2100", D("0"), D("1150.00")), ("5300", D("1000.00"), D("0"))]
    assert posted.journal_entry.posted_by == w.cfo and posted.journal_entry.reference == posted.number


def test_at52_closed_period_rolls_back_the_approval(w):
    FiscalPeriod.objects.filter(entity=w.alm, name="2027-01").update(status="closed")
    doc = bill(w)
    with pytest.raises(InvoicingError) as exc:
        services.approve_purchase_document(doc, user=w.cfo)
    assert "closed" in " ".join(exc.value.messages)
    stored = PurchaseDocument.objects.get(pk=doc.pk)
    assert (stored.status, stored.number) == ("submitted", "")
    assert not DocumentSequence.objects.filter(doc_type="BILL").exclude(last_number=0).exists()
    assert not JournalEntry.objects.exists()


def test_r58_ledger_refusal_rolls_back_number(w, monkeypatch):
    doc = bill(w)
    from ledger.models import Account

    Account.objects.filter(entity=w.alm, code="5300").update(is_active=False)  # the ledger refuses the posting
    with pytest.raises(InvoicingError):
        services.approve_purchase_document(doc, user=w.cfo)
    assert PurchaseDocument.objects.get(pk=doc.pk).status == "submitted"
    assert not DocumentSequence.objects.filter(doc_type="BILL").exclude(last_number=0).exists()


def test_at95_resident_vendor_without_vat_number_carries_no_vat(w):
    """D24 / R92, E8a (replaces AT-25): no input VAT, nothing to 1310 and nothing added to cost."""
    plain = services.create_vendor(w.alm, user=w.ap, vendor_kind="individual", code="V2", name_ar="مورد غير مسجل")
    doc = bill(w, vendor=plain, vat="0.00", gross="1000.00")
    posted = services.approve_purchase_document(doc, user=w.cfo)
    assert je(posted) == [("2100", D("0"), D("1000.00")), ("5300", D("1000.00"), D("0"))]
    assert (posted.total_vat, posted.total_gross, posted.reverse_charge_vat) == (D("0"), D("1000.00"), D("0"))
    line = posted.lines.get()
    assert (line.vat_category, line.vat_amount, line.vat_recoverable) == ("O", D("0"), False)
    assert posted.vat_breakdown[0]["category"] == "O" and posted.vat_breakdown[0]["vat"] == "0.00"
    assert not getattr(posted, "warnings", [])


def test_at96_unregistered_vendor_vat_refused_and_non_resident_still_reverse_charged(w):
    plain = services.create_vendor(w.alm, user=w.ap, vendor_kind="business", code="V2", name_ar="مورد غير مسجل")
    with pytest.raises(InvoicingError) as exc:  # vendor total VAT 150.00 on a bill without a VAT number
        bill(w, vendor=plain, vat="150.00", gross="1150.00")
    text = " ".join(exc.value.messages)
    assert "cannot charge VAT" in text and "غير مسجل" in text
    draft = PurchaseDocument.objects.get(vendor=plain)
    assert draft.status == "draft"
    services.update_purchase_document(draft, user=w.ap, vendor_total_vat="0", vendor_total_gross="1000.00")
    assert services.submit_purchase_document(draft, user=w.ap).status == "submitted"
    nr = foreign_vendor(w)  # no KSA VAT number by nature: E9 reverse charge stays
    posted = services.approve_purchase_document(
        bill(w, [("5300", "1", "1000", {"supply_kind": "services"})], vendor=nr, number="NR-2"), user=w.cfo)
    assert je(posted) == [("1310", D("150.00"), D("0")), ("2100", D("0"), D("1000.00")), ("2200", D("0"), D("150.00")),
                          ("5300", D("1000.00"), D("0"))]


def test_r92_vat_number_removed_from_the_document_makes_it_vat_free(w):
    """Reviewer round 6: a vendor registered on its master never gets the R92 VAT-free treatment by blanking the
    document's VAT number; only a vendor that is genuinely unregistered does."""
    doc = bill(w, submit=False)  # registered vendor: line S 15%
    assert doc.lines.get().vat_category == "S"
    services.update_purchase_document(doc, user=w.ap, vendor_vat_number="", vendor_total_vat="0",
                                      vendor_total_gross="1000.00")
    with pytest.raises(InvoicingError) as exc:
        services.submit_purchase_document(doc, user=w.ap)
    text = " ".join(exc.value.messages)
    assert exc.value.code == "document_invalid"
    assert "registered for VAT" in text and "V001" in text and "مسجل في ضريبة القيمة المضافة" in text
    doc.refresh_from_db()
    assert (doc.status, doc.lines.get().vat_category) == ("draft", "S")  # nothing turned VAT-free
    # The vendor's own number back on the document: the bill books with recoverable VAT as usual.
    services.update_purchase_document(doc, user=w.ap, vendor_vat_number="320000000000003", vendor_total_vat="150.00",
                                      vendor_total_gross="1150.00")
    posted = services.approve_purchase_document(services.submit_purchase_document(doc, user=w.ap), user=w.cfo)
    assert je(posted) == [("1310", D("150.00"), D("0")), ("2100", D("0"), D("1150.00")), ("5300", D("1000.00"), D("0"))]


@pytest.mark.parametrize("master", [{"is_vat_registered": True, "vat_number": "330000000000003"},
                                    {"is_vat_registered": False, "vat_number": "330000000000003"}])
def test_r92_registered_vendor_blank_document_number_refused_at_submit(w, master):
    """Probe: registered vendor, ``vendor_vat_number=""`` on create, VAT 0 → booked VAT-free as category O."""
    vendor = services.create_vendor(w.alm, user=w.ap, vendor_kind="business", code="V9", name_ar="مورد مسجل", **master)
    doc = services.create_purchase_document(w.alm, user=w.ap, vendor=vendor, vendor_document_number="X1",
                                            vendor_document_date=date(2027, 1, 15), accounting_date=date(2027, 1, 15),
                                            vendor_vat_number="")
    services.add_purchase_line(doc, user=w.ap, account="5300", quantity="1", unit_price="1150.00",
                               description_ar="بند", vat_category="S")
    assert doc.lines.get().vat_category == "S"  # not forced to O when the line is added
    services.update_purchase_document(doc, user=w.ap, vendor_total_vat="0", vendor_total_gross="1150.00")
    with pytest.raises(InvoicingError) as exc:
        services.submit_purchase_document(doc, user=w.ap)
    assert "registered for VAT" in " ".join(exc.value.messages)
    doc.refresh_from_db()
    assert doc.status == "draft" and doc.journal_entry_id is None


def test_r92_genuinely_unregistered_vendor_document_number_removed_is_vat_free(w):
    plain = services.create_vendor(w.alm, user=w.ap, vendor_kind="business", code="V2", name_ar="مورد غير مسجل")
    doc = bill(w, vendor=plain, submit=False, vendor_vat_number="310000000000003")  # number typed by mistake
    assert doc.lines.get().vat_category == "S"
    services.update_purchase_document(doc, user=w.ap, vendor_vat_number="", vendor_total_vat="0",
                                      vendor_total_gross="1000.00")
    posted = services.approve_purchase_document(services.submit_purchase_document(doc, user=w.ap), user=w.cfo)
    assert je(posted) == [("2100", D("0"), D("1000.00")), ("5300", D("1000.00"), D("0"))]
    assert posted.lines.get().vat_category == "O" and posted.total_vat == D("0")


def test_at39_attribution_taxable_exempt_mixed(w):
    lines = [("5300", "1", "1000", {"vat_attribution": "taxable"}), ("5200", "1", "1000", {"vat_attribution": "exempt"}),
             ("5900", "1", "1000", {"vat_attribution": "mixed"})]
    posted = services.approve_purchase_document(bill(w, lines), user=w.cfo)
    assert je(posted) == [("1310", D("150.00"), D("0")), ("2100", D("0"), D("3450.00")),
                          ("5200", D("1150.00"), D("0")), ("5300", D("1000.00"), D("0")), ("5900", D("1150.00"), D("0"))]
    assert [l.vat_recoverable for l in posted.lines.order_by("line_no")] == [True, False, False]


def foreign_vendor(w):
    return services.create_vendor(w.alm, user=w.ap, vendor_kind="business", code="NR1", name_ar="مورد أجنبي", name_en="Foreign Co",
                                   is_resident=False, residence_country="GB")


def test_at26_at40_reverse_charge_on_imported_services_only(w):
    nr = foreign_vendor(w)
    with pytest.raises(InvoicingError) as exc:  # R30: goods or services must be chosen
        bill(w, [("5300", "1", "10000")], vendor=nr)
    assert "Goods or services" in " ".join(exc.value.messages)
    lines = [("5300", "1", "10000", {"supply_kind": "services"}), ("1500", "1", "5000", {"supply_kind": "goods"})]
    posted = services.approve_purchase_document(bill(w, lines, vendor=nr, number="NR-1"), user=w.cfo)
    assert (posted.total_vat, posted.reverse_charge_vat, posted.total_gross) == (D("0.00"), D("1500.00"), D("15000.00"))
    assert je(posted) == [("1310", D("1500.00"), D("0")), ("1500", D("5000.00"), D("0")), ("2100", D("0"), D("15000.00")),
                          ("2200", D("0"), D("1500.00")), ("5300", D("10000.00"), D("0"))]


def test_r30_reverse_charge_exempt_attribution_goes_to_cost(w):
    nr = foreign_vendor(w)
    posted = services.approve_purchase_document(
        bill(w, [("5300", "1", "10000", {"supply_kind": "services", "vat_attribution": "exempt"})], vendor=nr),
        user=w.cfo)
    assert je(posted) == [("2100", D("0"), D("10000.00")), ("2200", D("0"), D("1500.00")), ("5300", D("11500.00"), D("0"))]


def test_at12_sharia_prohibited_bill_line(w):
    with pytest.raises(InvoicingError) as exc:
        bill(w, [("5610", "1", "100")])
    assert "Sharia-prohibited" in " ".join(exc.value.messages)
    from core.access.testing import make_user, member
    from invoicing.conftest import make_entity

    free = make_entity("FREE", "full", w.alm, vat="377777777700003", cr="1010000007", sharia_enforced=False)
    ap, cfo = make_user("free_ap"), make_user("free_cfo")
    member(ap, free, "payables_clerk")
    member(cfo, free, "approver")
    vendor = services.create_vendor(free, user=ap, vendor_kind="business", code="B1", name_ar="بنك تقليدي")
    doc = services.create_purchase_document(free, user=ap, vendor=vendor, vendor_document_number="I-1",
                                            vendor_document_date=date(2027, 1, 15), accounting_date=date(2027, 1, 15))
    services.add_purchase_line(doc, user=ap, account="5610", quantity="1", unit_price="100", description_ar="فوائد",
                               vat_category="E")
    services.update_purchase_document(doc, user=ap, vendor_total_vat="0", vendor_total_gross="100.00")
    services.submit_purchase_document(doc, user=ap)
    assert services.approve_purchase_document(doc, user=cfo).status == "posted"


def test_oq11_totals_must_match_the_vendor_document(w):
    with pytest.raises(InvoicingError) as exc:
        bill(w, vat="150.01", gross="1150.01")
    assert "differ from the vendor document" in " ".join(exc.value.messages)


def test_at23_duplicate_vendor_document_number(w):
    first = services.approve_purchase_document(bill(w), user=w.cfo)
    with pytest.raises(InvoicingError) as exc:
        bill(w)
    assert exc.value.code == "duplicate_vendor_document"
    other_vendor = services.create_vendor(w.alm, user=w.ap, vendor_kind="business", code="V3", name_ar="مورد آخر",
                                          is_vat_registered=True, vat_number="330000000000003")
    assert bill(w, vendor=other_vendor, vat="150.00", gross="1150.00").status == "submitted"  # another vendor may
    services.reverse_document(first, user=w.accountant)
    assert bill(w).status == "submitted"  # allowed after the first is reversed


def test_r23_database_guard_on_duplicates(w):
    from django.db import IntegrityError, transaction

    doc = bill(w)
    copy = PurchaseDocument(entity=w.alm, vendor=w.vendor, vendor_document_number="A-17",
                            vendor_document_date=doc.vendor_document_date, accounting_date=doc.accounting_date,
                            currency_id="SAR", created_by=w.ap)
    with pytest.raises(IntegrityError), transaction.atomic():
        copy.save()


def test_at24_reversal_into_the_next_open_period(w):
    posted = services.approve_purchase_document(bill(w, on=date(2027, 2, 10)), user=w.cfo)
    FiscalPeriod.objects.filter(entity=w.alm, name="2027-02").update(status="closed")
    reversed_ = services.reverse_document(posted, user=w.accountant, reason="Entered twice")
    assert reversed_.status == "reversed" and reversed_.reversal_entry.date == date(2027, 3, 1)
    tb = {r["code"]: r for r in ledger.trial_balance(w.alm, date(2027, 12, 31), user=w.cfo)}
    assert "2100" not in tb and "5300" not in tb  # net to zero


def test_at24_bill_with_allocation_cannot_be_reversed(w):
    posted = services.approve_purchase_document(bill(w), user=w.cfo)
    payment = services.create_vendor_payment(w.alm, user=w.ap, vendor=w.vendor, date=date(2027, 1, 20), amount="1150.00")
    services.add_payment_application(payment, posted, "1150.00", user=w.ap)
    services.submit_vendor_payment(payment, user=w.ap)
    services.approve_vendor_payment(payment, user=w.cfo)
    with pytest.raises(InvoicingError) as exc:
        services.reverse_document(posted, user=w.accountant)
    assert exc.value.code == "has_allocations"


def test_at69_one_booking_per_document(w):
    posted = services.approve_purchase_document(bill(w), user=w.cfo)
    services.reverse_document(posted, user=w.accountant)
    grant = access.require(w.cfo, w.alm, "invoicing.approve_bill", document=posted)
    with pytest.raises(LedgerError) as exc:  # re-booking a reversed document
        with services._posting_as(w.cfo):
            ledger.create_entry(w.alm, posted.accounting_date, "again", None, user=w.cfo, post=True, grant=grant,
                                document=posted)
    assert exc.value.code == "document_booked"
    with pytest.raises(access.AccessDenied):  # the reversal grant never books an entry
        ledger.create_entry(w.alm, posted.accounting_date, "x", None, user=w.accountant, post=True, document=posted,
                            grant=access.require(w.accountant, w.alm, "invoicing.reverse_document", document=posted))
    corrected = services.approve_purchase_document(bill(w, number="A-17"), user=w.cfo)
    assert corrected.number == "ALM-BILL-2027-000002" and corrected.journal_entry.status == "posted"


def test_vendor_credit_note_references_a_posted_bill_and_mirrors_it(w):
    with pytest.raises(InvoicingError) as exc:
        bill(w, doc_type="vendor_credit_note", number="CN-1")
    assert "references its bill" in " ".join(exc.value.messages)
    original = services.approve_purchase_document(bill(w), user=w.cfo)
    vcn = services.approve_purchase_document(
        bill(w, [("5300", "1", "400")], number="CN-1b", doc_type="vendor_credit_note", original_document=original),
        user=w.cfo)
    assert vcn.number == "ALM-VCN-2027-000001"
    assert je(vcn) == [("1310", D("0"), D("60.00")), ("2100", D("460.00"), D("0")), ("5300", D("0"), D("400.00"))]
    assert services.open_amount(original, user=w.ap) == D("690.00")
    with pytest.raises(InvoicingError):  # cannot credit more than the bill
        bill(w, [("5300", "1", "700")], number="CN-2", doc_type="vendor_credit_note", original_document=original)


def test_at22_agent_bill_stays_draft(w):
    role = Role.objects.create(code="agent_invoice_reader", name_en="Agent: invoice reader", name_ar="وكيل قراءة الفواتير",
                               scope="entity", kind="agent")
    for codename in ("invoicing.create_bill", "invoicing.view"):
        RolePermission.objects.create(role=role, codename=codename)
    agent = make_agent()
    Membership.objects.create(user=agent, entity=w.alm, role=role, valid_from=access.services.now() - timedelta(hours=1),
                              granted_by=w.cfo, reason="pilot")
    doc = bill(w, user=agent, submit=False)
    assert doc.source == "agent:invoice_reader" and doc.status == "draft"
    with pytest.raises(access.AccessDenied):
        services.submit_purchase_document(doc, user=agent)
    with pytest.raises(access.AccessDenied):
        services.approve_purchase_document(doc, user=agent)
    assert PurchaseDocument.objects.get(pk=doc.pk).status == "draft"
    services.submit_purchase_document(doc, user=w.ap)  # a human submits it (R22)
    assert services.approve_purchase_document(doc, user=w.cfo).status == "posted"


def test_r46_purchase_documents_never_go_to_zatca(w):
    services.approve_purchase_document(bill(w), user=w.cfo)
    assert w.zatca.submitted == []


def test_intercompany_bill_uses_2110(w):
    group_vendor = services.create_vendor(w.alm, user=w.ap, vendor_kind="business", code="G-SUB", name_ar="التابعة", group_entity=w.sub,
                                          is_vat_registered=True, vat_number="311111111100003")
    posted = services.approve_purchase_document(bill(w, vendor=group_vendor), user=w.cfo)
    line = posted.journal_entry.lines.get(account__code="2110")
    assert (line.credit, line.counterparty_entity) == (D("1150.00"), w.sub) and posted.is_intercompany
