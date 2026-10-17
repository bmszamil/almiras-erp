"""Analyse a signed ZATCA invoice (SDK sample, SDK -sign output or module output) against the R-13/R-14/R-15 encodings.

Usage: python3 -I analyse3.py signed.xml [...]
Prints one block per file: hash, what SignatureValue verifies against, CertDigest / SignedProperties digest form,
SigningTime form, X509IssuerName, QR TLV tags (BER lengths) and what each tag 1-9 equals.
"""
import base64
import hashlib
import re
import sys

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils as ecutils
from lxml import etree

NS = {"ext": "urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2",
      "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
      "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
      "ds": "http://www.w3.org/2000/09/xmldsig#", "xades": "http://uri.etsi.org/01903/v1.3.2#"}


def invoice_hash(data):
    root = etree.fromstring(data, etree.XMLParser(remove_comments=False, resolve_entities=False))
    for xp in ["//ext:UBLExtensions", "/*/cac:Signature", "//cac:AdditionalDocumentReference[cbc:ID='QR']"]:
        for el in root.xpath(xp, namespaces=NS):
            p, prev, tail = el.getparent(), el.getprevious(), el.tail
            if tail:
                if prev is not None:
                    prev.tail = (prev.tail or "") + tail
                else:
                    p.text = (p.text or "") + tail
            p.remove(el)
    c = etree.tostring(root.getroottree(), method="c14n", with_comments=False)  # comments dropped, as the SDK does
    return base64.b64encode(hashlib.sha256(c).digest()).decode()


def ber_tlv(buf):
    i, out = 0, []
    while i < len(buf):
        tag = buf[i]
        ln = buf[i + 1]
        j = i + 2
        form = "short"
        if ln & 0x80:
            n = ln & 0x7F
            ln = int.from_bytes(buf[j:j + n], "big")
            j += n
            form = f"long(0x{0x80 | n:02x})"
        out.append((tag, ln, form, buf[j:j + ln]))
        i = j + ln
    return out


def first(tree, xp):
    r = tree.xpath(xp, namespaces=NS)
    if not r:
        return None
    return r[0] if isinstance(r[0], str) else (r[0].text or "")


