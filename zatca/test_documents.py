"""Documents: stamping, chain, signature, QR, XML content and local checks (R-2, R-11 to R-19, R-31, R-33, R-38,
R-40, R-42, R-46, R-48, R-51; AT-10, AT-11, AT-14 to AT-16, AT-25 to AT-28, AT-33, AT-40, AT-42, AT-44 to AT-46,
AT-49, AT-51, AT-53 to AT-56, AT-58 to AT-60, AT-63 to AT-67, AT-72)."""
import base64
import hashlib
from datetime import date, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from django.db import transaction

from core.access.testing import make_user, member
from invoicing import services as invoicing_services
from invoicing.conftest import ADDRESS, approve, invoice
from invoicing.models import SalesDocument
from ledger.models import JournalEntry
from zatca import crypto, qr, services, ubl
from zatca.conftest import NOW, TODAY, einvoice_of, issue, mutate, onboard, run
from zatca.messages import ZatcaError
from zatca.models import EgsUnit, EInvoice, SubmissionAttempt, ValidationMessage
from zatca.testing import FakeZatcaClient, zatca_error

pytestmark = pytest.mark.django_db
D = __import__("decimal").Decimal


@pytest.fixture
def live(zw):
    zw.egs1 = onboard(zw, zw.tst1, zw.admin1)
    return zw


def stamped(zw, entity=None, **kw):
    entity = entity or zw.tst1
    doc = issue(zw, entity, **kw)
    einv = einvoice_of(doc)
    services.stamp_document(einv, user=zw.queue, now=NOW)
    einv.refresh_from_db()
    return doc, einv


def xml_of(einv):
    return einv.signed_xml.encode("utf-8")


def text(xml, xpath):
    return ubl.find_text(xml, xpath)


def last_icv(zw):
    return EgsUnit.objects.get(pk=zw.egs1.pk).last_icv


def held_codes(einv):
    return set(einv.messages.values_list("code", flat=True))


# --- R-11, R-12: ICV and PIH chain ----------------------------------------------------------------------------

def test_at10_at11_first_document_uses_the_literal_pih_then_chains(live):
    _, a = stamped(live)
    assert a.icv == 1 and a.pih == crypto.INITIAL_PIH and len(a.pih) == 88
    _, b = stamped(live, simplified=True)  # one sequence covers standard and simplified (DTG p.48)
    assert b.icv == 2 and b.pih == a.invoice_hash
    egs = EgsUnit.objects.get(pk=live.egs1.pk)
    assert egs.last_icv == 2 and egs.last_invoice_hash == b.invoice_hash
    assert text(xml_of(b), "/*/cac:AdditionalDocumentReference[cbc:ID='PIH']/cac:Attachment/"
                           "cbc:EmbeddedDocumentBinaryObject") == a.invoice_hash
    assert text(xml_of(b), "/*/cac:AdditionalDocumentReference[cbc:ID='ICV']/cbc:UUID") == "2"


def test_at53_at44_rejected_document_stays_in_the_chain_and_correction_is_new(live):
    doc_a, _ = stamped(live)
    live.fakes.client.script["clearance"] = [FakeZatcaClient.rejected(zatca_error("BR-KSA-44", "Bad buyer VAT"))]
    run(live)
    a = einvoice_of(doc_a)
    assert a.status == "rejected"
    doc_a.refresh_from_db()
    assert doc_a.status == "rejected"
    doc_b, b = stamped(live)  # invoicing's correction: a new document
    assert b.icv == a.icv + 1 and b.pih == a.invoice_hash
    assert doc_b.number != doc_a.number and b.uuid != a.uuid
    assert not EInvoice.objects.filter(source_document=doc_b, uuid=a.uuid).exists()


# --- R-13, R-14: hash, signature, XAdES ----------------------------------------------------------------------

