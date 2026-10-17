"""Scratch-only generator (named so pytest never collects it here; copy it as zatca/test_sdk3_export.py) (copied into a `git archive HEAD` export, never into the repo): writes module-signed fake
documents for the ZATCA SDK R3.4.8 run of 2026-10-17. Each test runs in a fresh fake world, so each first document
of an EGS unit carries the initial PIH. Signed with the SDK's bundled dummy key and certificate (the SDK compares
the certificate), default QR layout (DTG) and the RFC 2253 issuer style (SUN form). Output: $SDK3_OUT/<name>.xml.
"""
import os
from decimal import Decimal as D
from pathlib import Path

import pytest

from core.access.testing import make_user, member
from zatca import crypto, qr, services
from zatca.conftest import NOW, TODAY, einvoice_of, issue, mutate, onboard, run

pytestmark = pytest.mark.django_db
SDK = Path(os.environ["ZATCA_SDK_DIR"])
OUT = Path(os.environ["SDK3_OUT"])


def _sdk_cred(zw, settings, monkeypatch):
    from zatca import keystore
    from zatca.models import EgsCredential

    settings.ZATCA_QR_TAGS_7_9_LAYOUT = qr.LAYOUT_DTG
    settings.ZATCA_X509_ISSUER_NAME_STYLE = "rfc2253"
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
    stamp = EgsCredential(pk=cred.pk)
    stamp.__dict__.update(cred.__dict__)
    stamp.certificate_pem = crypto.cert_pem(sdk_cert)
    original = services._signing_credential
    monkeypatch.setattr(services, "_signing_credential", lambda *a, **k: (original(*a, **k), stamp)[1])
    return egs


def _write(doc, name):
    einv = einvoice_of(doc)
    services.stamp_document(einv, user=einv.entity and _queue(), now=NOW)
    einv.refresh_from_db()
    assert einv.icv, list(einv.messages.values_list("code", "message_en"))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.xml").write_text(einv.signed_xml, encoding="utf-8")
    return einv


def _queue():
    from core import access

    return access.system_user("zatca_queue")


def _rate(zw, cur, rate):
    from ledger import services as ledger

    treasurer = make_user(f"treasury_{cur.lower()}")
    member(treasurer, zw.par, "group_treasury")
    ledger.create_rate(user=treasurer, from_currency=cur, to_currency="SAR", on=TODAY, rate=D(rate), group=zw.par)


def test_simp_388(zw, settings, monkeypatch):
    _sdk_cred(zw, settings, monkeypatch)
    _write(issue(zw, zw.tst1, simplified=True), "module-simp-388")


def test_std_388(zw, settings, monkeypatch):
    _sdk_cred(zw, settings, monkeypatch)
    _write(issue(zw, zw.tst1), "module-std-388")


def test_simp_388_two_lines(zw, settings, monkeypatch):
    _sdk_cred(zw, settings, monkeypatch)
    _write(issue(zw, zw.tst1, simplified=True, lines=(("management_fee", "3", "10.03"), ("management_fee", "1", "7.77"))),
           "module-simp-388-2lines")


def test_kwd(zw, settings, monkeypatch):
    _sdk_cred(zw, settings, monkeypatch)
    _rate(zw, "KWD", "12.2000")
    _write(issue(zw, zw.tst1, lines=[("management_fee", "1", "1000.000")], currency="KWD"), "module-std-388-kwd")


def test_kwd_simplified(zw, settings, monkeypatch):
    _sdk_cred(zw, settings, monkeypatch)
    _rate(zw, "KWD", "12.2000")
    _write(issue(zw, zw.tst1, simplified=True, lines=[("management_fee", "1", "1000.000")], currency="KWD"),
           "module-simp-388-kwd")


def test_notes(zw, settings, monkeypatch):
    """381 and 383 after a cleared 388 (their PIH is the previous document's hash, not the seed)."""
    from types import SimpleNamespace

    from invoicing.conftest import approve, invoice

    _sdk_cred(zw, settings, monkeypatch)
    original = issue(zw, zw.tst1)
    _write(original, "module-std-388-chain1")
    run(zw)
    original.refresh_from_db()
    w = SimpleNamespace(alm=zw.tst1, clerk=zw.clerk, customer=original.customer, cfo=zw.cfo)
    for doc_type, name in (("credit_note", "module-std-381-chain2"), ("debit_note", "module-std-383-chain3")):
        note = invoice(w, [("management_fee", "1", "10.00")], customer=original.customer, issue_date=TODAY,
                       entity=zw.tst1, doc_type=doc_type, original_document=original, payment_means_code="10",
                       reason_ar="إرجاع البضاعة")
        note = approve(w, note, zw.cfo)
        _write(note, name)
        run(zw)


def test_simplified_long_name(zw, settings, monkeypatch):
    """A seller name of 127 UTF-8 bytes (the interim R-15 cap)."""
    _sdk_cred(zw, settings, monkeypatch)
    name = ("شركة " * 40).encode()[:127].decode("utf-8", "ignore")
    name += "x" * (127 - len(name.encode()))
    mutate(monkeypatch, **{"seller.name": name})
    _write(issue(zw, zw.tst1, simplified=True), "module-simp-388-name127")
