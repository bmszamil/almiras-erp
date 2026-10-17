"""AT-69 (opt-in): validate a module-signed fake simplified 388 with the ZATCA SDK, offline.

Collected only when ``ZATCA_SDK_DIR`` points to an unpacked SDK (never in the repo) and ``ZATCA_SDK_CMD`` is an
executable that runs ``fatoora`` without network (e.g. ``unshare -n``, BouncyCastle first on Java >= 16) and
takes ``-validate -invoice <file>``. The SDK compares X509IssuerName / serial against its own configured
certificate, so the document is signed with the SDK's bundled dummy key and certificate, in the SDK 2.0.3 QR
layout and BouncyCastle issuer style. QR results are recorded, not gating (OQ-28).
"""
import os
import subprocess
from pathlib import Path

import pytest

from zatca import crypto, qr
from zatca.conftest import NOW, einvoice_of, issue, onboard

pytestmark = pytest.mark.django_db
SDK = Path(os.environ.get("ZATCA_SDK_DIR", "/nonexistent"))


def _sdk_unit(zw, settings):
    """Onboard TST1 through the services, then let its unit sign with the SDK's dummy key and certificate."""
    from zatca import keystore, services
    from zatca.models import EgsCredential

    settings.ZATCA_QR_TAGS_7_9_LAYOUT = qr.LAYOUT_SDK_203
    settings.ZATCA_X509_ISSUER_NAME_STYLE = "bc"
    certs = SDK / "Data" / "Certificates"
    pem = (certs / "ec-secp256k1-priv-key.pem").read_text().strip()
    if "BEGIN" not in pem:
        pem = f"-----BEGIN EC PRIVATE KEY-----\n{pem}\n-----END EC PRIVATE KEY-----\n"
    sdk_key = crypto.load_private_key(pem.encode())
    sdk_cert = crypto.load_cert_base64((certs / "cert.pem").read_text().strip())
    # SDK 2.0.3's 2021 rules require the seller additional number (BR-KSA-09/-64); the 2023 Data Dictionary makes
    # it optional (KSA-23 "O", spec R-4). The fake entity gets one so the old SDK can check the rest.
    type(zw.tst1).objects.filter(pk=zw.tst1.pk).update(additional_no="5678")
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
    out = subprocess.run([os.environ["ZATCA_SDK_CMD"], str(path)], capture_output=True, text=True, timeout=300).stdout
    return einv, out


def _passed(out, minimum):
    results = [line for line in out.splitlines() if "validation result" in line]
    assert "GLOBAL VALIDATION RESULT = PASSED" in out, out
    assert len(results) >= minimum and all("PASSED" in r for r in results), out


def test_at69_sdk_validates_a_module_signed_simplified_invoice(zw, settings, monkeypatch, tmp_path):
    cred = _sdk_unit(zw, settings)
    _, out = _validate(zw, monkeypatch, cred, issue(zw, zw.tst1, simplified=True), tmp_path, "module-simp-388.xml")
    _passed(out, 6)  # XSD, EN, KSA, QR, SIGNATURE, PIH


def test_sdk_validates_module_signed_standard_invoice(zw, settings, monkeypatch, tmp_path):
    """The SDK checks PIH against the initial literal, so only the first document of the chain is checked."""
    from zatca.conftest import mutate

    cred = _sdk_unit(zw, settings)
    # required by SDK 2.0.3's 2021 rules only (BR-KSA-63, BR-KSA-10); optional in the 2023 documents
    mutate(monkeypatch, **{"buyer.additional_no": "6789", "buyer.region": "Makkah Region"})
    _, out = _validate(zw, monkeypatch, cred, issue(zw, zw.tst1), tmp_path, "module-std-388.xml")
    _passed(out, 4)