def test_at60_hash_ignores_qr_signature_and_declaration_but_not_amounts(live):
    _, einv = stamped(live, simplified=True)
    xml = xml_of(einv)
    assert crypto.invoice_hash(xml) == einv.invoice_hash
    other_qr = xml.replace(einv.qr_tlv.encode(), base64.b64encode(b"\x01\x01x"))
    assert crypto.invoice_hash(other_qr) == einv.invoice_hash
    start, end = xml.index(b"<ext:UBLExtensions>"), xml.index(b"</ext:UBLExtensions>") + len(b"</ext:UBLExtensions>")
    assert crypto.invoice_hash(xml[:start] + xml[end:]) == einv.invoice_hash
    assert crypto.invoice_hash(xml.split(b"?>", 1)[1]) == einv.invoice_hash
    changed = xml.replace(b">115.00</cbc:TaxInclusiveAmount>", b">116.00</cbc:TaxInclusiveAmount>")
    assert changed != xml and crypto.invoice_hash(changed) != einv.invoice_hash


def test_at64_signature_verifies_over_the_raw_hash_only(live):
    _, einv = stamped(live)
    xml = xml_of(einv)
    sig = base64.b64decode(text(xml, "//ds:SignatureValue"))
    cert = crypto.load_cert_base64(text(xml, "//ds:X509Certificate"))
    raw = base64.b64decode(einv.invoice_hash)
    assert sig[0] == 0x30 and 70 <= len(sig) <= 72
    assert crypto.verify_hash_signature(cert.public_key(), sig, raw)
    from lxml import etree

    signed_info = etree.tostring(etree.fromstring(xml).xpath("//ds:SignedInfo", namespaces=crypto.NS)[0],
                                 method="c14n")
    for wrong in (einv.invoice_hash.encode(), raw.hex().encode(), signed_info):
        assert not crypto.verify_hash_signature(cert.public_key(), sig, wrong)
    spki = crypto.spki_der(cert.public_key())
    assert bytes.fromhex("06052b8104000a") in spki  # curve OID 1.3.132.0.10 (secp256k1)
    assert text(xml, "//ds:Reference[@Id='invoiceSignedData']/ds:DigestValue") == einv.invoice_hash


def test_at65_cert_digest_is_of_the_base64_text(live):
    _, einv = stamped(live)
    xml = xml_of(einv)
    cert_b64 = text(xml, "//ds:X509Certificate")
    expected = base64.b64encode(hashlib.sha256(cert_b64.encode()).hexdigest().encode()).decode()
    assert text(xml, "//xades:CertDigest/ds:DigestValue") == expected
    der = base64.b64decode(cert_b64)
    assert expected != base64.b64encode(hashlib.sha256(der).hexdigest().encode()).decode()
    assert expected != base64.b64encode(hashlib.sha256(der).digest()).decode()


def test_at66_signed_properties_digest_and_structure(live):
    _, einv = stamped(live)
    xml = xml_of(einv)
    written = ubl.signed_properties_bytes(xml)
    assert b"> <" not in written and b">\n" not in written  # no inter-element whitespace
    digest_form = ubl.signed_properties_digest_form(written)
    assert digest_form.startswith(b'<xades:SignedProperties xmlns:xades="http://uri.etsi.org/01903/v1.3.2#" ')
    assert digest_form.count(b'xmlns:ds="http://www.w3.org/2000/09/xmldsig#"') == 4  # each ds: child
    expected = base64.b64encode(hashlib.sha256(digest_form).hexdigest().encode()).decode()
    ref = "//ds:Reference[@URI='#xadesSignedProperties']"
    assert text(xml, f"{ref}/ds:DigestValue") == expected
    assert text(xml, f"{ref}/@Type") == "http://www.w3.org/2000/09/xmldsig#SignatureProperties"
    for absent in (b"SigningCertificateV2", b"SignaturePolicyIdentifier", b"DataObjectFormat"):
        assert absent not in xml
    signing_time = text(xml, "//xades:SigningTime")
    assert signing_time == NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    cert = crypto.load_cert_base64(text(xml, "//ds:X509Certificate"))
    assert text(xml, "//ds:X509SerialNumber") == str(cert.serial_number)
    assert text(xml, "//ds:X509IssuerName") == "CN=FAKE-TEST-ZATCA-SubCA, DC=test, DC=local"
    assert b"<cbc:ID>urn:oasis:names:specification:ubl:signature:1</cbc:ID>" in xml  # BR-KSA-28
    assert b"<sbc:ReferencedSignatureID>urn:oasis:names:specification:ubl:signature:Invoice" in xml  # BR-KSA-29
    assert text(xml, "/*/cac:Signature/cbc:SignatureMethod") == "urn:oasis:names:specification:ubl:dsig:enveloped:xades"