def analyse(path):
    raw = open(path, "rb").read()
    tree = etree.fromstring(raw)
    print(f"=== {path.rsplit('/', 1)[-1]}")
    h = invoice_hash(raw)
    sig = tree.find(".//ds:Signature", NS)
    if sig is None:
        print("  no ds:Signature")
        return
    d_inv = sig.find("ds:SignedInfo/ds:Reference[@Id='invoiceSignedData']/ds:DigestValue", NS).text
    print(f"  hash {h} == Reference DigestValue: {h == d_inv}")
    sv64 = sig.find("ds:SignatureValue", NS).text.strip()
    sv = base64.b64decode(sv64)
    cert_b64 = sig.find("ds:KeyInfo/ds:X509Data/ds:X509Certificate", NS).text.strip()
    cert = x509.load_der_x509_certificate(base64.b64decode(cert_b64))
    pub = cert.public_key()
    r, s = ecutils.decode_dss_signature(sv)
    print(f"  SignatureValue: DER SEQUENCE(r,s) len {len(sv)} first 0x{sv[0]:02x}; curve {pub.curve.name}")
    ok = []
    for name, data in {"raw32 hash": base64.b64decode(h), "b64 hash text": h.encode(),
                       "hex hash text": base64.b64decode(h).hex().encode()}.items():
        try:
            pub.verify(sv, data, ec.ECDSA(hashes.SHA256()))
            ok.append(name)
        except InvalidSignature:
            pass
    try:
        pub.verify(sv, base64.b64decode(h), ec.ECDSA(ecutils.Prehashed(hashes.SHA256())))
        ok.append("prehashed raw32")
    except InvalidSignature:
        pass
    print(f"  SignatureValue verifies (SHA256withECDSA) over: {ok or 'NONE'}")
    cd = sig.find(".//xades:CertDigest/ds:DigestValue", NS).text
    forms = []
    for name, data in {"DER": base64.b64decode(cert_b64), "b64 text": cert_b64.encode()}.items():
        dg = hashlib.sha256(data)
        if base64.b64encode(dg.digest()).decode() == cd:
            forms.append(f"b64(raw sha256({name}))")
        if base64.b64encode(dg.hexdigest().encode()).decode() == cd:
            forms.append(f"b64(hex sha256({name}))")
    print(f"  CertDigest form: {forms or 'UNKNOWN'}")
    spd = sig.find("ds:SignedInfo/ds:Reference[@URI='#xadesSignedProperties']/ds:DigestValue", NS).text
    m = re.search(rb"<xades:SignedProperties.*?</xades:SignedProperties>", raw, re.S)
    sp = m.group(0)
    sp_el = sig.find(".//xades:SignedProperties", NS)
    # the SDK digests the element re-serialised with xmlns:xades on SignedProperties and xmlns:ds on ds children
    sp_ns = sp.replace(b'<xades:SignedProperties ', b'<xades:SignedProperties xmlns:xades="http://uri.etsi.org/01903/v1.3.2#" ', 1)
    sp_ns = re.sub(rb"<ds:(\w+)", rb'<ds:\1 xmlns:ds="http://www.w3.org/2000/09/xmldsig#"', sp_ns)
    sp_ns = sp_ns.replace(b' xmlns:ds="http://www.w3.org/2000/09/xmldsig#" ', b' xmlns:ds="http://www.w3.org/2000/09/xmldsig#" ')
    cands = {"text as written": sp, "text + xmlns (dom4j asXML)": sp_ns,
             "C14N in context": etree.tostring(sp_el, method="c14n"),
             "exc-C14N": etree.tostring(sp_el, method="c14n", exclusive=True)}
    found = []
    for name, data in cands.items():
        dg = hashlib.sha256(data)
        if base64.b64encode(dg.digest()).decode() == spd:
            found.append(f"b64(raw) of {name}")
        if base64.b64encode(dg.hexdigest().encode()).decode() == spd:
            found.append(f"b64(hex) of {name}")
    print(f"  SignedProperties digest: {found or 'NO MATCH among candidates'}; Type="
          f"{sig.find('ds:SignedInfo/ds:Reference[@URI=\"#xadesSignedProperties\"]', NS).get('Type')}")
    st = sig.find(".//xades:SigningTime", NS).text
    print(f"  SigningTime {st!r} (Z suffix: {st.endswith('Z')}); X509IssuerName {sig.find('.//ds:X509IssuerName', NS).text!r};"
          f" serial decimal: {sig.find('.//ds:X509SerialNumber', NS).text == str(cert.serial_number)}")
    sp_ws = bool(re.search(rb">\s+<", sp))
    print(f"  SignedProperties has inter-element whitespace: {sp_ws}")
    qr = first(tree, "//cac:AdditionalDocumentReference[cbc:ID='QR']/cac:Attachment/cbc:EmbeddedDocumentBinaryObject")
    if not qr:
        print("  no QR")
        return
    tlv = ber_tlv(base64.b64decode(qr))
    spki = pub.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    g = lambda xp: first(tree, xp)  # noqa: E731
    expect = {1: g("/*/cac:AccountingSupplierParty/cac:Party/cac:PartyLegalEntity/cbc:RegistrationName"),
              2: g("/*/cac:AccountingSupplierParty/cac:Party/cac:PartyTaxScheme/cbc:CompanyID"),
              5: g("/*/cac:TaxTotal/cbc:TaxAmount")}
    issue = f"{g('/*/cbc:IssueDate')}T{g('/*/cbc:IssueTime')}"
    print(f"  QR: {len(qr)} base64 chars, {len(base64.b64decode(qr))} TLV bytes, tags {[t for t, *_ in tlv]}")
    for tag, ln, form, v in tlv:
        note = ""
        if tag in expect:
            note = f"== XML: {v.decode('utf-8', 'replace') == expect[tag]}"
        elif tag == 3:
            note = f"value {v.decode()!r}; IssueDate+T+IssueTime={issue!r}"
        elif tag == 4:
            note = (f"value {v.decode()!r}; TaxInclusive={g('//cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount')!r} "
                    f"Payable={g('//cac:LegalMonetaryTotal/cbc:PayableAmount')!r}")
        elif tag == 6:
            note = f"== hash b64 text: {v == h.encode()}"
        elif tag == 7:
            note = f"== SignatureValue b64 text: {v.decode(errors='replace') == sv64}"
        elif tag == 8:
            note = f"== DER SPKI: {v == spki}"
        elif tag == 9:
            note = f"== cert.signature (DER ECDSA by CA): {v == cert.signature}"
        print(f"    tag {tag} len {ln} {form} {note}")


for p in sys.argv[1:]:
    analyse(p)
