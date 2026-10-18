"""UBL 2.1 XML for ZATCA (388 / 381 / 383, root ``Invoice`` for all; zatca §3.4, R-14, R-19, R-31, R-40, R-48).

Pure functions on :class:`InvoiceData`; ``zatca.services`` builds that data from an issued
``invoicing.SalesDocument`` and runs the local checks first. The signature block is assembled as text
exactly as the proof kit did (``docs/compliance/zatca-sdk-kit/scripts/pysign.py``), so the bytes of
``xades:SignedProperties`` that are digested are the bytes written into the document (R-14, AT-66).
"""
import base64
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from lxml import etree

from . import crypto

INV, CAC, CBC, EXT = (crypto.NS[k] for k in ("inv", "cac", "cbc", "ext"))
DS, XADES = crypto.NS["ds"], crypto.NS["xades"]
TWO = Decimal("0.01")
RIYADH = ZoneInfo("Asia/Riyadh")


@dataclass
class Party:
    name: str = ""
    vat_number: str = ""
    id_scheme: str = ""
    id_value: str = ""
    street: str = ""
    building_no: str = ""
    additional_no: str = ""
    district: str = ""
    city: str = ""
    postal_code: str = ""
    region: str = ""  # CountrySubentity (BT-39/54): optional; required only by SDK 2.0.3's 2021 BR-KSA-10
    country: str = ""
    # KSA-40 buyer CompanyLegalForm: "GOV" for a government buyer under VAT IR Art. 20(5) (B2G), else empty.
    # Emitted on standard documents only (R4.0.0 rules BR-KSA-91 / -92; findings 2026-10-17 §7.3).
    legal_form: str = ""


@dataclass
class Line:
    line_no: int
    name: str
    quantity: Decimal
    unit_code: str
    unit_price: Decimal
    discount: Decimal
    net: Decimal
    vat: Decimal
    category: str
    rate: Decimal
    amount_with_vat: Decimal | None = None  # KSA-12; net + vat unless a test sets it


@dataclass
class TaxGroup:
    category: str
    rate: Decimal
    taxable: Decimal
    vat: Decimal
    exemption_codes: tuple = ()
    exemption_text: str = ""


@dataclass
class PrepaymentLine:
    """One prepayment deduction line of a final 388 (R-39; SDK R3.4.8 findings §3.4). Validated by
    ``zatca.rules.prepayment_problems`` only: not emitted until the invoicing prepayment flow exists (R-39)."""
    number: str  # KSA-26: the 386's invoice number (BT-1)
    uuid: str  # the 386's UUID (cbc:UUID in the line DocumentReference; every SDK sample carries it)
    issue_date: str  # KSA-28, as emitted on the 386
    issue_time: str  # KSA-29, as emitted on the 386
    type_code: str  # KSA-30, must be 386 (BR-KSA-74)
    taxable: Decimal | None  # KSA-31
    vat: Decimal | None  # KSA-32 = round(KSA-31 x KSA-34 / 100, 2) (BR-KSA-79)
    category: str  # KSA-33
    rate: Decimal | None  # KSA-34
    # BR-KSA-82: these line amounts are all 0 on a deduction line.
    quantity: Decimal = Decimal("0")
    price: Decimal = Decimal("0")
    net: Decimal = Decimal("0")
    allowance: Decimal = Decimal("0")
    charge: Decimal = Decimal("0")
    line_vat: Decimal = Decimal("0")
    line_amount_with_vat: Decimal = Decimal("0")