# --- R-15: QR ----------------------------------------------------------------------------------------------

def test_at14_simplified_qr_tags_1_to_9(live):
    _, einv = stamped(live, simplified=True)
    tags = qr.decode(einv.qr_tlv)
    assert sorted(tags) == list(range(1, 10))
    assert tags[1].decode() == "شركة TST1 للاختبار"  # Arabic seller name
    assert tags[2] == b"311111111100003"
    xml = xml_of(einv)
    assert tags[3].decode() == f"{text(xml, '/*/cbc:IssueDate')}T{text(xml, '/*/cbc:IssueTime').rstrip('Z')}Z"
    assert tags[4] == b"115.00" and tags[5] == b"15.00"
    assert tags[6].decode() == einv.invoice_hash and len(tags[6]) == 44
    assert tags[7].decode() == text(xml, "//ds:SignatureValue") and base64.b64decode(tags[7])[0] == 0x30
    serialization.load_der_public_key(tags[8])  # raw DER SubjectPublicKeyInfo
    cert = crypto.load_cert_base64(text(xml, "//ds:X509Certificate"))
    assert tags[9] == cert.signature
    assert all(len(v) <= 127 for v in tags.values())
    assert ubl.qr_from_xml(xml) == einv.qr_tlv and len(einv.qr_tlv) <= 700


def test_at14_standard_qr_comes_from_the_cleared_xml(live):
    doc, einv = stamped(live)
    assert einv.qr_tlv == "" and b"<cbc:ID>QR</cbc:ID>" not in xml_of(einv)  # seller QR optional, not built
    run(live)
    einv.refresh_from_db()
    tags = qr.decode(einv.qr_tlv)
    assert sorted(tags) == list(range(1, 9))  # no tag 9 on standard documents
    assert einv.qr_tlv == ubl.qr_from_xml(einv.cleared_xml.encode())
    doc.refresh_from_db()
    assert doc.zatca_qr == einv.qr_tlv


def test_at56_seller_name_bytes_and_characters(live, monkeypatch):
    name_128_bytes = "ش" * 64  # 2 bytes each in UTF-8
    mutate(monkeypatch, **{"seller.name": name_128_bytes})
    doc = issue(live, live.tst1, simplified=True)
    run(live)
    einv = einvoice_of(doc)
    assert einv.status == "pending" and einv.icv is None and "qr_value" in held_codes(einv)
    assert last_icv(live) == 0
    msg = einv.messages.get(code="qr_value")
    assert msg.message_ar
    mutate(monkeypatch, **{"seller.name": "ش" * 63 + "x"})  # 127 bytes passes the TLV check
    doc2 = issue(live, live.tst1, simplified=True)
    run(live)
    assert einvoice_of(doc2).icv == 1
    mutate(monkeypatch, **{"seller.name": "x" * 128})  # 128 characters: BT-27
    doc3 = issue(live, live.tst1, simplified=True)
    run(live)
    assert "seller_name_length" in held_codes(einvoice_of(doc3)) and last_icv(live) == 1


def test_at39_qr_over_the_limit_rolls_back_inside_the_stamp(live, settings):
    settings.ZATCA_QR_MAX_CHARS = 400
    doc = issue(live, live.tst1, simplified=True)
    run(live)
    einv = einvoice_of(doc)
    assert einv.icv is None and "qr_length" in held_codes(einv) and last_icv(live) == 0


# --- R-16, R-19, R-31, R-40, R-46, R-48, R-51: local checks (no ICV consumed) ------------------------------

