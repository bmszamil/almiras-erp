"""Compute ZATCA invoice hash (R-13) with lxml; prints base64 hash. Usage: pyhash.py file..."""
import sys, base64, hashlib
from lxml import etree
NS = {"ext": "urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2",
      "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
      "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"}
def h(data):
    root = etree.fromstring(data, etree.XMLParser(remove_comments=False, resolve_entities=False))
    for xp in ["//ext:UBLExtensions", "/*/cac:Signature", "//cac:AdditionalDocumentReference[cbc:ID='QR']"]:
        for el in root.xpath(xp, namespaces=NS):
            p, prev, tail = el.getparent(), el.getprevious(), el.tail
            if tail:
                if prev is not None: prev.tail = (prev.tail or "") + tail
                else: p.text = (p.text or "") + tail
            p.remove(el)
    c = etree.tostring(root.getroottree(), method="c14n", with_comments=False)
    return base64.b64encode(hashlib.sha256(c).digest()).decode()
for f in sys.argv[1:]:
    print(h(open(f, "rb").read()), f.rsplit("/", 1)[-1])
