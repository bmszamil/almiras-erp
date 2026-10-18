"""B2G supplies under VAT IR Art. 20(5) (zatca R-54, AT-81 to AT-83; SDK R4.0.0 findings 2026-10-17 §7.3): buyer
KSA-40 ``CompanyLegalForm`` = ``GOV`` and BT-7 ``TaxPointDate`` = the payment order date on standard documents to a
government customer; local BR-KSA-91 / -92; KSA-2 stays at 7 positions (the R3.4.8 9-position form is refused)."""
from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from invoicing import services as invoicing_services
from invoicing.conftest import ADDRESS, approve, invoice
from zatca import rules, services, ubl
from zatca.conftest import TODAY, einvoice_of, held_codes, onboard, run, xml_text
from zatca.models import EgsUnit

pytestmark = pytest.mark.django_db
PAYMENT_ORDER = date(2026, 10, 5)


# --- pure XML and rules ----------------------------------------------------------------------------------------

def _data(category="standard", legal_form="", tax_point_date=None, subtype=None):
    seller = ubl.Party(name="شركة البائع", vat_number="311111111100003", id_scheme="CRN", id_value="1010000001",
                       street="شارع", building_no="1234", district="حي", city="جدة", postal_code="12345", country="SA")
    buyer = ubl.Party(name="جهة حكومية", vat_number="310000000000003", street="شارع", building_no="2345",
                      district="حي", city="الرياض", postal_code="23456", country="SA", legal_form=legal_form)
    if category == "simplified":
        buyer = ubl.Party(legal_form=legal_form)
    from datetime import datetime, timezone

    return ubl.InvoiceData(
        number="INV-1", uuid="8e6000cf-1a98-4174-b3e7-b5d5954bc10d",
        issue=datetime(2026, 10, 15, 7, 0, tzinfo=timezone.utc), type_code="388", category=category,
        subtype_code=subtype or ubl.subtype_code(category, {}), currency="SAR", currency_places=2, seller=seller,
        buyer=buyer, lines=[ubl.Line(1, "خدمة", D("1"), "PCE", D("100.00"), D("0"), D("100.00"), D("15.00"), "S",
                                     D("15.00"))],
        groups=[ubl.TaxGroup("S", D("15.00"), D("100.00"), D("15.00"))], total_net=D("100.00"),
        total_vat=D("15.00"), total_gross=D("115.00"), total_vat_sar=D("15.00"), supply_date=date(2026, 10, 1),
        payment_means_code="10", tax_point_date=tax_point_date)


def _children(xml, xpath):
    from lxml import etree

    from zatca import crypto

    root = etree.fromstring(xml, crypto._parser())
    return [etree.QName(el).localname for el in root.xpath(xpath, namespaces=crypto.NS)[0]]


def test_at81_gov_and_tax_point_date_emitted_in_ubl_order():
    xml = ubl.build_unsigned(_data(legal_form="GOV", tax_point_date=PAYMENT_ORDER), 1, "x")
    assert xml_text(xml, "/*/cac:AccountingCustomerParty/cac:Party/cac:PartyLegalEntity/cbc:CompanyLegalForm") == "GOV"
    assert xml_text(xml, "/*/cbc:TaxPointDate") == "2026-10-05"
    head = _children(xml, "/*")
    assert head.index("InvoiceTypeCode") < head.index("TaxPointDate") == head.index("DocumentCurrencyCode") - 1
    assert _children(xml, "/*/cac:AccountingCustomerParty/cac:Party/cac:PartyLegalEntity") == [
        "RegistrationName", "CompanyLegalForm"]
    assert xml_text(xml, "/*/cbc:InvoiceTypeCode/@name") == "0100000"  # KSA-2 stays 7 positions


def test_at81_nothing_emitted_without_b2g():
    xml = ubl.build_unsigned(_data(), 1, "x")
    assert b"CompanyLegalForm" not in xml and b"TaxPointDate" not in xml


def test_at81_never_emitted_on_simplified_documents():
    xml = ubl.build_unsigned(_data("simplified", legal_form="GOV", tax_point_date=PAYMENT_ORDER), 1, "x")
    assert b"CompanyLegalForm" not in xml and b"TaxPointDate" not in xml  # BR-KSA-91/-92 apply to 01 only


def _codes(data):
    return {p[0] for p in rules.document_problems(data)}


def test_at82_br_ksa_92_gov_needs_the_payment_order_date():
    assert "tax_point_date" in _codes(_data(legal_form="GOV"))
    assert not {"tax_point_date", "buyer_legal_form"} & _codes(_data(legal_form="GOV", tax_point_date=PAYMENT_ORDER))