def _held(zw, monkeypatch, code, simplified=False, **changes):
    mutate(monkeypatch, **changes)
    doc = issue(zw, zw.tst1, simplified=simplified)
    run(zw)
    einv = einvoice_of(doc)
    assert einv.status == "pending" and einv.icv is None, held_codes(einv)
    assert einv.hold_reason == "local_validation"
    assert code in held_codes(einv), held_codes(einv)
    assert last_icv(zw) == 0
    assert not [c for c in zw.fakes.client.calls if c[0] in ("clearance", "reporting")]
    return einv


def test_at15_missing_buyer_vat_on_b2b_stays_pending(live, monkeypatch):
    einv = _held(live, monkeypatch, "buyer_missing", **{"buyer.vat_number": "", "buyer.id_value": ""})
    msg = einv.messages.get(code="buyer_missing")
    assert msg.origin == "local_rules" and msg.level == "error" and msg.message_ar
    doc = einv.source_document
    doc.refresh_from_db()
    assert doc.zatca_status == "pending" and any(m["code"] == "buyer_missing" for m in doc.zatca_messages)
    run(live)  # held documents are not retried every run
    assert einv.messages.filter(code="buyer_missing").count() == 1


@pytest.mark.parametrize("subtype,changes,code", [
    ("0200100", {}, "simplified_flags"),  # simplified + export
    ("0100101", {"buyer.vat_number": ""}, "self_billed_export"),  # standard export + self-billed
    ("0100100", {}, "export_buyer_vat"),  # export with a buyer VAT number
    ("0100000", {"buyer.vat_number": "300000000000001"}, "buyer_vat"),  # ends with 1
    ("0100", {}, "subtype"),
])
def test_at49_subtype_and_buyer_rules(live, monkeypatch, subtype, changes, code):
    _held(live, monkeypatch, code, simplified=subtype.startswith("02"), subtype_code=subtype, **changes)


def test_at33a_document_level_vat_passes_without_line_tolerance(live):
    doc = issue(live, live.tst1, lines=[("management_fee", "1", "10.03")] * 3)
    assert doc.total_vat == D("4.51") and sum(line.vat_amount for line in doc.lines.all()) == D("4.50")
    run(live)
    assert einvoice_of(doc).status == "cleared"


def test_at33b_category_vat_off_by_a_cent_is_refused_locally(live, monkeypatch):
    def off(data):
        data.groups[0].vat = D("4.50")
        return D("4.50")

    mutate(monkeypatch, total_vat=off)
    doc = issue(live, live.tst1, lines=[("management_fee", "1", "10.03")] * 3)
    run(live)
    einv = einvoice_of(doc)
    assert "category_vat" in held_codes(einv) and einv.icv is None and last_icv(live) == 0


def test_at33c_header_net_differs_from_lines(live, monkeypatch):
    _held(live, monkeypatch, "header_net", total_net=D("30.10"))


def test_r31_line_total_must_be_net_plus_vat(live, monkeypatch):
    def bad_line(data):
        data.lines[0].amount_with_vat = data.lines[0].net + data.lines[0].vat + D("0.01")
        return data.total_vat

    _held(live, monkeypatch, "line_total", total_vat=bad_line)


def test_at55_signed_xml_over_10_mb_refused(live, monkeypatch):
    _held(live, monkeypatch, "xml_size", attachments=[("BIG", "A" * (10 * 1024 * 1024 + 10))])


