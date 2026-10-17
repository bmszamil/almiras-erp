"""Generate fake UBL 2.1 test documents for the open SDK points of 2026-10-17 (fake seller/buyer data only).

Usage: python3 -I make_invoices3.py OUTDIR
Seller VAT 300000000000003 (fake), buyer VAT 399999999800003 (the SDK samples' fake buyer), CR 1010010000.
Documents (unsigned; sign them with the SDK -sign or the module):
  simp-388-name127 / -name128 / -name200  seller name of 127 / 128 / 200 UTF-8 bytes (QR TLV length form)
  std-386, simp-386                     prepayment invoices (type 386), one line, 1,000.00 + 15 %
  std-388-final, simp-388-final         final invoices settling std-386 / simp-386 like the SDK "Advance
                                        Payment adjustments" sample (zero-amount line with DocumentReference 386)
  std-388-final-qrtotal                 as std-388-final (used to test QR tag 4 = BT-112 vs BT-115 when signed)
  std-388-kwd2, simp-388-kwd2           KWD documents, 2-decimal amounts (BT-111 in SAR)
  std-388-kwd3, simp-388-kwd3           KWD documents with 3-decimal amounts (KWD 1000.125 etc.)
  std-388-kwd3-vat3                     only the VAT amounts carry 3 decimals
"""
import pathlib
import sys
import uuid
from decimal import ROUND_HALF_UP, Decimal

OUT = pathlib.Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
PIH0 = "NWZlY2ViNjZmZmM4NmYzOGQ5NTI3ODZjNmQ2OTZjNzljMmRiYzIzOWRkNGU5MWI0NjcyOWQ3M2EyN2ZiNTdlOQ=="
NS = ('xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2" '
      'xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2" '
      'xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2" '
      'xmlns:ext="urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2"')
ADDR = """<cac:PostalAddress>
        <cbc:StreetName>Test Street</cbc:StreetName>
        <cbc:BuildingNumber>1234</cbc:BuildingNumber>
        <cbc:CitySubdivisionName>Test District</cbc:CitySubdivisionName>
        <cbc:CityName>Riyadh</cbc:CityName>
        <cbc:PostalZone>12345</cbc:PostalZone>
        <cac:Country><cbc:IdentificationCode>SA</cbc:IdentificationCode></cac:Country>
      </cac:PostalAddress>"""
SELLER = "Fake Seller Co | شركة البائع التجريبية"


def q(v, places):
    return Decimal(v).quantize(Decimal(1).scaleb(-places), ROUND_HALF_UP)


def name_of_bytes(n):
    """A fake Arabic/Latin seller name of exactly n UTF-8 bytes."""
    s = "Fake Seller | "
    for ch in "شركة البائع التجريبية " * 20:
        if len((s + ch).encode()) > n:
            break
        s += ch
    s = s.rstrip()
    s += "x" * (n - len(s.encode()))
    assert len(s.encode()) == n, (n, len(s.encode()))
    return s


def line_xml(i, net, vat, cur, places, name="Test item", qty="1.000000", price=None, docref=None, prepaid=None):
    ref = ""
    sub = ""
    if docref:
        ref = f"""
    <cac:DocumentReference>
      <cbc:ID>{docref['id']}</cbc:ID>
      <cbc:UUID>{docref['uuid']}</cbc:UUID>
      <cbc:IssueDate>{docref['date']}</cbc:IssueDate>
      <cbc:IssueTime>{docref['time']}</cbc:IssueTime>
      <cbc:DocumentTypeCode>386</cbc:DocumentTypeCode>
    </cac:DocumentReference>"""
    if prepaid:
        sub = f"""
      <cac:TaxSubtotal>
        <cbc:TaxableAmount currencyID="{cur}">{prepaid[0]}</cbc:TaxableAmount>
        <cbc:TaxAmount currencyID="{cur}">{prepaid[1]}</cbc:TaxAmount>
        <cac:TaxCategory><cbc:ID>S</cbc:ID><cbc:Percent>15.00</cbc:Percent><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:TaxCategory>
      </cac:TaxSubtotal>"""
    gross = Decimal(net) + Decimal(vat)
    return f"""  <cac:InvoiceLine>
    <cbc:ID>{i}</cbc:ID>
    <cbc:InvoicedQuantity unitCode="PCE">{qty}</cbc:InvoicedQuantity>
    <cbc:LineExtensionAmount currencyID="{cur}">{net}</cbc:LineExtensionAmount>{ref}
    <cac:TaxTotal>
      <cbc:TaxAmount currencyID="{cur}">{vat}</cbc:TaxAmount>
      <cbc:RoundingAmount currencyID="{cur}">{q(gross, places)}</cbc:RoundingAmount>{sub}
    </cac:TaxTotal>
    <cac:Item>
      <cbc:Name>{name} {i}</cbc:Name>
      <cac:ClassifiedTaxCategory><cbc:ID>S</cbc:ID><cbc:Percent>15.00</cbc:Percent><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:ClassifiedTaxCategory>
    </cac:Item>
    <cac:Price><cbc:PriceAmount currencyID="{cur}">{price if price is not None else net}</cbc:PriceAmount></cac:Price>
  </cac:InvoiceLine>"""


