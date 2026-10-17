"""Extract compiled-schematron rules (id, flag, context, real test, message) from ZATCA SDK XSLs and diff them.

Usage: python -I xsl_rules.py OUT.json label=path.xsl [label=path.xsl ...]
       python -I xsl_rules.py --diff OUT.json labelA labelB   (prints added / removed / changed ids)
"""
import json
import re
import sys

from lxml import etree

XSL = "http://www.w3.org/1999/XSL/Transform"
SVRL = "http://purl.oclc.org/dsdl/svrl"


def norm(s):
    return re.sub(r"\s+", " ", s or "").strip()


def extract(path):
    tree = etree.parse(path, etree.XMLParser(huge_tree=True))
    rules = {}
    for el in tree.iter(f"{{{SVRL}}}failed-assert", f"{{{SVRL}}}successful-report"):
        rid = el.get("id") or ""
        iff = el.getparent()
        test = iff.get("test") if iff is not None and iff.tag == f"{{{XSL}}}if" else ""
        tpl = next((a for a in el.iterancestors(f"{{{XSL}}}template")), None)
        ctx = tpl.get("match") if tpl is not None else ""
        shown = "".join(a.xpath("string(.)") for a in el.iter(f"{{{XSL}}}attribute") if a.get("name") == "test")
        text = norm("".join(t.xpath("string(.)") for t in el.iter(f"{{{SVRL}}}text")))
        key = rid
        n = 2
        while key in rules:
            key = f"{rid}#{n}"
            n += 1
        rules[key] = {"flag": el.get("flag"), "context": norm(ctx), "test": norm(test), "shown_test": norm(shown),
                      "text": text}
    return rules


def main():
    if sys.argv[1] == "--diff":
        out = json.load(open(sys.argv[2]))
        a, b = out[sys.argv[3]], out[sys.argv[4]]
        key = lambda r: (r["test"], r["flag"], r["context"])  # noqa: E731
        print("ADDED", sorted(set(b) - set(a)))
        print("REMOVED", sorted(set(a) - set(b)))
        print("CHANGED", [k for k in sorted(set(a) & set(b)) if key(a[k]) != key(b[k])])
        return
    out = {}
    for arg in sys.argv[2:]:
        label, path = arg.split("=", 1)
        out[label] = extract(path)
        print(label, len(out[label]), "rules")
    json.dump(out, open(sys.argv[1], "w"), indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