def test_at63_seller_id_crn_only_and_700_without_cr(live, monkeypatch):
    doc, einv = stamped(live)
    xml = xml_of(einv)
    from lxml import etree

    ids = etree.fromstring(xml).xpath("/*/cac:AccountingSupplierParty/cac:Party/cac:PartyIdentification/cbc:ID",
                                      namespaces=crypto.NS)
    assert [(i.get("schemeID"), i.text) for i in ids] == [("CRN", "1010000001")]
    assert b"7000000001" not in xml  # the unified number is never in the XML (R-48)
    original = services._invoice_data

    def unified_only(einv):
        einv.source_document.seller_cr_number = ""
        einv.source_document.seller_unified_number = "7000000002"
        return original(einv)

    monkeypatch.setattr(services, "_invoice_data", unified_only)
    _, einv2 = stamped(live)
    ids = etree.fromstring(xml_of(einv2)).xpath(
        "/*/cac:AccountingSupplierParty/cac:Party/cac:PartyIdentification/cbc:ID", namespaces=crypto.NS)
    assert [(i.get("schemeID"), i.text) for i in ids] == [("700", "7000000002")]

    def none(einv):
        einv.source_document.seller_cr_number = einv.source_document.seller_unified_number = ""
        return original(einv)

    monkeypatch.setattr(services, "_invoice_data", none)
    doc3 = issue(live, live.tst1)
    run(live)
    assert "seller_id" in held_codes(einvoice_of(doc3)) and last_icv(live) == 2


def test_r4_seller_address_checked_at_stamping(live, monkeypatch):
    _held(live, monkeypatch, "building_no", **{"seller.building_no": "754"})


def test_r51_third_decimal_refused(live, monkeypatch):
    def three(data):
        data.currency_places = 3
        data.lines[0].net = D("99.999")
        return D("99.999")

    _held(live, monkeypatch, "third_decimal", total_net=three)


def test_r40_non_sar_needs_sar_vat(live, monkeypatch):
    _held(live, monkeypatch, "sar_vat", currency="USD", total_vat_sar=D("0"))


# --- XML content (R-14, R-19, R-33, R-40, R-45, R-46) ---------------------------------------------------------

def test_at51_at45_sar_invoice_xml(live):
    _, einv = stamped(live)
    xml = xml_of(einv)
    from lxml import etree

    root = etree.fromstring(xml)
    assert root.tag == "{urn:oasis:names:specification:ubl:schema:xsd:Invoice-2}Invoice"
    assert text(xml, "/*/cbc:TaxCurrencyCode") == "SAR" and text(xml, "/*/cbc:DocumentCurrencyCode") == "SAR"
    totals = root.xpath("/*/cac:TaxTotal", namespaces=crypto.NS)
    assert len(totals) == 2
    assert totals[0].xpath("cbc:TaxAmount/@currencyID", namespaces=crypto.NS) == ["SAR"]
    assert totals[0].xpath("cac:TaxSubtotal", namespaces=crypto.NS)
    assert totals[1].xpath("cbc:TaxAmount/@currencyID", namespaces=crypto.NS) == ["SAR"]
    assert not totals[1].xpath("cac:TaxSubtotal", namespaces=crypto.NS)
    seller = "/*/cac:AccountingSupplierParty/cac:Party"
    assert text(xml, f"{seller}/cac:PartyLegalEntity/cbc:RegistrationName") == "شركة TST1 للاختبار"
    assert text(xml, f"{seller}/cac:PostalAddress/cbc:StreetName") == "شارع الاختبار"
    assert text(xml, f"{seller}/cac:PostalAddress/cbc:CitySubdivisionName") == "حي الاختبار"
    assert text(xml, f"{seller}/cac:PostalAddress/cbc:CityName") == "الرياض"
    assert text(xml, f"{seller}/cac:PostalAddress/cbc:BuildingNumber") == "1234"
    assert text(xml, "/*/cbc:InvoiceTypeCode/@name") == "0100000" and text(xml, "/*/cbc:InvoiceTypeCode") == "388"
    assert text(xml, "/*/cbc:IssueTime").endswith("Z")
    assert text(xml, "/*/cac:Delivery/cbc:ActualDeliveryDate") == TODAY.isoformat()


