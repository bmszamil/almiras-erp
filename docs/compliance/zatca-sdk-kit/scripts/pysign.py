"""Proof: sign a simplified invoice with Python only (lxml + cryptography), mimicking the SDK 2.0.3 output.
Usage: python3 -I pysign.py unsigned.xml key.pem cert_b64.txt out.xml qr_layout(sdk|dtg) issuer_style(sun|bc)
Uses only the SDK's bundled DUMMY key/cert and fake invoice data."""
import sys, base64, hashlib, datetime, re
from lxml import etree
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils as ecutils

src, keyf, certf, outf, layout, issuer_style = sys.argv[1:7]
NS = {"ext": "urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2",
      "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
      "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"}
DS, XADES = "http://www.w3.org/2000/09/xmldsig#", "http://uri.etsi.org/01903/v1.3.2#"

raw = open(src, "rb").read()


def invoice_hash(data):
    root = etree.fromstring(data)
    for xp in ["//ext:UBLExtensions", "/*/cac:Signature", "//cac:AdditionalDocumentReference[cbc:ID='QR']"]:
        for el in root.xpath(xp, namespaces=NS):
            p, prev, tail = el.getparent(), el.getprevious(), el.tail
            if tail:
                if prev is not None: prev.tail = (prev.tail or "") + tail
                else: p.text = (p.text or "") + tail
            p.remove(el)
    return hashlib.sha256(etree.tostring(root, method="c14n")).digest()


digest = invoice_hash(raw)
h64 = base64.b64encode(digest).decode()

pem = open(keyf).read().strip()
if "BEGIN" not in pem:  # SDK stores the key without header/footer
    pem = "-----BEGIN EC PRIVATE KEY-----\n" + pem + "\n-----END EC PRIVATE KEY-----\n"
key = serialization.load_pem_private_key(pem.encode(), None)
cert_b64 = open(certf).read().strip()
cert = x509.load_der_x509_certificate(base64.b64decode(cert_b64))

# what the SDK signs: SHA256withECDSA over the raw 32-byte invoice hash
sig_der = key.sign(digest, ec.ECDSA(hashes.SHA256()))
sig64 = base64.b64encode(sig_der).decode()

cert_digest = base64.b64encode(hashlib.sha256(cert_b64.encode()).hexdigest().encode()).decode()
if issuer_style == "sun":
    issuer = ", ".join(f"{a.oid._name if a.oid._name != 'domainComponent' else 'DC'}={a.value}"
                       .replace("commonName", "CN") for a in reversed(list(cert.issuer)))
else:  # BouncyCastle X509Name.toString style seen with BC as first JCA provider
    issuer = ",".join(f"{'CN' if a.oid._name == 'commonName' else 'DC'}={a.value}" for a in cert.issuer)
signing_time = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
sp_inner = (f'<xades:SignedSignatureProperties><xades:SigningTime>{signing_time}</xades:SigningTime>'
            f'<xades:SigningCertificate><xades:Cert><xades:CertDigest>'
            f'<ds:DigestMethod{{D}} Algorithm="http://www.w3.org/2001/04/xmlenc#sha256"/>'
            f'<ds:DigestValue{{D}}>{cert_digest}</ds:DigestValue></xades:CertDigest>'
            f'<xades:IssuerSerial><ds:X509IssuerName{{D}}>{issuer}</ds:X509IssuerName>'
            f'<ds:X509SerialNumber{{D}}>{cert.serial_number}</ds:X509SerialNumber></xades:IssuerSerial>'
            f'</xades:Cert></xades:SigningCertificate></xades:SignedSignatureProperties>')
sp_in_doc = '<xades:SignedProperties Id="xadesSignedProperties">' + sp_inner.replace("{D}", "") + '</xades:SignedProperties>'
# digest form = how dom4j asXML() re-serialises the detached element (own ns declarations on each element)
sp_for_digest = (f'<xades:SignedProperties xmlns:xades="{XADES}" Id="xadesSignedProperties">'
                 + sp_inner.replace("{D}", f' xmlns:ds="{DS}"') + '</xades:SignedProperties>')
sp_digest = base64.b64encode(hashlib.sha256(sp_for_digest.encode()).hexdigest().encode()).decode()