@dataclass
class InvoiceData:
    number: str
    uuid: str
    issue: datetime  # aware; emitted as Asia/Riyadh local date and time (KSA-25 "hh:mm:ss" in AST, no suffix)
    type_code: str  # 388 / 381 / 383
    category: str  # standard / simplified
    subtype_code: str
    currency: str
    currency_places: int
    seller: Party
    buyer: Party
    lines: list
    groups: list
    total_net: Decimal
    total_vat: Decimal
    total_gross: Decimal
    total_vat_sar: Decimal
    supply_date: date | None = None
    supply_end_date: date | None = None
    payment_means_code: str = ""
    billing_reference: str = ""  # BT-25: the original's invoice number (not a UUID)
    billing_reference_entity_ok: bool = True
    billing_reference_category: str = ""
    billing_reference_pre_go_live: bool = False  # the original was issued before go-live (no e-invoice, R-19)
    reason: str = ""  # KSA-10, Arabic required
    flags: dict = field(default_factory=dict)  # third_party, nominal, export, summary, self_billed
    retention_class: str = "standard"
    acquisition_date: date | None = None
    attachments: list = field(default_factory=list)  # [(id, base64 text)] extra AdditionalDocumentReference
    prepaid_amount: Decimal = Decimal("0")  # BT-113; non-zero only with prepayment lines (R-39, not built yet)
    prepayments: list = field(default_factory=list)  # [PrepaymentLine] (R-39; validated, not emitted yet)
    # BT-7 TaxPointDate: for a B2G supply (buyer legal form GOV) the payment order date (BR-KSA-92; Art. 20(5)).
    tax_point_date: date | None = None

    @property
    def payable(self):
        """BT-115 PayableAmount = BT-112 - BT-113 (BR-CO-16); QR tag 4 (R-15; SDK R3.4.8 findings §3.1)."""
        return Decimal(self.total_gross) - Decimal(self.prepaid_amount)

    @property
    def qr_vat_total(self):
        """QR tag 5 (owner decision G2; R-40, OQ-22): the VAT total in SAR, BT-111, i.e. the first ``TaxTotal`` of the
        document as both SDK generators read it. For a SAR document it equals BT-110."""
        return Decimal(self.total_vat_sar)

    @property
    def issue_local(self):
        """The issue moment in Riyadh time. Coordinator decision 2026-10-17, pending the owner's confirmation in an
        OQ-18 amendment: BT-2 / KSA-25 are emitted in AST without a suffix (XML v1.2 p.47 allows ``hh:mm:ss`` in
        AST), so the XML issue date is the Riyadh calendar date of the document (invoicing R78)."""
        if self.issue.tzinfo is None:
            raise ValueError("InvoiceData.issue must be timezone-aware")
        return self.issue.astimezone(RIYADH)

    @property
    def issue_date_text(self):
        return self.issue_local.strftime("%Y-%m-%d")

    @property
    def issue_time_text(self):
        return self.issue_local.strftime("%H:%M:%S")

    @property
    def qr_timestamp(self):
        """QR tag 3 = ``IssueDate "T" IssueTime`` of the emitted XML, **without** ``Z`` (R-15).

        Settled offline by SDK R3.4.8 and R4.0.0 (findings 2026-10-17 §7.3): both generators (``-sign``, ``-qr``)
        write this form, and both validators print the warning ``timeStamp`` for anything else (``...Z``,
        ``+03:00``, date only, another time). The warning is only printed when another QR check fails, so
        ``zatca/test_sdk_offline.py`` forces one (broken tag 7) to make a mismatch visible. IssueTime is AST with no
        suffix (KSA-25), so tag 3 is that AST text."""
        return f"{self.issue_date_text}T{self.issue_time_text}"


def subtype_code(category, flags):
    """KSA-2 ``NNPNESB``: 01 standard / 02 simplified, then P third party, N nominal, E export, S summary,
    B self-billed (BR-KSA-06)."""
    head = "01" if category == "standard" else "02"
    return head + "".join("1" if flags.get(name) else "0"
                          for name in ("third_party", "nominal", "export", "summary", "self_billed"))


def amount(value: Decimal) -> str:
    """Amounts are emitted with 2 decimals (R-51: a non-zero third decimal is refused before this)."""
    return f"{Decimal(value).quantize(TWO):.2f}"


def price(value: Decimal, places: int) -> str:
    q = Decimal(1).scaleb(-max(2, places))
    text = f"{Decimal(value).quantize(q):f}"
    if places > 2 and text.endswith("0"):
        text = text[:-1]
    return text


def _sub(parent, ns, tag, text=None, **attrs):
    el = etree.SubElement(parent, f"{{{ns}}}{tag}", **attrs)
    if text is not None:
        el.text = str(text)
    return el


def _address(parent, p):
    addr = _sub(parent, CAC, "PostalAddress")
    if p.street:
        _sub(addr, CBC, "StreetName", p.street)
    if p.building_no:
        _sub(addr, CBC, "BuildingNumber", p.building_no)
    if p.additional_no:  # KSA-23 optional (DD KSA-23 "O")
        _sub(addr, CBC, "PlotIdentification", p.additional_no)
    if p.district:
        _sub(addr, CBC, "CitySubdivisionName", p.district)
    if p.city:
        _sub(addr, CBC, "CityName", p.city)
    if p.postal_code:
        _sub(addr, CBC, "PostalZone", p.postal_code)
    if p.region:
        _sub(addr, CBC, "CountrySubentity", p.region)
    country = _sub(addr, CAC, "Country")
    _sub(country, CBC, "IdentificationCode", p.country or "SA")