def test_at46_intercompany_cleared_as_b2b_signed_by_tst1(live):
    onboard(live, live.tst2, live.admin2)
    ic = invoicing_services.create_customer(live.tst1, user=live.clerk, code="IC-TST2", name_ar="شركة TST2",
                                            name_en="TST2 Co", is_vat_registered=True, vat_number="322222222200003",
                                            group_entity=live.tst2, **ADDRESS)
    from types import SimpleNamespace

    w = SimpleNamespace(alm=live.tst1, clerk=live.clerk, customer=ic, cfo=live.cfo)
    doc = approve(w, invoice(w, [("management_fee", "1", "100.00")], customer=ic, issue_date=TODAY, entity=live.tst1),
                  live.cfo)
    run(live)
    einv = einvoice_of(doc)
    assert einv.status == "cleared" and einv.api == "clearance" and einv.egs_unit == live.egs1
    assert text(xml_of(einv), "/*/cac:AccountingCustomerParty/cac:Party/cac:PartyTaxScheme/cbc:CompanyID") == \
        "322222222200003"


def _final_invoice(zw):
    doc = issue(zw, zw.tst1)
    run(zw)
    doc.refresh_from_db()
    assert doc.status == "final"
    return doc


def _note(zw, original, doc_type="credit_note", **fields):
    from types import SimpleNamespace

    w = SimpleNamespace(alm=zw.tst1, clerk=zw.clerk, customer=original.customer, cfo=zw.cfo)
    fields.setdefault("payment_means_code", "10")
    fields.setdefault("reason_ar", "إرجاع البضاعة")
    note = invoice(w, [("management_fee", "1", "10.00")], customer=original.customer, issue_date=TODAY,
                   entity=zw.tst1, doc_type=doc_type, original_document=original, **fields)
    return approve(w, note, zw.cfo)


def test_at54_at67_credit_and_debit_notes(live, monkeypatch):
    original = _final_invoice(live)
    for doc_type, code in (("credit_note", "381"), ("debit_note", "383")):
        note = _note(live, original, doc_type)
        run(live)
        einv = einvoice_of(note)
        assert einv.status == "cleared" and einv.billing_reference == einvoice_of(original)
        xml = xml_of(einv)
        assert xml.split(b"\n", 2)[1].startswith(b"<Invoice ")  # root Invoice for 381/383
        assert text(xml, "/*/cbc:InvoiceTypeCode") == code
        assert text(xml, "/*/cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID") == original.number
        assert text(xml, "/*/cac:PaymentMeans/cbc:InstructionNote") == "إرجاع البضاعة"
        assert text(xml, "/*/cac:PaymentMeans/cbc:PaymentMeansCode") == "10"
    held_before = last_icv(live)
    mutate(monkeypatch, payment_means_code="")
    note = _note(live, original)
    run(live)
    assert "payment_means" in held_codes(einvoice_of(note)) and last_icv(live) == held_before


def test_at67_at28_note_without_reason_refused(live, monkeypatch):
    original = _final_invoice(live)
    icv = last_icv(live)
    mutate(monkeypatch, reason="")
    note = _note(live, original)
    run(live)
    assert "reason" in held_codes(einvoice_of(note)) and last_icv(live) == icv
    with pytest.raises(Exception):  # invoicing refuses an English-only reason at issue (R-19, invoicing R19)
        _note(live, original, reason_ar="", reason_en="Goods returned")


def test_at27_note_on_another_entitys_invoice(live, monkeypatch):
    onboard(live, live.tst2, live.admin2)
    other = issue(live, live.tst2)
    run(live)
    original = _final_invoice(live)
    note = _note(live, original)
    einv = einvoice_of(note)
    einv.billing_reference = einvoice_of(other)
    einv._service_write = True
    with pytest.raises(ZatcaError):  # R-1 model guard
        einv.save()
    mutate(monkeypatch, billing_reference_entity_ok=False)
    run(live)
    assert "billing_reference" in held_codes(einvoice_of(note))


def test_at16_other_entity_egs_never_signs(live):
    egs2 = onboard(live, live.tst2, live.admin2, code="EGS9")
    doc = issue(live, live.tst1)
    einv = einvoice_of(doc)
    einv.egs_unit = egs2
    einv._service_write = True
    with pytest.raises(ZatcaError):
        einv.save()
    einv.refresh_from_db()
    profile = services.ZatcaProfile.objects.get(entity=live.tst1)
    with pytest.raises(ZatcaError) as exc:
        services._signing_credential(einv, egs2, profile, NOW)
    assert exc.value.code == "other_entity_egs"


