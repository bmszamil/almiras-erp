"""Intercompany billing and reconciliation (billing R3, R4, R26, R27, R50, R63, R64, R69; AT-11, AT-32, AT-44,
AT-50)."""
from datetime import date
from decimal import Decimal

import pytest

from billing import reports, services
from billing.conftest import approve_note, approved, clear, fake_cfo_approval, period, run, version
from billing.models import BillingAdjustment
from core import access
from core.access.testing import make_user, member
from invoicing import services as inv
from invoicing.conftest import ADDRESS
from invoicing.models import PurchaseDocument, SalesDocument
from ledger import services as ledger

D = Decimal
IC_RENT = dict(customer=None, lines=(("rent_commercial", "5000.00"),))


def _lines(doc):
    return sorted((ln.account.code, ln.debit, ln.credit, getattr(ln.counterparty_entity, "code", None))
                  for ln in doc.journal_entry.lines.all())


def _nothing_in_sub(w):
    assert not SalesDocument.objects.filter(entity=w.sub).exists()
    assert not PurchaseDocument.objects.filter(entity=w.sub).exists()


@fake_cfo_approval
def test_at11_intercompany_invoice_carries_the_counterparty(w):
    c = approved(w, customer=w.ic_customer, lines=(("rent_commercial", "5000.00"),))
    assert c.counterparty_entity == w.sub and c.lines.get().income_account.code == "4110"
    run(w, date(2027, 1, 1))
    doc = clear(w, period(c, date(2027, 1, 1)).sales_document)
    assert (doc.doc_type, doc.zatca_profile, doc.is_intercompany, doc.buyer_vat_number) == (
        "invoice", "standard", True, "311111111100003")
    assert _lines(doc) == [("1210", D("5750.00"), D("0.00"), "SUB"), ("2200", D("0.00"), D("750.00"), None),
                           ("4110", D("0.00"), D("5000.00"), None)]
    _nothing_in_sub(w)  # no mirror bill (D10, R4)


def test_at32_retroactive_intercompany_cancellation_credits_1210(w):
    c = approved(w, customer=w.ic_customer, lines=(("rent_commercial", "5000.00"),))
    run(w, date(2027, 1, 1))
    clear(w, period(c, date(2027, 1, 1)).sales_document)
    w.clock.set(date(2027, 2, 10))
    v = version(w, c, kind="cancellation", effective=date(2027, 1, 1), reason_ar="ألغي العقد بين الشركتين")
    services.approve_version(v, user=w.c)
    (adj,) = BillingAdjustment.objects.filter(contract=c, billing_period__period_start=date(2027, 1, 1))
    final = approve_note(w, adj.document, day=date(2027, 2, 10))
    assert _lines(final) == [("1210", D("0.00"), D("5750.00"), "SUB"), ("2200", D("750.00"), D("0.00"), None),
                             ("4110", D("5000.00"), D("0.00"), None)]
    _nothing_in_sub(w)


@fake_cfo_approval
def test_at50_intercompany_ip_licence_on_4310(w):
    c = approved(w, customer=w.ic_customer, contract_type="service", lease_use="", property_ref="", unit_ref="",
                 lines=(("ip_licence", "20000.00"),))
    run(w, date(2027, 1, 1))
    doc = clear(w, period(c, date(2027, 1, 1)).sales_document)
    assert _lines(doc) == [("1210", D("23000.00"), D("0.00"), "SUB"), ("2200", D("0.00"), D("3000.00"), None),
                           ("4310", D("0.00"), D("20000.00"), None)]  # B9
    _nothing_in_sub(w)


# --- AT-44, R64: reconciliation ---------------------------------------------------------------------------