def _tax_scheme(parent):
    _sub(_sub(parent, CAC, "TaxScheme"), CBC, "ID", "VAT")


def build_unsigned(data: InvoiceData, icv: int, pih: str) -> bytes:
    """The unsigned UBL document with ICV and PIH; QR and signature are added by :func:`sign`."""
    if data.prepayments:  # R-39: deduction lines are validated (rules.prepayment_problems) but not built yet
        raise NotImplementedError("prepayment deduction lines are not built until invoicing prepayments exist (R-39)")
    root = etree.Element(f"{{{INV}}}Invoice", nsmap={None: INV, "cac": CAC, "cbc": CBC, "ext": EXT})
    cur = data.currency
    _sub(root, CBC, "ProfileID", "reporting:1.0")
    _sub(root, CBC, "ID", data.number)
    _sub(root, CBC, "UUID", data.uuid)
    _sub(root, CBC, "IssueDate", data.issue_date_text)
    _sub(root, CBC, "IssueTime", data.issue_time_text)  # KSA-25 in AST, no suffix (OQ-18 amendment pending)
    _sub(root, CBC, "InvoiceTypeCode", data.type_code, name=data.subtype_code)  # KSA-2: always 7 positions
    if data.category == "standard" and data.tax_point_date:  # BT-7, B2G only (R-54); UBL order: before BT-5
        _sub(root, CBC, "TaxPointDate", data.tax_point_date.isoformat())
    _sub(root, CBC, "DocumentCurrencyCode", cur)
    _sub(root, CBC, "TaxCurrencyCode", "SAR")  # R-40: every document, SAR included
    if data.billing_reference:
        ref = _sub(_sub(root, CAC, "BillingReference"), CAC, "InvoiceDocumentReference")
        _sub(ref, CBC, "ID", data.billing_reference)
    adr = _sub(root, CAC, "AdditionalDocumentReference")
    _sub(adr, CBC, "ID", "ICV")
    _sub(adr, CBC, "UUID", str(icv))
    adr = _sub(root, CAC, "AdditionalDocumentReference")
    _sub(adr, CBC, "ID", "PIH")
    _sub(_sub(adr, CAC, "Attachment"), CBC, "EmbeddedDocumentBinaryObject", pih, mimeCode="text/plain")
    for ident, b64 in data.attachments:
        adr = _sub(root, CAC, "AdditionalDocumentReference")
        _sub(adr, CBC, "ID", ident)
        _sub(_sub(adr, CAC, "Attachment"), CBC, "EmbeddedDocumentBinaryObject", b64, mimeCode="application/pdf",
             filename=f"{ident}.pdf")

    seller = _sub(_sub(root, CAC, "AccountingSupplierParty"), CAC, "Party")
    pid = _sub(seller, CAC, "PartyIdentification")  # R-48: exactly one seller identification
    _sub(pid, CBC, "ID", data.seller.id_value, schemeID=data.seller.id_scheme)
    _address(seller, data.seller)
    pts = _sub(seller, CAC, "PartyTaxScheme")
    _sub(pts, CBC, "CompanyID", data.seller.vat_number)
    _tax_scheme(pts)
    _sub(_sub(seller, CAC, "PartyLegalEntity"), CBC, "RegistrationName", data.seller.name)

    buyer = _sub(_sub(root, CAC, "AccountingCustomerParty"), CAC, "Party")
    b = data.buyer
    if data.category == "standard":
        if b.id_value and not b.vat_number:
            _sub(_sub(buyer, CAC, "PartyIdentification"), CBC, "ID", b.id_value, schemeID=b.id_scheme)
        _address(buyer, b)
        if b.vat_number:
            pts = _sub(buyer, CAC, "PartyTaxScheme")
            _sub(pts, CBC, "CompanyID", b.vat_number)
            _tax_scheme(pts)
        ple = _sub(buyer, CAC, "PartyLegalEntity")
        _sub(ple, CBC, "RegistrationName", b.name)
        if b.legal_form:  # KSA-40 (R-54); UBL order: after RegistrationName
            _sub(ple, CBC, "CompanyLegalForm", b.legal_form)
    elif b.id_scheme == "NAT" and b.id_value:  # BR-KSA-49 / BR-KSA-25: citizen education / healthcare
        _sub(_sub(buyer, CAC, "PartyIdentification"), CBC, "ID", b.id_value, schemeID="NAT")
        _sub(_sub(buyer, CAC, "PartyLegalEntity"), CBC, "RegistrationName", b.name)

    if data.supply_date:
        delivery = _sub(root, CAC, "Delivery")
        _sub(delivery, CBC, "ActualDeliveryDate", data.supply_date.isoformat())
        if data.supply_end_date:
            _sub(delivery, CBC, "LatestDeliveryDate", data.supply_end_date.isoformat())
    if data.payment_means_code or data.reason or data.category == "standard":
        # BR-KSA-16 (SDK 2.0.3 rules): a standard document carries BT-81; without a code from invoicing it is
        # 1 "instrument not defined". Simplified documents carry it only when given (or for a note, R-19).
        pm = _sub(root, CAC, "PaymentMeans")
        _sub(pm, CBC, "PaymentMeansCode", data.payment_means_code or "1")
        if data.reason:  # KSA-10 (BR-KSA-17)
            _sub(pm, CBC, "InstructionNote", data.reason)

    # R-40 as amended (owner decision G2): BT-111, the VAT total in SAR without subtotals, first, as in ZATCA's SDK
    # samples (the FX sample included); then the document-currency TaxTotal with subtotals (BT-110). The SDK
    # generators take QR tag 5 from the first TaxTotal, so tag 5 is the SAR total (qr_vat_total).
    tt = _sub(root, CAC, "TaxTotal")
    _sub(tt, CBC, "TaxAmount", amount(data.total_vat_sar), currencyID="SAR")
    tt = _sub(root, CAC, "TaxTotal")
    _sub(tt, CBC, "TaxAmount", amount(data.total_vat), currencyID=cur)
    for g in data.groups:
        st = _sub(tt, CAC, "TaxSubtotal")
        _sub(st, CBC, "TaxableAmount", amount(g.taxable), currencyID=cur)
        _sub(st, CBC, "TaxAmount", amount(g.vat), currencyID=cur)
        tc = _sub(st, CAC, "TaxCategory")
        _sub(tc, CBC, "ID", g.category)
        _sub(tc, CBC, "Percent", f"{Decimal(g.rate).quantize(TWO):.2f}")
        if g.category != "S" and g.exemption_codes:
            _sub(tc, CBC, "TaxExemptionReasonCode", g.exemption_codes[0])
            _sub(tc, CBC, "TaxExemptionReason", g.exemption_text)
        _tax_scheme(tc)

    lmt = _sub(root, CAC, "LegalMonetaryTotal")
    line_sum = sum((line.net for line in data.lines), Decimal("0"))
    _sub(lmt, CBC, "LineExtensionAmount", amount(line_sum), currencyID=cur)
    _sub(lmt, CBC, "TaxExclusiveAmount", amount(data.total_net), currencyID=cur)
    _sub(lmt, CBC, "TaxInclusiveAmount", amount(data.total_gross), currencyID=cur)
    _sub(lmt, CBC, "AllowanceTotalAmount", amount(0), currencyID=cur)
    _sub(lmt, CBC, "PrepaidAmount", amount(data.prepaid_amount), currencyID=cur)  # BT-113
    _sub(lmt, CBC, "PayableAmount", amount(data.payable), currencyID=cur)  # BT-115 (QR tag 4)

    for line in data.lines:
        il = _sub(root, CAC, "InvoiceLine")
        _sub(il, CBC, "ID", line.line_no)
        _sub(il, CBC, "InvoicedQuantity", f"{Decimal(line.quantity):f}", unitCode=line.unit_code)
        _sub(il, CBC, "LineExtensionAmount", amount(line.net), currencyID=cur)
        if line.discount:
            ac = _sub(il, CAC, "AllowanceCharge")
            _sub(ac, CBC, "ChargeIndicator", "false")
            _sub(ac, CBC, "AllowanceChargeReason", "discount")
            _sub(ac, CBC, "Amount", amount(line.discount), currencyID=cur)
        ltt = _sub(il, CAC, "TaxTotal")
        _sub(ltt, CBC, "TaxAmount", amount(line.vat), currencyID=cur)  # KSA-11
        with_vat = line.amount_with_vat if line.amount_with_vat is not None else line.net + line.vat
        _sub(ltt, CBC, "RoundingAmount", amount(with_vat), currencyID=cur)  # KSA-12
        item = _sub(il, CAC, "Item")
        _sub(item, CBC, "Name", line.name)
        ctc = _sub(item, CAC, "ClassifiedTaxCategory")
        _sub(ctc, CBC, "ID", line.category)
        _sub(ctc, CBC, "Percent", f"{Decimal(line.rate).quantize(TWO):.2f}")
        _tax_scheme(ctc)
        _sub(_sub(il, CAC, "Price"), CBC, "PriceAmount", price(line.unit_price, data.currency_places), currencyID=cur)

    etree.indent(root, space="  ")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + etree.tostring(root, encoding="UTF-8") + b"\n"


