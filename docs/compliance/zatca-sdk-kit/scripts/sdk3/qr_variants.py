"""Make QR variants of SDK-signed simplified documents (the QR is outside the hash and the signature, so only the
QR stage can change). Usage: python3 -I qr_variants.py OUTDIR signed-final.xml signed-name200.xml"""
import base64
import pathlib
import re
import sys

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import utils as ecutils
from lxml import etree

NS = {"cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
      "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
      "ds": "http://www.w3.org/2000/09/xmldsig#"}
out = pathlib.Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)


def ber(tag, v):
    n = len(v)
    return bytes([tag]) + (bytes([n]) if n < 128 else bytes([0x81, n])) + v


def one_byte(tag, v):
    return bytes([tag, len(v)]) + v


def parts(path):
    raw = open(path, "rb").read()
    t = etree.fromstring(raw)
    g = lambda xp: t.xpath(xp, namespaces=NS)[0].text.strip()  # noqa: E731
    sv = g("//ds:SignatureValue")
    cert = x509.load_der_x509_certificate(base64.b64decode(g("//ds:X509Certificate")))
    qr_old = g("//cac:AdditionalDocumentReference[cbc:ID='QR']//cbc:EmbeddedDocumentBinaryObject")
    v = {1: g("//cac:AccountingSupplierParty//cbc:RegistrationName").encode(),
         2: g("//cac:AccountingSupplierParty//cac:PartyTaxScheme/cbc:CompanyID").encode(),
         3: f"{g('/*/cbc:IssueDate')}T{g('/*/cbc:IssueTime')}".encode(),
         4: g("//cac:LegalMonetaryTotal/cbc:PayableAmount").encode(),
         5: g("/*/cac:TaxTotal/cbc:TaxAmount").encode(),
         6: g("//ds:Reference[@Id='invoiceSignedData']/ds:DigestValue").encode(),
         7: sv.encode(),
         8: cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo),
         9: cert.signature}
    extra = {"incl": g("//cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount").encode(), "sv_der": base64.b64decode(sv)}
    return raw, qr_old, v, extra


def write(name, raw, qr_old, tlv):
    new = base64.b64encode(tlv).decode()
    (out / f"{name}.xml").write_bytes(raw.replace(qr_old.encode(), new.encode(), 1))


raw, qr_old, v, x = parts(sys.argv[2])
std = lambda vals, enc=ber: b"".join(enc(k, vals[k]) for k in sorted(vals))  # noqa: E731
write("qrv-final-baseline", raw, qr_old, std(v))
write("qrv-final-tag4-taxinclusive", raw, qr_old, std({**v, 4: x["incl"]}))
write("qrv-final-tag3-Z", raw, qr_old, std({**v, 3: v[3] + b"Z"}))
write("qrv-final-no-tag9", raw, qr_old, std({k: v[k] for k in range(1, 9)}))
write("qrv-final-tags1-7", raw, qr_old, std({k: v[k] for k in range(1, 8)}))
write("qrv-final-tags1-6", raw, qr_old, std({k: v[k] for k in range(1, 7)}))
r, s = ecutils.decode_dss_signature(x["sv_der"])
jb = lambda n: n.to_bytes(n.bit_length() // 8 + 1, "big")  # noqa: E731
write("qrv-final-sdk203-layout", raw, qr_old, std({**{k: v[k] for k in range(1, 7)}, 7: v[8], 8: jb(r), 9: jb(s)}))
write("qrv-final-tag7-raw-der", raw, qr_old, std({**v, 7: x["sv_der"]}))
raw2, qr_old2, v2, _ = parts(sys.argv[3])
write("qrv-name200-ber", raw2, qr_old2, std(v2))
write("qrv-name200-onebyte", raw2, qr_old2, std(v2, one_byte))
print("ok", sorted(p.name for p in out.iterdir()))