def test_at72_kwd_amounts_emitted_with_two_decimals(live, monkeypatch):
    from ledger import services as ledger

    treasurer = make_user("treasury_kwd")
    member(treasurer, live.par, "group_treasury")
    ledger.create_rate(user=treasurer, from_currency="KWD", to_currency="SAR", on=TODAY, rate=D("12.2000"),
                       group=live.par)
    doc = issue(live, live.tst1, lines=[("management_fee", "1", "1000.000")], currency="KWD")
    assert doc.total_net == D("1000.000") and doc.total_vat == D("150.000")
    run(live)
    einv = einvoice_of(doc)
    xml = xml_of(einv)
    assert text(xml, "/*/cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount") == "1000.00"
    assert text(xml, "/*/cac:TaxTotal[cac:TaxSubtotal]/cbc:TaxAmount") == "150.00"
    assert text(xml, "/*/cac:TaxTotal[not(cac:TaxSubtotal)]/cbc:TaxAmount") == "1830.00"
    assert text(xml, "/*/cbc:DocumentCurrencyCode") == "KWD"
    icv = last_icv(live)

    def three(data):
        data.lines[0].net = D("999.999")
        return D("999.999")

    mutate(monkeypatch, total_net=three)
    doc2 = issue(live, live.tst1, lines=[("management_fee", "1", "1000.000")], currency="KWD")
    run(live)
    assert "third_decimal" in held_codes(einvoice_of(doc2)) and last_icv(live) == icv


def test_at42_usd_invoice_carries_the_sar_tax_total(live, monkeypatch):
    from ledger import services as ledger

    treasurer = make_user("treasury_usd")
    member(treasurer, live.par, "group_treasury")
    ledger.create_rate(user=treasurer, from_currency="USD", to_currency="SAR", on=TODAY, rate=D("3.75"),
                       group=live.par)
    doc = issue(live, live.tst1, lines=[("management_fee", "1", "1000.00")], currency="USD")
    assert doc.total_vat_sar == D("562.50")
    run(live)
    xml = xml_of(einvoice_of(doc))
    assert text(xml, "/*/cbc:TaxCurrencyCode") == "SAR"
    assert text(xml, "/*/cac:TaxTotal[cac:TaxSubtotal]/cbc:TaxAmount/@currencyID") == "USD"
    assert text(xml, "/*/cac:TaxTotal[not(cac:TaxSubtotal)]/cbc:TaxAmount") == "562.50"
    assert text(xml, "/*/cac:TaxTotal[not(cac:TaxSubtotal)]/cbc:TaxAmount/@currencyID") == "SAR"
    icv = last_icv(live)
    mutate(monkeypatch, total_vat_sar=D("0"))
    doc2 = issue(live, live.tst1, lines=[("management_fee", "1", "1000.00")], currency="USD")
    run(live)
    assert "sar_vat" in held_codes(einvoice_of(doc2)) and last_icv(live) == icv


# --- R-17, R-18, R-30, R-38: immutability, retention, archive -----------------------------------------------------

def test_at25_cleared_e_invoice_never_changes(live):
    doc, einv = stamped(live)
    run(live)
    einv.refresh_from_db()
    assert einv.status == "cleared"
    for field, value in (("signed_xml", "<x/>"), ("cleared_xml", "<y/>"), ("invoice_hash", "x"), ("icv", 99)):
        fresh = EInvoice.objects.get(pk=einv.pk)
        setattr(fresh, field, value)
        fresh._service_write = True
        with pytest.raises(ZatcaError):
            fresh.save()
    with pytest.raises(ZatcaError):
        EInvoice.objects.get(pk=einv.pk).delete()
    with pytest.raises(ZatcaError):
        EInvoice.objects.filter(pk=einv.pk).update(signed_xml="")
    with pytest.raises(ZatcaError):
        EInvoice.objects.filter(pk=einv.pk).delete()
    fresh = EInvoice.objects.get(pk=einv.pk)
    fresh.status = "pending"
    with pytest.raises(ZatcaError):  # not through the services
        fresh.save()