# --- signature and QR (R-14, R-15) ----------------------------------------------------------------

def signed_properties(signing_time: str, cert_digest: str, issuer: str, serial: int, *, for_digest=False) -> str:
    """``xades:SignedProperties`` with no inter-element whitespace and ``/>`` for empty elements (R-14).

    ``for_digest=False``: the form written in the document (namespaces inherited from QualifyingProperties /
    ds:Signature). ``for_digest=True``: the form that is digested, i.e. dom4j ``asXML()`` of the detached
    element: ``xmlns:xades`` on SignedProperties and ``xmlns:ds`` on every ``ds:`` child (findings §3; proven
    with SDK 2.0.3). Writing the declared form into the document fails SDK 2.0.3
    (``wrong xadesSignedPropertiesDigestValue``, checked 2026-10-17), so the two forms differ only by those
    declarations; see :func:`signed_properties_digest_form`.
    """
    d = f' xmlns:ds="{DS}"' if for_digest else ""
    x = f' xmlns:xades="{XADES}"' if for_digest else ""
    return (f'<xades:SignedProperties{x} Id="xadesSignedProperties">'
            f'<xades:SignedSignatureProperties><xades:SigningTime>{signing_time}</xades:SigningTime>'
            f'<xades:SigningCertificate><xades:Cert><xades:CertDigest>'
            f'<ds:DigestMethod{d} Algorithm="http://www.w3.org/2001/04/xmlenc#sha256"/>'
            f'<ds:DigestValue{d}>{cert_digest}</ds:DigestValue></xades:CertDigest>'
            f'<xades:IssuerSerial><ds:X509IssuerName{d}>{_text(issuer)}</ds:X509IssuerName>'
            f'<ds:X509SerialNumber{d}>{serial}</ds:X509SerialNumber></xades:IssuerSerial>'
            f'</xades:Cert></xades:SigningCertificate></xades:SignedSignatureProperties></xades:SignedProperties>')


