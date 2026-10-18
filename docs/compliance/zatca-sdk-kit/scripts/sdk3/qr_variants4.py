"""QR tag 3 / tag 5 variants of signed simplified documents, for the R4.0.0 run of 2026-10-18 (the QR is outside
the hash and the signature, so only the QR stage can change). Each variant also exists with a broken tag 7
("+bad7"): the SDK reports QR warnings (tag 3, tag 4) only when another QR check fails, so the "+bad7" twin shows
whether the variant itself raised a warning.
Usage: python3 -I qr_variants4.py OUTDIR signed-simplified.xml [signed-simplified-fx.xml]"""
import base64
import pathlib
import sys

from lxml import etree

NS = {"cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
      "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"}
out = pathlib.Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)


def tlv_parse(b):
    tags, i = {}, 0
    while i < len(b):
        t, n = b[i], b[i + 1]
        i += 2
        if n == 0x81:
            n, i = b[i], i + 1
        elif n == 0x82:
            n, i = int.from_bytes(b[i:i + 2], "big"), i + 2
        tags[t], i = b[i:i + n], i + n
    return tags


def ber(tag, v):
    n = len(v)
    return bytes([tag]) + (bytes([n]) if n < 128 else bytes([0x81, n]) if n < 256 else bytes([0x82]) + n.to_bytes(2, "big")) + v


def variants(path, prefix, changes):
    raw = pathlib.Path(path).read_bytes()
    t = etree.fromstring(raw)
    qr_old = t.xpath("//cac:AdditionalDocumentReference[cbc:ID='QR']//cbc:EmbeddedDocumentBinaryObject",
                     namespaces=NS)[0].text.strip()
    tags = tlv_parse(base64.b64decode(qr_old))
    print(prefix, {k: (v.decode() if k in (3, 4, 5) else len(v)) for k, v in tags.items()},
          "TaxTotal/TaxAmount:", [(e.text, e.get("currencyID")) for e in t.xpath("/*/cac:TaxTotal/cbc:TaxAmount", namespaces=NS)])
    for name, ch in changes(tags, t).items():
        for bad7 in (False, True):
            new = {**tags, **ch}
            if bad7:
                new[7] = base64.b64encode(b"\x00" * 70)
            qr = base64.b64encode(b"".join(ber(k, new[k]) for k in sorted(new))).decode()
            (out / f"{prefix}-{name}{'+bad7' if bad7 else ''}.xml").write_bytes(raw.replace(qr_old.encode(), qr.encode(), 1))


def base(tags, t):
    d3 = tags[3].decode().rstrip("Z")
    return {"asis": {}, "tag3-noZ": {3: d3.encode()}, "tag3-Z": {3: (d3 + "Z").encode()},
            "tag3-plus0300": {3: (d3 + "+03:00").encode()}, "tag3-wrongtime": {3: (d3[:-8] + "23:59:59").encode()},
            "tag3-dateonly": {3: d3[:10].encode()}, "tag5-wrong": {5: b"0.01"}}


def fx(tags, t):
    amounts = [e.text for e in t.xpath("/*/cac:TaxTotal/cbc:TaxAmount", namespaces=NS)]
    return {"asis": {}, **{f"tag5-{i}-{a}": {5: a.encode()} for i, a in enumerate(amounts)}, "tag5-wrong": {5: b"0.01"}}


variants(sys.argv[2], "q4", base)
if len(sys.argv) > 3:
    variants(sys.argv[3], "q4fx", fx)
print("ok", len(list(out.iterdir())))