def test_at26_attempts_and_messages_are_append_only(live):
    doc, einv = stamped(live)
    run(live)
    attempt = SubmissionAttempt.objects.get(einvoice=einv)
    attempt.http_status = 500
    with pytest.raises(ZatcaError):
        attempt.save()
    with pytest.raises(ZatcaError):
        attempt.delete()
    with pytest.raises(ZatcaError):
        SubmissionAttempt.objects.filter(pk=attempt.pk).update(http_status=1)
    event = services.OnboardingEvent.objects.first()
    with pytest.raises(ZatcaError):
        event.delete()


def test_at40_retention_dates(live):
    type(live.tst1).objects.filter(pk=live.tst1.pk).update(vat_return_period="monthly")
    day = date(2027, 3, 10)
    a = einvoice_of(issue(live, live.tst1, day=day))
    b = einvoice_of(issue(live, live.tst1, day=day, lines=(("rent_commercial", "1", "100.00"),)))
    assert a.retention_class == "standard" and a.retention_until == date(2033, 3, 31)
    assert b.retention_class == "capital_asset_real_estate" and b.retention_until == date(2042, 3, 31)


def test_at58_movable_asset_retention_never_shortened(live, settings):
    settings.ZATCA_MOVABLE_ADJUSTMENT_YEARS = 5
    type(live.tst1).objects.filter(pk=live.tst1.pk).update(vat_return_period="monthly")
    doc = issue(live, live.tst1, day=date(2027, 3, 10))
    einv = einvoice_of(doc)
    einv.retention_class, einv.acquisition_date = "movable_capital_asset", date(2027, 3, 10)  # supplied by invoicing
    einv._service_write = True
    einv.save()
    run(live)
    einv.refresh_from_db()
    assert einv.icv == 1 and einv.retention_until == date(2037, 3, 10)
    settings.ZATCA_MOVABLE_ADJUSTMENT_YEARS = 1
    einv.retention_until = date(2034, 3, 10)
    einv._service_write = True
    with pytest.raises(ZatcaError):
        einv.save()


def test_r38_movable_without_acquisition_date_held(live):
    doc = issue(live, live.tst1)
    einv = einvoice_of(doc)
    einv.retention_class = "movable_capital_asset"
    einv._service_write = True
    einv.save()
    run(live)
    assert "movable_acquisition" in held_codes(einvoice_of(doc))


def test_at59_archive_file_name_and_export_rights(live):
    doc, einv = stamped(live)
    run(live)
    name, xml = services.export_archive(einv, user=live.admin1)
    issue_utc = einv.issue_datetime
    assert name == f"311111111100003_{issue_utc:%Y%m%d}_{issue_utc:%H%M%S}_{doc.number}.xml"
    einv.refresh_from_db()
    assert xml == einv.cleared_xml.encode()
    assert services.export_archive(einv, user=live.viewer)[0] == name
    from core import access

    with pytest.raises(access.AccessDenied):
        services.export_archive(einv, user=live.clerk)


def test_at34_purge_keeps_rows(live):
    doc, einv = stamped(live)
    assert einv.retention_until > date.today()
    result = services.purge_archive(live.tst1, user=live.admin1)
    assert result["deleted"] == 0 and EInvoice.objects.filter(pk=einv.pk).exists()
    with pytest.raises(ZatcaError):
        einv.delete()


def test_at69_module_signed_simplified_invoice_shape(live):
    """Without the SDK: the module output has the structure the SDK validated (test_sdk_offline.py checks it
    with the SDK where available)."""
    _, einv = stamped(live, simplified=True)
    xml = xml_of(einv)
    order = [xml.index(tag) for tag in (b"<ext:UBLExtensions>", b"<cbc:ProfileID>", b"<cbc:ID>ICV</cbc:ID>",
                                        b"<cbc:ID>PIH</cbc:ID>", b"<cbc:ID>QR</cbc:ID>", b"<cac:Signature>",
                                        b"<cac:AccountingSupplierParty>")]
    assert order == sorted(order)