def signed_properties_digest_form(written: bytes) -> bytes:
    """The digested form of SignedProperties bytes as written in a document: add ``xmlns:xades`` on the element
    and ``xmlns:ds`` on each ``ds:`` element (AT-66)."""
    text = written.decode("utf-8")
    text = text.replace("<xades:SignedProperties ", f'<xades:SignedProperties xmlns:xades="{XADES}" ', 1)
    text = re.sub(r"<ds:(\w+)", lambda m: f'<ds:{m.group(1)} xmlns:ds="{DS}"', text)
    return text.encode("utf-8")


def _text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def ubl_extensions(invoice_hash: str, signature_b64: str, cert_b64: str, sp: str, sp_digest_form: str) -> str:
    sp_digest = crypto.hex_digest_b64(sp_digest_form.encode("utf-8"))
    xpath = "http://www.w3.org/TR/1999/REC-xpath-19991116"
    c14n11 = "http://www.w3.org/2006/12/xml-c14n11"
    sha256 = "http://www.w3.org/2001/04/xmlenc#sha256"
    return (
        '<ext:UBLExtensions><ext:UBLExtension><ext:ExtensionURI>urn:oasis:names:specification:ubl:dsig:enveloped:xades'
        '</ext:ExtensionURI><ext:ExtensionContent><sig:UBLDocumentSignatures '
        'xmlns:sac="urn:oasis:names:specification:ubl:schema:xsd:SignatureAggregateComponents-2" '
        'xmlns:sbc="urn:oasis:names:specification:ubl:schema:xsd:SignatureBasicComponents-2" '
        'xmlns:sig="urn:oasis:names:specification:ubl:schema:xsd:CommonSignatureComponents-2">'
        '<sac:SignatureInformation><cbc:ID>urn:oasis:names:specification:ubl:signature:1</cbc:ID>'
        '<sbc:ReferencedSignatureID>urn:oasis:names:specification:ubl:signature:Invoice</sbc:ReferencedSignatureID>'
        f'<ds:Signature xmlns:ds="{DS}" Id="signature"><ds:SignedInfo>'
        f'<ds:CanonicalizationMethod Algorithm="{c14n11}"/>'
        '<ds:SignatureMethod Algorithm="http://www.w3.org/2001/04/xmldsig-more#ecdsa-sha256"/>'
        '<ds:Reference Id="invoiceSignedData" URI=""><ds:Transforms>'
        f'<ds:Transform Algorithm="{xpath}"><ds:XPath>not(//ancestor-or-self::ext:UBLExtensions)</ds:XPath></ds:Transform>'
        f'<ds:Transform Algorithm="{xpath}"><ds:XPath>not(//ancestor-or-self::cac:Signature)</ds:XPath></ds:Transform>'
        f'<ds:Transform Algorithm="{xpath}"><ds:XPath>not(//ancestor-or-self::cac:AdditionalDocumentReference'
        "[cbc:ID='QR'])</ds:XPath></ds:Transform>"
        f'<ds:Transform Algorithm="{c14n11}"/></ds:Transforms>'
        f'<ds:DigestMethod Algorithm="{sha256}"/><ds:DigestValue>{invoice_hash}</ds:DigestValue></ds:Reference>'
        '<ds:Reference Type="http://www.w3.org/2000/09/xmldsig#SignatureProperties" URI="#xadesSignedProperties">'
        f'<ds:DigestMethod Algorithm="{sha256}"/><ds:DigestValue>{sp_digest}</ds:DigestValue></ds:Reference>'
        f'</ds:SignedInfo><ds:SignatureValue>{signature_b64}</ds:SignatureValue>'
        f'<ds:KeyInfo><ds:X509Data><ds:X509Certificate>{cert_b64}</ds:X509Certificate></ds:X509Data></ds:KeyInfo>'
        f'<ds:Object><xades:QualifyingProperties xmlns:xades="{XADES}" Target="signature">{sp}'
        '</xades:QualifyingProperties></ds:Object></ds:Signature></sac:SignatureInformation>'
        '</sig:UBLDocumentSignatures></ext:ExtensionContent></ext:UBLExtension></ext:UBLExtensions>'
    )


