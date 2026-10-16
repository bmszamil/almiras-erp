"""Generate minimal fake UBL 2.1 test documents for the ZATCA SDK (fake seller/buyer data only)."""
import sys, uuid, pathlib

OUT = pathlib.Path(sys.argv[1])
PIH0 = "NWZlY2ViNjZmZmM4NmYzOGQ5NTI3ODZjNmQ2OTZjNzljMmRiYzIzOWRkNGU5MWI0NjcyOWQ3M2EyN2ZiNTdlOQ=="

NS = ('xmlns="urn:oasis:names:specification:ubl:schema:xsd:{root}-2" '
      'xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2" '
      'xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2" '
      'xmlns:ext="urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2"')

ADDR = """<cac:PostalAddress>
        <cbc:StreetName>Test Street</cbc:StreetName>
        <cbc:BuildingNumber>1234</cbc:BuildingNumber>
        <cbc:PlotIdentification>1234</cbc:PlotIdentification>
        <cbc:CitySubdivisionName>Test District</cbc:CitySubdivisionName>
        <cbc:CityName>Riyadh</cbc:CityName>
        <cbc:PostalZone>12345</cbc:PostalZone>
        <cbc:CountrySubentity>Riyadh Region</cbc:CountrySubentity>
        <cac:Country><cbc:IdentificationCode>SA</cbc:IdentificationCode></cac:Country>
      </cac:PostalAddress>"""


def doc(name, type_code, subtype, root="Invoice", billing_ref=None, buyer=True, note_reason=None,
        net="100.00", vat="15.00", gross="115.00", prepaid=None, icv=1, issue="2022-08-17"):
    std = subtype.startswith("01")
    billing = (f"""
  <cac:BillingReference><cac:InvoiceDocumentReference><cbc:ID>{billing_ref}</cbc:ID></cac:InvoiceDocumentReference></cac:BillingReference>"""
               if billing_ref else "")
    buyer_xml = f"""
  <cac:AccountingCustomerParty>
    <cac:Party>
      {ADDR}
      <cac:PartyTaxScheme><cbc:CompanyID>399999999900003</cbc:CompanyID><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:PartyTaxScheme>
      <cac:PartyLegalEntity><cbc:RegistrationName>Fake Buyer Co</cbc:RegistrationName></cac:PartyLegalEntity>
    </cac:Party>
  </cac:AccountingCustomerParty>""" if buyer else """
  <cac:AccountingCustomerParty><cac:Party/></cac:AccountingCustomerParty>"""
    delivery = f"""
  <cac:Delivery><cbc:ActualDeliveryDate>{issue}</cbc:ActualDeliveryDate></cac:Delivery>""" if std else ""
    reason = f"<cbc:InstructionNote>{note_reason}</cbc:InstructionNote>" if note_reason else ""
    paid = prepaid or "0.00"
    payable = f"{float(gross) - float(paid):.2f}"
    x = f"""<?xml version="1.0" encoding="UTF-8"?>
<{root} {NS.format(root=root)}>
  <cbc:ProfileID>reporting:1.0</cbc:ProfileID>
  <cbc:ID>TEST-{name}</cbc:ID>
  <cbc:UUID>{uuid.uuid5(uuid.NAMESPACE_URL, name)}</cbc:UUID>
  <cbc:IssueDate>{issue}</cbc:IssueDate>
  <cbc:IssueTime>17:41:08</cbc:IssueTime>
  <cbc:{"InvoiceTypeCode" if root == "Invoice" else "CreditNoteTypeCode"} name="{subtype}">{type_code}</cbc:{"InvoiceTypeCode" if root == "Invoice" else "CreditNoteTypeCode"}>
  <cbc:DocumentCurrencyCode>SAR</cbc:DocumentCurrencyCode>
  <cbc:TaxCurrencyCode>SAR</cbc:TaxCurrencyCode>{billing}
  <cac:AdditionalDocumentReference>
    <cbc:ID>ICV</cbc:ID>
    <cbc:UUID>{icv}</cbc:UUID>
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
      <cac:PartyLegalEntity><cbc:RegistrationName>Fake Seller Co | شركة البائع التجريبية</cbc:RegistrationName></cac:PartyLegalEntity>
    </cac:Party>
  </cac:AccountingSupplierParty>{buyer_xml}{delivery}
  <cac:PaymentMeans>
    <cbc:PaymentMeansCode>10</cbc:PaymentMeansCode>{reason}
  </cac:PaymentMeans>
  <cac:TaxTotal>
    <cbc:TaxAmount currencyID="SAR">{vat}</cbc:TaxAmount>
  </cac:TaxTotal>
  <cac:TaxTotal>
    <cbc:TaxAmount currencyID="SAR">{vat}</cbc:TaxAmount>
    <cac:TaxSubtotal>
      <cbc:TaxableAmount currencyID="SAR">{net}</cbc:TaxableAmount>
      <cbc:TaxAmount currencyID="SAR">{vat}</cbc:TaxAmount>
      <cac:TaxCategory>
        <cbc:ID>S</cbc:ID>
        <cbc:Percent>15.00</cbc:Percent>
        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
      </cac:TaxCategory>
    </cac:TaxSubtotal>
  </cac:TaxTotal>
  <cac:LegalMonetaryTotal>
    <cbc:LineExtensionAmount currencyID="SAR">{net}</cbc:LineExtensionAmount>
    <cbc:TaxExclusiveAmount currencyID="SAR">{net}</cbc:TaxExclusiveAmount>
    <cbc:TaxInclusiveAmount currencyID="SAR">{gross}</cbc:TaxInclusiveAmount>
    <cbc:AllowanceTotalAmount currencyID="SAR">0.00</cbc:AllowanceTotalAmount>
    <cbc:PrepaidAmount currencyID="SAR">{paid}</cbc:PrepaidAmount>
    <cbc:PayableAmount currencyID="SAR">{payable}</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>
LINES
</{root}>
"""
    return x


