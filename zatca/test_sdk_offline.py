"""AT-69 (opt-in): validate module-signed fake documents with ZATCA SDK R3.4.8 **or R4.0.0**, offline.

Collected only when ``ZATCA_SDK_DIR`` points to an unpacked copy of the SDK (never in the repo; CI has none, so this
file is skipped there) and ``ZATCA_SDK_CMD`` is an executable that runs the SDK without network and takes the
path of one XML file, e.g. a wrapper around ``docs/compliance/zatca-sdk-kit/scripts/sdk3/fat348.sh -validate
-invoice <file>`` or ``fat400.sh -validate -invoice <file>`` (both ``unshare -n``).

Java (findings 2026-10-17 §2, §7.3):

- **R4.0.0** needs Java **21 or later** (class version 65; its readme's "11 to 14" is stale). The **JDK default
  provider order** is right: the jar registers BouncyCastle itself and asks for it by name for secp256k1.
- **R3.4.8**: Java 11, or a later JVM with the JCA provider order **SUN first, BouncyCastle second**
  (``scripts/sdk3/bc-second.security``): SUN gives the RFC 2253 issuer string the SDK's samples carry and BC
  supplies secp256k1.
- Never BouncyCastle first, with either build: it fails ``wrong X509IssuerName``.

Target: the module's defaults, i.e. DTG QR layout, BER TLV lengths, tag 3 without ``Z``, tag 4 = BT-115 and the
``rfc2253`` issuer style; no 2.0.3 workarounds (seller additional number, buyer region). The SDK compares
X509IssuerName / serial against its own certificate, so documents are signed with the SDK's bundled dummy key and
certificate. QR is **gating** for simplified documents. The SDK checks PIH against its one ``pih.txt`` (the R-12
seed), so each test signs the first document of a fresh EGS chain.

**QR warnings.** Both builds put a tag-3 (``timeStamp``) or tag-4 (``invoiceTotal``) mismatch in a warning map that
is printed **only when another QR check fails**, so a clean run cannot show them. Each simplified document is
therefore also validated as a twin whose tag 7 is deliberately broken: the QR stage must fail on tag 7 alone and
print no ``timeStamp`` / ``invoiceTotal`` warning. A control twin with ``Z`` added to tag 3 must print
``timeStamp``, which proves the technique still works with the SDK under test (findings §7.3).
"""
import base64
import os
import subprocess
from pathlib import Path

import pytest

from zatca import crypto, qr, ubl
from zatca.conftest import NOW, einvoice_of, issue, mutate, onboard

pytestmark = pytest.mark.django_db
SDK = Path(os.environ.get("ZATCA_SDK_DIR", "/nonexistent"))


def _sdk_unit(zw):
    """Onboard TST1 through the services, then let its unit sign with the SDK's dummy key and certificate."""
    from zatca import keystore, services
    from zatca.models import EgsCredential

    certs = SDK / "Data" / "Certificates"
    pem = (certs / "ec-secp256k1-priv-key.pem").read_text().strip()
    if "BEGIN" not in pem:
        pem = f"-----BEGIN EC PRIVATE KEY-----\n{pem}\n-----END EC PRIVATE KEY-----\n"
    sdk_key = crypto.load_private_key(pem.encode())
    sdk_cert = crypto.load_cert_base64((certs / "cert.pem").read_text().strip())
    egs = onboard(zw, zw.tst1, zw.admin1)
    egs.private_key_ref = zw.fakes.store.write_private_key(keystore.private_key_path("TST1", "EGS1"),
                                                           crypto.private_key_pem(sdk_key), services._master_ref(zw.tst1))
    egs._service_write = True
    egs.save()
    cred = EgsCredential.objects.get(egs_unit=egs, kind="production", status="active")
    stamp_cred = EgsCredential(pk=cred.pk)
    stamp_cred.__dict__.update(cred.__dict__)
    stamp_cred.certificate_pem = crypto.cert_pem(sdk_cert)
    return stamp_cred


