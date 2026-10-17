"""AT-69 (opt-in): validate module-signed fake documents with ZATCA SDK R3.4.8, offline.

Collected only when ``ZATCA_SDK_DIR`` points to an unpacked copy of the SDK (never in the repo; CI has none, so this
file is skipped there) and ``ZATCA_SDK_CMD`` is an executable that runs the SDK without network and takes the
path of one XML file, e.g. a wrapper around ``docs/compliance/zatca-sdk-kit/scripts/sdk3/fat348.sh -validate
-invoice <file>`` (``unshare -n``). Java: 11, or a later JVM with the JCA provider order **SUN first, BouncyCastle
second** (``scripts/sdk3/bc-second.security``): SUN gives the RFC 2253 issuer string the SDK's samples carry and BC
supplies secp256k1. BouncyCastle first fails ``wrong X509IssuerName`` (findings §2).

Target (``docs/compliance/zatca-sdk-3-findings-2026-10-17.md``): the module's defaults, i.e. DTG QR layout, BER
TLV lengths and the ``rfc2253`` issuer style; no 2.0.3 workarounds (seller additional number, buyer region). The
SDK compares X509IssuerName / serial against its own certificate, so documents are signed with the SDK's bundled
dummy key and certificate. QR is **gating** for simplified documents. The SDK checks PIH against its one
``pih.txt`` (the R-12 seed), so each test signs the first document of a fresh EGS chain.
"""
import os
import subprocess
from pathlib import Path

import pytest

from zatca import crypto, qr
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
    path = Path(os.environ.get("ZATCA_SDK_OUT", tmp_path)) / name
    path.write_text(einv.signed_xml, encoding="utf-8")
    run = subprocess.run([os.environ["ZATCA_SDK_CMD"], str(path)], capture_output=True, text=True, timeout=300)
    return einv, run.stdout + run.stderr


def _passed(out, stages):
    """Every listed stage reports PASSED (gating, QR included) and the global result is PASSED."""
    for stage in stages:
        assert f"[{stage}] validation result : PASSED" in out, out
    assert "GLOBAL VALIDATION RESULT = PASSED" in out, out
    assert "wrong X509IssuerName" not in out, out  # provider order: SUN first, BouncyCastle second
    assert "Tag 4" not in out, out  # tag 4 = BT-115 (findings §3.1)


SIMPLIFIED = ("XSD", "EN", "KSA", "QR", "SIGNATURE", "PIH")
STANDARD = ("XSD", "EN", "KSA", "PIH")  # the SDK checks signature and QR on simplified documents only


def test_at69_sdk_validates_a_module_signed_simplified_invoice(zw, monkeypatch, tmp_path):
    cred = _sdk_unit(zw)
    einv, out = _validate(zw, monkeypatch, cred, issue(zw, zw.tst1, simplified=True), tmp_path, "module-simp-388.xml")
    assert qr.missing_tags(qr.decode(einv.qr_tlv), simplified=True) == []  # the SDK does not enforce presence
    _passed(out, SIMPLIFIED)


def test_at69_sdk_validates_a_long_seller_name_with_ber_length(zw, monkeypatch, tmp_path):
    """A 200-byte Arabic seller name: tag 1 carries the BER long form 0x81 C8 (R-15 as built)."""
    import base64

    cred = _sdk_unit(zw)
    mutate(monkeypatch, **{"seller.name": "ش" * 100})
    einv, out = _validate(zw, monkeypatch, cred, issue(zw, zw.tst1, simplified=True), tmp_path,
                          "module-simp-388-name200.xml")
    assert base64.b64decode(einv.qr_tlv)[:3] == bytes([1, 0x81, 200])
    _passed(out, SIMPLIFIED)


def test_sdk_validates_module_signed_standard_invoice(zw, monkeypatch, tmp_path):
    cred = _sdk_unit(zw)
    _, out = _validate(zw, monkeypatch, cred, issue(zw, zw.tst1), tmp_path, "module-std-388.xml")
    _passed(out, STANDARD)