ubl_ext = f'''<ext:UBLExtensions><ext:UBLExtension><ext:ExtensionURI>urn:oasis:names:specification:ubl:dsig:enveloped:xades</ext:ExtensionURI><ext:ExtensionContent><sig:UBLDocumentSignatures xmlns:sac="urn:oasis:names:specification:ubl:schema:xsd:SignatureAggregateComponents-2" xmlns:sbc="urn:oasis:names:specification:ubl:schema:xsd:SignatureBasicComponents-2" xmlns:sig="urn:oasis:names:specification:ubl:schema:xsd:CommonSignatureComponents-2"><sac:SignatureInformation><cbc:ID>urn:oasis:names:specification:ubl:signature:1</cbc:ID><sbc:ReferencedSignatureID>urn:oasis:names:specification:ubl:signature:Invoice</sbc:ReferencedSignatureID><ds:Signature xmlns:ds="{DS}" Id="signature"><ds:SignedInfo><ds:CanonicalizationMethod Algorithm="http://www.w3.org/2006/12/xml-c14n11"/><ds:SignatureMethod Algorithm="http://www.w3.org/2001/04/xmldsig-more#ecdsa-sha256"/><ds:Reference Id="invoiceSignedData" URI=""><ds:Transforms><ds:Transform Algorithm="http://www.w3.org/TR/1999/REC-xpath-19991116"><ds:XPath>not(//ancestor-or-self::ext:UBLExtensions)</ds:XPath></ds:Transform><ds:Transform Algorithm="http://www.w3.org/TR/1999/REC-xpath-19991116"><ds:XPath>not(//ancestor-or-self::cac:Signature)</ds:XPath></ds:Transform><ds:Transform Algorithm="http://www.w3.org/TR/1999/REC-xpath-19991116"><ds:XPath>not(//ancestor-or-self::cac:AdditionalDocumentReference[cbc:ID='QR'])</ds:XPath></ds:Transform><ds:Transform Algorithm="http://www.w3.org/2006/12/xml-c14n11"/></ds:Transforms><ds:DigestMethod Algorithm="http://www.w3.org/2001/04/xmlenc#sha256"/><ds:DigestValue>{h64}</ds:DigestValue></ds:Reference><ds:Reference Type="http://www.w3.org/2000/09/xmldsig#SignatureProperties" URI="#xadesSignedProperties"><ds:DigestMethod Algorithm="http://www.w3.org/2001/04/xmlenc#sha256"/><ds:DigestValue>{sp_digest}</ds:DigestValue></ds:Reference></ds:SignedInfo><ds:SignatureValue>{sig64}</ds:SignatureValue><ds:KeyInfo><ds:X509Data><ds:X509Certificate>{cert_b64}</ds:X509Certificate></ds:X509Data></ds:KeyInfo><ds:Object><xades:QualifyingProperties xmlns:xades="{XADES}" Target="signature">{sp_in_doc}</xades:QualifyingProperties></ds:Object></ds:Signature></sac:SignatureInformation></sig:UBLDocumentSignatures></ext:ExtensionContent></ext:UBLExtension></ext:UBLExtensions>'''

root = etree.fromstring(raw)
g = lambda xp: root.xpath(xp, namespaces=NS)[0].text
seller, vat = g("/*/cac:AccountingSupplierParty/cac:Party/cac:PartyLegalEntity/cbc:RegistrationName"), \
    g("/*/cac:AccountingSupplierParty/cac:Party/cac:PartyTaxScheme/cbc:CompanyID")
ts = g("/*/cbc:IssueDate") + "T" + g("/*/cbc:IssueTime") + "Z"
total, vat_total = g("/*/cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount"), g("/*/cac:TaxTotal/cbc:TaxAmount")
spki = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def tlv(tag, v):
    assert len(v) <= 255
    return bytes([tag, len(v)]) + v


def i2b(n):  # java BigInteger.toByteArray (signed, minimal)
    return n.to_bytes(n.bit_length() // 8 + 1, "big", signed=False) if n.bit_length() % 8 == 0 else n.to_bytes((n.bit_length() + 7) // 8, "big")


r, s = ecutils.decode_dss_signature(sig_der)
base = b"".join(tlv(t, v.encode()) for t, v in [(1, seller), (2, vat), (3, ts), (4, total), (5, vat_total), (6, h64)])
if layout == "sdk":
    qr = base + tlv(7, spki) + tlv(8, i2b(r)) + tlv(9, i2b(s))
else:  # DTG layout: 7 signature b64 text, 8 SPKI DER, 9 CA signature DER
    qr = base + tlv(7, sig64.encode()) + tlv(8, spki) + tlv(9, cert.signature)
qr64 = base64.b64encode(qr).decode()

txt = raw.decode("utf-8")
txt = re.sub(r"(<Invoice[^>]*>)", r"\1" + ubl_ext.replace("\\", "\\\\"), txt, count=1)
qr_adr = f'<cac:AdditionalDocumentReference><cbc:ID>QR</cbc:ID><cac:Attachment><cbc:EmbeddedDocumentBinaryObject mimeCode="text/plain">{qr64}</cbc:EmbeddedDocumentBinaryObject></cac:Attachment></cac:AdditionalDocumentReference><cac:Signature><cbc:ID>urn:oasis:names:specification:ubl:signature:Invoice</cbc:ID><cbc:SignatureMethod>urn:oasis:names:specification:ubl:dsig:enveloped:xades</cbc:SignatureMethod></cac:Signature>'
txt = txt.replace("<cac:AccountingSupplierParty>", qr_adr + "<cac:AccountingSupplierParty>", 1)
open(outf, "w", encoding="utf-8").write(txt)
assert base64.b64encode(invoice_hash(txt.encode())).decode() == h64, "hash changed by insertion"
print("hash", h64, "| qr bytes", len(qr), "| layout", layout, "| issuer", issuer)