def _validate(zw, monkeypatch, stamp_cred, doc, tmp_path, name):
    from zatca import services

    original = services._signing_credential
    monkeypatch.setattr(services, "_signing_credential", lambda *a, **k: (original(*a, **k), stamp_cred)[1])
    einv = einvoice_of(doc)
    services.stamp_document(einv, user=zw.queue, now=NOW)
    einv.refresh_from_db()
    assert einv.icv, list(einv.messages.values_list("code", "message_en"))
    path = _folder(tmp_path) / name
    path.write_text(einv.signed_xml, encoding="utf-8")
    return einv, _run(path)


def _folder(tmp_path):
    return Path(os.environ.get("ZATCA_SDK_OUT", tmp_path))


def _run(path):
    run = subprocess.run([os.environ["ZATCA_SDK_CMD"], str(path)], capture_output=True, text=True, timeout=300)
    return run.stdout + run.stderr


def _passed(out, stages):
    """Every listed stage reports PASSED (gating, QR included) and the global result is PASSED."""
    for stage in stages:
        assert f"[{stage}] validation result : PASSED" in out, out
    assert "GLOBAL VALIDATION RESULT = PASSED" in out, out
    assert "wrong X509IssuerName" not in out, out  # provider order: never BouncyCastle first
    assert "timeStamp" not in out, out  # tag 3 = IssueDate T IssueTime, no "Z" (findings §7.3)
    assert "Tag 4" not in out and "invoiceTotal" not in out, out  # tag 4 = BT-115 (findings §3.1)


BROKEN_TAG_7 = base64.b64encode(b"\x00" * 70)  # a well-formed base64 text that is not the SignatureValue


def _qr_twin(signed_xml, path, **tags):
    """``signed_xml`` with its QR re-encoded after replacing the given tags (``t7=...``); hash and signature are
    unchanged because the QR is outside both (R-13)."""
    old = ubl.qr_from_xml(signed_xml.encode())
    values = qr.decode(old)
    for name, value in tags.items():
        values[int(name[1:])] = value
    new = qr.encode(sorted(values.items()))
    path.write_text(signed_xml.replace(old, new, 1), encoding="utf-8")
    return path


def _no_qr_warnings(einv, folder, name, control=False):
    """The broken-tag-7 technique: the QR stage fails on tag 7 only, so its warnings are printed, and none is
    ``timeStamp`` (tag 3) or ``invoiceTotal`` (tag 4). ``control``: also show that a tag 3 with ``Z`` is flagged."""
    out = _run(_qr_twin(einv.signed_xml, folder / f"{name}+bad7.xml", t7=BROKEN_TAG_7))
    assert "[QR] validation result : FAILED" in out, out
    assert "CODE : digital signature" in out, out  # the deliberate tag-7 failure, so warnings are printed
    assert "timeStamp" not in out, out
    assert "invoiceTotal" not in out and "Tag 4" not in out, out
    if control:
        tag3 = qr.decode(einv.qr_tlv)[3]
        out = _run(_qr_twin(einv.signed_xml, folder / f"{name}+tag3Z+bad7.xml", t3=tag3 + b"Z", t7=BROKEN_TAG_7))
        assert "CODE : timeStamp" in out, out  # the SDK under test does report a tag-3 mismatch this way


SIMPLIFIED = ("XSD", "EN", "KSA", "QR", "SIGNATURE", "PIH")
STANDARD = ("XSD", "EN", "KSA", "PIH")  # the SDK checks signature and QR on simplified documents only


def test_at69_sdk_validates_a_module_signed_simplified_invoice(zw, monkeypatch, tmp_path):
    cred = _sdk_unit(zw)
    einv, out = _validate(zw, monkeypatch, cred, issue(zw, zw.tst1, simplified=True), tmp_path, "module-simp-388.xml")
    assert qr.missing_tags(qr.decode(einv.qr_tlv), simplified=True) == []  # the SDK does not enforce presence
    _passed(out, SIMPLIFIED)
    _no_qr_warnings(einv, _folder(tmp_path), "module-simp-388", control=True)