def doc(name, type_code, subtype, *, seller=SELLER, cur="SAR", places=2, net="1000.00", vat="150.00", vat_sar=None,
        prepaid="0.00", lines=None, issue="2022-09-07", time="15:23:55"):
    std = subtype.startswith("01")
    gross = Decimal(net) + Decimal(vat)
    payable = gross - Decimal(prepaid)
    buyer = f"""
  <cac:AccountingCustomerParty>
    <cac:Party>
      {ADDR}
      <cac:PartyTaxScheme><cbc:CompanyID>399999999800003</cbc:CompanyID><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:PartyTaxScheme>
      <cac:PartyLegalEntity><cbc:RegistrationName>Fake Buyer Co</cbc:RegistrationName></cac:PartyLegalEntity>
    </cac:Party>
  </cac:AccountingCustomerParty>""" if std else """
  <cac:AccountingCustomerParty><cac:Party/></cac:AccountingCustomerParty>"""
    delivery = f"""
  <cac:Delivery><cbc:ActualDeliveryDate>{issue}</cbc:ActualDeliveryDate></cac:Delivery>""" if std else ""
    sar = vat_sar if vat_sar is not None else vat
    body = lines if lines is not None else line_xml(1, net, vat, cur, places)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice {NS}>
  <cbc:ProfileID>reporting:1.0</cbc:ProfileID>
  <cbc:ID>TEST3-{name}</cbc:ID>
  <cbc:UUID>{uuid.uuid5(uuid.NAMESPACE_URL, 'almiras-sdk3-' + name)}</cbc:UUID>
  <cbc:IssueDate>{issue}</cbc:IssueDate>
  <cbc:IssueTime>{time}</cbc:IssueTime>
  <cbc:InvoiceTypeCode name="{subtype}">{type_code}</cbc:InvoiceTypeCode>
  <cbc:DocumentCurrencyCode>{cur}</cbc:DocumentCurrencyCode>
  <cbc:TaxCurrencyCode>SAR</cbc:TaxCurrencyCode>
  <cac:AdditionalDocumentReference>
    <cbc:ID>ICV</cbc:ID>
    <cbc:UUID>1</cbc:UUID>
  </cac:AdditionalDocumentReference>
  <cac:AdditionalDocumentReference>
    <cbc:ID>PIH</cbc:ID>
    <cac:Attachment>
      <cbc:EmbeddedDocumentBinaryObject mimeCode="text/plain">{PIH0}</cbc:EmbeddedDocumentBinaryObject>
    </cac:Attachment>
  </cac:AdditionalDocumentReference>
  <cac:AccountingSupplierParty>
    <cac:Party>
      <cac:PartyIdentification><cbc:ID schemeID="CRN">1010010000</cbc:ID></cac:PartyIdentification>
      {ADDR}
      <cac:PartyTaxScheme><cbc:CompanyID>300000000000003</cbc:CompanyID><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:PartyTaxScheme>
      <cac:PartyLegalEntity><cbc:RegistrationName>{seller}</cbc:RegistrationName></cac:PartyLegalEntity>
    </cac:Party>
  </cac:AccountingSupplierParty>{buyer}{delivery}
  <cac:PaymentMeans>
    <cbc:PaymentMeansCode>10</cbc:PaymentMeansCode>
  </cac:PaymentMeans>
  <cac:TaxTotal>
    <cbc:TaxAmount currencyID="{cur}">{vat}</cbc:TaxAmount>
    <cac:TaxSubtotal>
      <cbc:TaxableAmount currencyID="{cur}">{net}</cbc:TaxableAmount>
      <cbc:TaxAmount currencyID="{cur}">{vat}</cbc:TaxAmount>
      <cac:TaxCategory><cbc:ID>S</cbc:ID><cbc:Percent>15.00</cbc:Percent><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:TaxCategory>
    </cac:TaxSubtotal>
  </cac:TaxTotal>
  <cac:TaxTotal>
    <cbc:TaxAmount currencyID="SAR">{sar}</cbc:TaxAmount>
  </cac:TaxTotal>
  <cac:LegalMonetaryTotal>
    <cbc:LineExtensionAmount currencyID="{cur}">{net}</cbc:LineExtensionAmount>
    <cbc:TaxExclusiveAmount currencyID="{cur}">{net}</cbc:TaxExclusiveAmount>
    <cbc:TaxInclusiveAmount currencyID="{cur}">{q(gross, places)}</cbc:TaxInclusiveAmount>
    <cbc:AllowanceTotalAmount currencyID="{cur}">{q(0, places)}</cbc:AllowanceTotalAmount>
    <cbc:PrepaidAmount currencyID="{cur}">{q(prepaid, places)}</cbc:PrepaidAmount>
    <cbc:PayableAmount currencyID="{cur}">{q(payable, places)}</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>
{body}
</Invoice>
"""


def write(name, text):
    (OUT / f"{name}.xml").write_text(text, encoding="utf-8")


for n in (127, 128, 200):
    write(f"simp-388-name{n}", doc(f"simp-388-name{n}", "388", "0200000", seller=name_of_bytes(n),
                                   net="100.00", vat="15.00"))
write("std-386", doc("std-386", "386", "0100000", issue="2022-08-15", time="12:28:17"))
write("simp-386", doc("simp-386", "386", "0200000", issue="2022-08-15", time="12:28:17"))
for cat, sub in (("std", "0100000"), ("simp", "0200000")):
    ref = {"id": f"TEST3-{cat}-386", "uuid": uuid.uuid5(uuid.NAMESPACE_URL, f"almiras-sdk3-{cat}-386"),
           "date": "2022-08-15", "time": "12:28:17"}
    body = (line_xml(1, "4000.00", "600.00", "SAR", 2) + "\n"
            + line_xml(2, "0.00", "0.00", "SAR", 2, qty="0.000000", price="0.00", docref=ref,
                       prepaid=("1000.00", "150.00")))
    write(f"{cat}-388-final", doc(f"{cat}-388-final", "388", sub, net="4000.00", vat="600.00", prepaid="1150.00",
                                  lines=body))
# KWD: rate SAR per KWD 12.25 (fake); VAT in SAR = 150.00 * 12.25 etc.
for cat, sub in (("std", "0100000"), ("simp", "0200000")):
    write(f"{cat}-388-kwd2", doc(f"{cat}-388-kwd2", "388", sub, cur="KWD", net="1000.00", vat="150.00",
                                 vat_sar="1837.50"))
    write(f"{cat}-388-kwd3", doc(f"{cat}-388-kwd3", "388", sub, cur="KWD", places=3, net="1000.125",
                                 vat="150.019", vat_sar="1837.73",
                                 lines=line_xml(1, "1000.125", "150.019", "KWD", 3)))
write("std-388-kwd3-vat3", doc("std-388-kwd3-vat3", "388", "0100000", cur="KWD", places=3, net="1000.10",
                               vat="150.015", vat_sar="1837.68", lines=line_xml(1, "1000.10", "150.015", "KWD", 3)))
print("ok", sorted(p.name for p in OUT.iterdir()))