def test_at82_br_ksa_91_legal_form_is_only_ever_gov():
    assert "buyer_legal_form" in _codes(_data(legal_form="LLC", tax_point_date=PAYMENT_ORDER))
    assert "buyer_legal_form" in _codes(_data(legal_form="gov", tax_point_date=PAYMENT_ORDER))


def test_at82_messages_are_bilingual():
    problems = {p[0]: p for p in rules.document_problems(_data(legal_form="GOV"))}
    _, en, ar = problems["tax_point_date"]
    assert "payment order date" in en and "BR-KSA-92" in en and "أمر الدفع" in ar


@pytest.mark.parametrize("subtype", ["010000001", "01000000", "0100000G", "010000"])
def test_at83_ksa2_other_than_7_positions_refused(subtype):
    assert "subtype" in _codes(_data(subtype=subtype))  # R3.4.8's NNPNESBCG fails BR-KSA-06 in R4.0.0


def test_at83_subtype_code_always_7_positions():
    flags = ("third_party", "nominal", "export", "summary", "self_billed")
    for category in ("standard", "simplified"):
        assert len(ubl.subtype_code(category, {f: True for f in flags})) == 7
        assert len(ubl.subtype_code(category, {"b2g": True, "continuous": True})) == 7  # no positions 8, 9


# --- through the services --------------------------------------------------------------------------------------

@pytest.fixture
def live(zw):
    zw.egs1 = onboard(zw, zw.tst1, zw.admin1)
    zw.gov = invoicing_services.create_customer(
        zw.tst1, user=zw.clerk, code="G001", customer_kind="government", name_en="Test Ministry",
        name_ar="وزارة الاختبار", is_vat_registered=True, vat_number="310000000000003", **ADDRESS)
    return zw


def _issue(zw, customer):
    w = SimpleNamespace(alm=zw.tst1, clerk=zw.clerk, customer=customer, cfo=zw.cfo)
    doc = invoice(w, (("management_fee", "1", "100.00"),), customer=customer, user=zw.clerk, issue_date=TODAY,
                  entity=zw.tst1)
    return approve(w, doc, zw.cfo)


def test_at82_government_buyer_without_payment_order_date_is_held(live):
    """Invoicing does not hand over a payment order date yet (OQ-37): the document is held, nothing guessed."""
    doc = _issue(live, live.gov)
    run(live)
    einv = einvoice_of(doc)
    assert einv.status == "pending" and einv.icv is None and "tax_point_date" in held_codes(einv)
    assert EgsUnit.objects.get(pk=live.egs1.pk).last_icv == 0
    message = einv.messages.get(code="tax_point_date")
    assert "payment order date" in message.message_en and "أمر الدفع" in message.message_ar


def test_at81_government_buyer_with_payment_order_date_is_cleared(live, monkeypatch):
    monkeypatch.setattr(services, "_payment_order_date", lambda doc: PAYMENT_ORDER)
    doc = _issue(live, live.gov)
    run(live)
    einv = einvoice_of(doc)
    assert einv.status == "cleared", list(einv.messages.values_list("code", "message_en"))
    xml = einv.signed_xml.encode()
    assert xml_text(xml, "/*/cac:AccountingCustomerParty/cac:Party/cac:PartyLegalEntity/cbc:CompanyLegalForm") == "GOV"
    assert xml_text(xml, "/*/cbc:TaxPointDate") == PAYMENT_ORDER.isoformat()
    assert xml_text(xml, "/*/cbc:InvoiceTypeCode/@name") == "0100000"


def test_at81_business_buyer_gets_neither(live, monkeypatch):
    monkeypatch.setattr(services, "_payment_order_date", lambda doc: PAYMENT_ORDER)  # ignored: not B2G
    doc = _issue(live, live.customers["TST1"])
    run(live)
    einv = einvoice_of(doc)
    assert einv.status == "cleared"
    assert "CompanyLegalForm" not in einv.signed_xml and "TaxPointDate" not in einv.signed_xml


def test_at81_payment_order_date_hook_is_none_until_invoicing_has_it(live):
    doc = _issue(live, live.customers["TST1"])
    assert services._payment_order_date(doc) is None
    assert services._buyer_legal_form(doc, "standard") == ""
    live.gov.refresh_from_db()
    doc.customer = live.gov
    assert services._buyer_legal_form(doc, "standard") == "GOV"
    assert services._buyer_legal_form(doc, "simplified") == ""
