"""Analyse an SDK-signed invoice: recompute hash, verify SignatureValue against candidates,
check digests, decode QR TLV. Usage: python3 -I analyse_signed.py signed.xml unsigned.xml"""
import sys, base64, hashlib, re
from lxml import etree
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils as ecutils
from cryptography.exceptions import InvalidSignature

NS = {"ext": "urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2",
      "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
      "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
      "ds": "http://www.w3.org/2000/09/xmldsig#",
      "xades": "http://uri.etsi.org/01903/v1.3.2#"}

signed_path, unsigned_path = sys.argv[1], sys.argv[2]
raw = open(signed_path, "rb").read()
tree = etree.fromstring(raw)


def invoice_hash(xml_bytes, method="c14n"):
    """R-13: drop UBLExtensions, cac:Signature (child of root), QR ADR; C14N; sha256."""
    root = etree.fromstring(xml_bytes)
    for xp in ["//ext:UBLExtensions", "/*/cac:Signature", "//cac:AdditionalDocumentReference[cbc:ID='QR']"]:
        for el in root.xpath(xp, namespaces=NS):
            parent = el.getparent()
            # keep the tail whitespace (as XPath-filter / XSLT removal does): attach it to the previous node
            tail = el.tail
            prev = el.getprevious()
            if tail:
                if prev is not None:
                    prev.tail = (prev.tail or "") + tail
                else:
                    parent.text = (parent.text or "") + tail
            parent.remove(el)
    c = etree.tostring(root, method=method)  # lxml: c14n = C14N 1.0 inclusive
    return c, base64.b64encode(hashlib.sha256(c).digest()).decode()


c14n, h = invoice_hash(raw)
print("python hash (lxml c14n 1.0):", h)
c2, h2 = invoice_hash(raw, method="c14n2")
print("python hash (lxml c14n2)   :", h2, "same bytes as c14n1.0:", c2 == c14n)

sig = tree.find(".//ds:Signature", NS)
digest_inv = sig.find("ds:SignedInfo/ds:Reference[@Id='invoiceSignedData']/ds:DigestValue", NS).text
print("Reference invoiceSignedData DigestValue:", digest_inv, "== python hash:", digest_inv == h)
sv = base64.b64decode(sig.find("ds:SignatureValue", NS).text)
cert_b64 = sig.find("ds:KeyInfo/ds:X509Data/ds:X509Certificate", NS).text
cert = x509.load_der_x509_certificate(base64.b64decode(cert_b64))
pub = cert.public_key()
print("curve:", pub.curve.name, "| SignatureValue DER len", len(sv), "first byte", hex(sv[0]))
r, s = ecutils.decode_dss_signature(sv)

si = sig.find("ds:SignedInfo", NS)
cands = {
    "raw 32-byte invoice hash (SHA256withECDSA over it)": base64.b64decode(h),
    "base64 hash text": h.encode(),
    "hex text of hash": base64.b64decode(h).hex().encode(),
    "C14N 1.0 of SignedInfo (in-context)": etree.tostring(si, method="c14n"),
    "canonical invoice bytes": c14n,
}
for name, data in cands.items():
    try:
        pub.verify(sv, data, ec.ECDSA(hashes.SHA256()))
        print("  VERIFY OK   :", name)
    except InvalidSignature:
        print("  verify fail :", name)
# prehashed: signature directly over the 32-byte hash (no second SHA-256)
try:
    pub.verify(sv, base64.b64decode(h), ec.ECDSA(ecutils.Prehashed(hashes.SHA256())))
    print("  VERIFY OK   : prehashed raw hash (no re-hash)")
except InvalidSignature:
    print("  verify fail : prehashed raw hash (no re-hash)")

# Cert digest
cd = sig.find(".//xades:CertDigest/ds:DigestValue", NS).text
print("CertDigest:", cd[:24] + "...", "len", len(cd))
for name, data in {"sha256(DER cert)": base64.b64decode(cert_b64), "sha256(base64 cert text)": cert_b64.encode()}.items():
    d = hashlib.sha256(data)
    print(f"  {name}: b64(raw)={base64.b64encode(d.digest()).decode() == cd}  b64(hex)={base64.b64encode(d.hexdigest().encode()).decode() == cd}")

# SignedProperties digest
sp_digest = sig.find("ds:SignedInfo/ds:Reference[@URI='#xadesSignedProperties']/ds:DigestValue", NS).text
m = re.search(rb"<xades:SignedProperties.*?</xades:SignedProperties>", raw, re.S)
sp_text = m.group(0)
sp_el = sig.find(".//xades:SignedProperties", NS)
for name, data in {"source text as serialised": sp_text,
                   "C14N 1.0 in context": etree.tostring(sp_el, method="c14n"),
                   "exclusive C14N": etree.tostring(sp_el, method="c14n", exclusive=True)}.items():
    d = hashlib.sha256(data)
    print(f"  SignedProperties {name}: b64(raw)={base64.b64encode(d.digest()).decode() == sp_digest}  b64(hex)={base64.b64encode(d.hexdigest().encode()).decode() == sp_digest}")
print("  SignedProperties text starts:", sp_text[:140])

# QR
qr = tree.xpath("//cac:AdditionalDocumentReference[cbc:ID='QR']/cac:Attachment/cbc:EmbeddedDocumentBinaryObject", namespaces=NS)[0].text
tlv = base64.b64decode(qr)
print("QR base64 len", len(qr), "TLV bytes", len(tlv))
i = 0
while i < len(tlv):
    t, l = tlv[i], tlv[i + 1]
    v = tlv[i + 2:i + 2 + l]
    i += 2 + l
    try:
        shown = v.decode("utf-8") if t <= 7 else v[:12].hex() + "..."
    except UnicodeDecodeError:
        shown = v[:12].hex() + "..."
    print(f"  tag {t} len {l}: {shown}")
    if t == 6:
        print("    tag6 == invoice hash b64 text:", v == h.encode(), "| == raw 32 bytes:", v == base64.b64decode(h))
    if t == 7:
        print("    tag7 == SignatureValue b64 text:", v.decode() == sig.find('ds:SignatureValue', NS).text)
    if t == 8:
        spki = pub.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        print("    tag8 == DER SPKI of cert key:", v == spki)
    if t == 9:
        print("    tag9 == cert.signature (DER ECDSA of CA):", v == cert.signature)

# unsigned input hash for comparison
_, hu = invoice_hash(open(unsigned_path, "rb").read())
print("hash of unsigned input:", hu, "== signed hash:", hu == h)