def lines(n, unit_net, unit_vat, root="Invoice"):
    tag = "InvoiceLine" if root == "Invoice" else "CreditNoteLine"
    qty = "InvoicedQuantity" if root == "Invoice" else "CreditedQuantity"
    out = []
    for i in range(1, n + 1):
        out.append(f"""  <cac:{tag}>
    <cbc:ID>{i}</cbc:ID>
    <cbc:{qty} unitCode="PCE">1.000000</cbc:{qty}>
    <cbc:LineExtensionAmount currencyID="SAR">{unit_net}</cbc:LineExtensionAmount>
    <cac:TaxTotal>
      <cbc:TaxAmount currencyID="SAR">{unit_vat}</cbc:TaxAmount>
      <cbc:RoundingAmount currencyID="SAR">{float(unit_net) + float(unit_vat):.2f}</cbc:RoundingAmount>
    </cac:TaxTotal>
    <cac:Item>
      <cbc:Name>Test item {i}</cbc:Name>
      <cac:ClassifiedTaxCategory>
        <cbc:ID>S</cbc:ID>
        <cbc:Percent>15.00</cbc:Percent>
        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
      </cac:ClassifiedTaxCategory>
    </cac:Item>
    <cac:Price>
      <cbc:PriceAmount currencyID="SAR">{unit_net}</cbc:PriceAmount>
    </cac:Price>
  </cac:{tag}>""")
    return "\n".join(out)


def write(fname, x, ln):
    (OUT / fname).write_text(x.replace("LINES", ln), encoding="utf-8")


L1 = lines(1, "100.00", "15.00")
write("std-388.xml", doc("std-388", "388", "0100000"), L1)
write("simp-388.xml", doc("simp-388", "388", "0200000", buyer=False), L1)
write("std-381.xml", doc("std-381", "381", "0100000", billing_ref="TEST-std-388", note_reason="Goods returned"), L1)
write("std-381-cnroot.xml", doc("std-381-cn", "381", "0100000", root="CreditNote", billing_ref="TEST-std-388",
                                note_reason="Goods returned"), lines(1, "100.00", "15.00", root="CreditNote"))
write("simp-381.xml", doc("simp-381", "381", "0200000", buyer=False, billing_ref="TEST-simp-388", note_reason="Goods returned"), L1)
write("std-383.xml", doc("std-383", "383", "0100000", billing_ref="TEST-std-388", note_reason="Price correction"), L1)
write("simp-383.xml", doc("simp-383", "383", "0200000", buyer=False, billing_ref="TEST-simp-388", note_reason="Price correction"), L1)
write("std-386.xml", doc("std-386", "386", "0100000"), L1)
write("simp-386.xml", doc("simp-386", "386", "0200000", buyer=False), L1)
# R-31 / AT-33(a): 3 lines net 10.03, line VAT 1.50 each (sum 4.50), category VAT 4.51
write("std-388-tol.xml", doc("std-388-tol", "388", "0100000", net="30.09", vat="4.51", gross="34.60"),
      lines(3, "10.03", "1.50"))
# AT-33(b): category VAT 4.50 (wrong per BR-CO-17 rounding)
write("std-388-tolb.xml", doc("std-388-tolb", "388", "0100000", net="30.09", vat="4.50", gross="34.59"),
      lines(3, "10.03", "1.50"))
print("ok")