@pytest.fixture
def ic_books(w):
    """ALM bills SUB 11,500.00 SAR and USD 1,150.00 (both final); SUB books a vendor bill for the SAR one (B10)."""
    treasurer = make_user("treasury")
    member(treasurer, w.alm, "group_treasury")
    ledger.create_rate(user=treasurer, from_currency="USD", to_currency="SAR", on=date(2027, 3, 1), rate=D("3.75"),
                       group=w.alm)
    sar = approved(w, customer=w.ic_customer, contract_type="service", lease_use="", property_ref="", unit_ref="",
                   start=date(2027, 3, 1), lines=(("management_fee", "10000.00"),))
    usd = approved(w, customer=w.ic_customer, contract_type="service", lease_use="", property_ref="", unit_ref="",
                   start=date(2027, 3, 1), currency="USD", lines=(("management_fee", "1000.00"),))
    run(w, date(2027, 3, 1))
    sar_doc = clear(w, period(sar, date(2027, 3, 1)).sales_document)
    clear(w, period(usd, date(2027, 3, 1)).sales_document)
    payables = make_user("sub_ap")
    member(payables, w.sub, "payables_clerk")
    vendor = inv.create_vendor(w.sub, user=payables, vendor_kind="business", code="V-ALM", name_ar="شركة ALM",
                               is_vat_registered=True, vat_number="300000000000003", group_entity=w.alm, **ADDRESS)
    bill = inv.create_purchase_document(w.sub, user=payables, vendor=vendor, vendor_document_number=sar_doc.number,
                                        vendor_document_date=date(2027, 3, 1), accounting_date=date(2027, 3, 5),
                                        vendor_total_vat="1500.00", vendor_total_gross="11500.00")
    inv.add_purchase_line(bill, user=payables, account="5300", quantity="1", unit_price="10000.00",
                          description_ar="أتعاب إدارة من ALM")
    inv.submit_purchase_document(bill, user=payables)
    inv.approve_purchase_document(bill, user=w.c2)
    return sar, usd


@fake_cfo_approval
def test_at44_reconciliation_by_pair_and_currency(w, ic_books):
    member(w.c, w.alm, "group_viewer")  # the CFO also sees the group (ledger.view on SUB through the group role)
    rows = services.intercompany_reconciliation(date(2027, 3, 31), user=w.c, group=w.alm)
    by_currency = {r["currency"]: r for r in rows}
    sar, usd = by_currency["SAR"], by_currency["USD"]
    assert (sar["seller"], sar["buyer"]) == ("ALM", "SUB")
    assert (sar["seller_position"], sar["buyer_position"], sar["difference"]) == (D("11500.00"), D("-11500.00"),
                                                                                  D("0.00"))
    assert (usd["seller_position"], usd["buyer_position"], usd["difference"]) == (D("1150.00"), D("0"), D("1150.00"))
    assert usd["billing_open"] == D("1150.00") and usd["billing_unposted"] == D("0")
    assert sar["billing_open"] == D("11500.00")
    assert sar["buyer_billing_open"] is None  # C has no billing.view in SUB

    viewer_rows = services.intercompany_reconciliation(date(2027, 3, 31), user=w.v, group=w.alm)
    for r in viewer_rows:
        assert r["buyer_position"] is None and r["difference"] is None
        assert (r["buyer_note_en"], r["buyer_note_ar"]) == ("not visible", "غير ظاهر")
    assert {r["currency"]: r["seller_position"] for r in viewer_rows} == {"SAR": D("11500.00"), "USD": D("1150.00")}
    with pytest.raises(access.AccessDenied):  # ledger.view on the group parent is required
        services.intercompany_reconciliation(date(2027, 3, 31), user=w.sub_s, group=w.alm)

    csv_en = reports.reconciliation_csv(viewer_rows, language="en")
    csv_ar = reports.reconciliation_csv(viewer_rows, language="ar")
    assert "Seller position" in csv_en.splitlines()[0] and "not visible" in csv_en
    assert "مركز البائع" in csv_ar.splitlines()[0] and "غير ظاهر" in csv_ar
    assert "-11500.00" not in csv_en  # never the hidden side's amount


def test_r64_unposted_billing_documents(w):
    c = approved(w, customer=w.ic_customer, lines=(("rent_commercial", "5000.00"),))
    run(w, date(2027, 1, 1))  # issued, ZATCA pending: booked in neither entity
    member(w.c, w.alm, "group_viewer")
    (row,) = services.intercompany_reconciliation(date(2027, 1, 31), user=w.c, group=w.alm)
    assert (row["seller_position"], row["billing_unposted"], row["billing_open"]) == (D("0"), D("5750.00"), D("0"))
    assert period(c, date(2027, 1, 1)).sales_document.status == "issued"