def test_at69_sdk_validates_a_long_seller_name_with_ber_length(zw, monkeypatch, tmp_path):
    """A 200-byte Arabic seller name: tag 1 carries the BER long form 0x81 C8 (R-15 as built)."""
    cred = _sdk_unit(zw)
    mutate(monkeypatch, **{"seller.name": "ش" * 100})
    einv, out = _validate(zw, monkeypatch, cred, issue(zw, zw.tst1, simplified=True), tmp_path,
                          "module-simp-388-name200.xml")
    assert base64.b64decode(einv.qr_tlv)[:3] == bytes([1, 0x81, 200])
    _passed(out, SIMPLIFIED)
    _no_qr_warnings(einv, _folder(tmp_path), "module-simp-388-name200")


def test_sdk_validates_module_signed_standard_invoice(zw, monkeypatch, tmp_path):
    cred = _sdk_unit(zw)
    _, out = _validate(zw, monkeypatch, cred, issue(zw, zw.tst1), tmp_path, "module-std-388.xml")
    _passed(out, STANDARD)


def test_sdk_validates_a_b2g_standard_invoice(zw, monkeypatch, tmp_path):
    """R-54: buyer KSA-40 GOV + BT-7 payment order date, KSA-2 at 7 positions. R4.0.0 would warn BR-KSA-91 / -92
    on a wrong or incomplete pair and refuse a 9-position KSA-2 (BR-KSA-06); R3.4.8 ignores KSA-40."""
    from datetime import date
    from types import SimpleNamespace

    from invoicing import services as invoicing_services
    from invoicing.conftest import ADDRESS, approve, invoice
    from zatca.conftest import TODAY

    cred = _sdk_unit(zw)
    gov = invoicing_services.create_customer(
        zw.tst1, user=zw.clerk, code="G001", customer_kind="government", name_en="Test Ministry",
        name_ar="وزارة الاختبار", is_vat_registered=True, vat_number="310000000000003", **ADDRESS)
    w = SimpleNamespace(alm=zw.tst1, clerk=zw.clerk, customer=gov, cfo=zw.cfo)
    doc = approve(w, invoice(w, (("management_fee", "1", "100.00"),), customer=gov, user=zw.clerk,
                             issue_date=TODAY, entity=zw.tst1, payment_order_date=date(2026, 10, 5)), zw.cfo)
    einv, out = _validate(zw, monkeypatch, cred, doc, tmp_path, "module-std-388-b2g.xml")
    assert "<cbc:CompanyLegalForm>GOV</cbc:CompanyLegalForm>" in einv.signed_xml
    assert "<cbc:TaxPointDate>2026-10-05</cbc:TaxPointDate>" in einv.signed_xml
    _passed(out, STANDARD)
    for code in ("BR-KSA-91", "BR-KSA-92", "BR-KSA-06"):
        assert code not in out, out


def test_g2_sdk_validates_a_foreign_currency_simplified_invoice_with_the_sar_tax_total_first(zw, monkeypatch,
                                                                                           tmp_path):
    """Owner decision G2 (R-40, OQ-22): a USD simplified invoice with BT-111 (SAR) as the first TaxTotal and QR
    tag 5 = the SAR VAT total, as the SDK generator would write it; every stage passes, no QR warning."""
    from core.access.testing import make_user, member
    from ledger import services as ledger
    from zatca.conftest import TODAY

    cred = _sdk_unit(zw)
    treasurer = make_user("treasury_sdk_usd")
    member(treasurer, zw.par, "group_treasury")
    ledger.create_rate(user=treasurer, from_currency="USD", to_currency="SAR", on=TODAY, rate="3.75", group=zw.par,
                       source="sama", source_reference="SAMA test bulletin")
    doc = issue(zw, zw.tst1, simplified=True, currency="USD")
    einv, out = _validate(zw, monkeypatch, cred, doc, tmp_path, "module-simp-388-usd.xml")
    assert "<cbc:TaxAmount currencyID=\"SAR\">56.25</cbc:TaxAmount>" in einv.signed_xml.split("<cac:TaxTotal>")[1]
    assert qr.decode(einv.qr_tlv)[5] == b"56.25"
    _passed(out, SIMPLIFIED)
    _no_qr_warnings(einv, _folder(tmp_path), "module-simp-388-usd")