QR_ADR = ('<cac:AdditionalDocumentReference><cbc:ID>QR</cbc:ID><cac:Attachment>'
          '<cbc:EmbeddedDocumentBinaryObject mimeCode="text/plain">{qr}</cbc:EmbeddedDocumentBinaryObject>'
          '</cac:Attachment></cac:AdditionalDocumentReference>')
UBL_SIGNATURE = ('<cac:Signature><cbc:ID>urn:oasis:names:specification:ubl:signature:Invoice</cbc:ID>'
                 '<cbc:SignatureMethod>urn:oasis:names:specification:ubl:dsig:enveloped:xades</cbc:SignatureMethod>'
                 '</cac:Signature>')


def assemble(unsigned: bytes, extensions: str, qr_b64: str | None) -> bytes:
    """Insert UBLExtensions as the first child of ``Invoice``, the QR reference (if any) and ``cac:Signature``
    before ``cac:AccountingSupplierParty``, i.e. after the last AdditionalDocumentReference (R-14)."""
    text = unsigned.decode("utf-8")
    start = text.index(">", text.index("<Invoice")) + 1
    text = text[:start] + extensions + text[start:]
    marker = "<cac:AccountingSupplierParty>"
    insert = (QR_ADR.format(qr=qr_b64) if qr_b64 else "") + UBL_SIGNATURE
    text = text.replace(marker, insert + marker, 1)
    return text.encode("utf-8")


def find_text(xml: bytes, xpath: str) -> str:
    root = etree.fromstring(xml, crypto._parser())
    found = root.xpath(xpath, namespaces=crypto.NS)
    if not found:
        return ""
    el = found[0]
    return el if isinstance(el, str) else (el.text or "")


def qr_from_xml(xml: bytes) -> str:
    return find_text(xml, "/*/cac:AdditionalDocumentReference[cbc:ID='QR']/cac:Attachment/"
                          "cbc:EmbeddedDocumentBinaryObject").strip()


def signed_properties_bytes(signed_xml: bytes) -> bytes:
    """The bytes of ``xades:SignedProperties`` exactly as written in ``signed_xml`` (AT-66)."""
    start = signed_xml.index(b"<xades:SignedProperties")
    end = signed_xml.index(b"</xades:SignedProperties>") + len(b"</xades:SignedProperties>")
    return signed_xml[start:end]


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
