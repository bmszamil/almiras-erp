"""R-13 to R-15, R-37 on the pure functions (no database): invoice hash against ZATCA SDK 2.0.3, QR TLV."""
import base64
import hashlib
from pathlib import Path

import pytest
from django.core.exceptions import ImproperlyConfigured

from zatca import crypto, qr, rules

KIT = Path(__file__).resolve().parent.parent / "docs" / "compliance" / "zatca-sdk-kit" / "inv"

# AT-13: ``fatoora -generateHash`` of ZATCA SDK 2.0.3 (cli-3.0.8), run offline (no network) on 2026-10-17
# over the kit documents; recorded here as the fixture values.
SDK_HASHES = {
    "simp-381.xml": "6jM5YCcXV5Wg3tIOl3QM9HUDkBcQUHxzYHh9cnn1ImE=",
    "simp-383.xml": "iH0x752aj7BeBglWqZmXgleHt24sBZnfh1Yg6xE1iFc=",
    "simp-386.xml": "LFiGnMO9OzMpvd8gyoqq2CVc5aZyUy89KQvC9bFJQzM=",
    "simp-388-long.xml": "vdDOZ8FCzMvEhwRhNIO1iox0zn2XCo4KOq6bspWnBsE=",
    "simp-388-short.xml": "0AodkI7kXhLLcRWEOXhXPaIPEBsv9mbWZvkKZYUzBIs=",
    "simp-388-tol.xml": "QxBz5/Nx5NO6MCI4OGWlU+AyZ1k4Bf88xOD1gs26Zwk=",
    "simp-388.xml": "Wmi9JGQPgNZ5c/2wqw5UrPjsjs0tIVhCechtaGL8iuc=",
    "std-381-cnroot.xml": "/PMNbKW2keG7dooeoNRoyUA59O4pcCziM0HZnPxEswU=",
    "std-381-noreason.xml": "HCgv4UJFfKsN1fMh961JWptGGmP9xMWRPOA4gdKuKL4=",
    "std-381-noref.xml": "nOm0CBcQEEFcANXBYGLrUpGDEz3HcxQ+Fl2jbYQI0ME=",
    "std-381.xml": "0rR+zW3L5+WL4w478eBZAusGXziBxXzU6w7lVlJUMYY=",
    "std-383.xml": "oa6gL493bgZJJNxWapc66sI/QTkwRVBsieQUPhkOR4o=",
    "std-386.xml": "B/aRwjIGua4i4OZDa14qMyAAPO1uEao040L2eZpMLas=",
    "std-388-pih2.xml": "gAUE0jOPciou4vZsINi30SGwsJEcGQCwhwiVlTdIBiM=",
    "std-388-tol.xml": "R2b46+Vz+YIwJ3iaMTjIQOKbWPcWEDXOin680MAyUu4=",
    "std-388-tolb.xml": "f3zKT2zw3KnZpSER70Exqbp6UKXIfJbb5sj033uXDyU=",
    "std-388.xml": "wFNDvpy7ja111O83rwuhxYf78KVALCC+vje0rLxEJwg=",
    "std-tol-5.50.xml": "zx8bNA+ugZDok8in/bQH+Meqks003myP44QPXYR9s1U=",
    "std-tol-5.51.xml": "Sa7dBJA79nZ0qesWZHpuWqDNA7O+OQWFufFzTQZZtDA=",
    "tricky.xml": "cO7HwPFFA2TuCFMGp4Ss2eJ5hNVwwqF2LBd9w9998io=",
}


def test_at13_kit_documents_hash_exactly_as_the_sdk():
    assert sorted(p.name for p in KIT.glob("*.xml")) == sorted(SDK_HASHES)
    for name, expected in SDK_HASHES.items():
        got = crypto.invoice_hash((KIT / name).read_bytes())
        assert got == expected, name
        assert len(got) == 44 and len(base64.b64decode(got)) == 32  # base64 of the raw 32-byte digest


def test_r13_c14n2_would_not_match():
    """R-13: lxml ``c14n2`` must not be used; it gives other bytes than the SDK's C14N 1.1 on the kit."""
    from lxml import etree

    data = (KIT / "tricky.xml").read_bytes()
    root = etree.fromstring(crypto.canonical_for_hash(data))
    c14n2 = etree.tostring(root, method="c14n2")
    assert base64.b64encode(hashlib.sha256(c14n2).digest()).decode() != SDK_HASHES["tricky.xml"]


def test_at10_initial_pih_is_the_literal():
    assert crypto.INITIAL_PIH == ("NWZlY2ViNjZmZmM4NmYzOGQ5NTI3ODZjNmQ2OTZjNzljMmRiYzIzOWRkNGU5MWI0NjcyOWQ3M2Ey"
                                  "N2ZiNTdlOQ==")
    assert len(crypto.INITIAL_PIH) == 88
    # It is base64 of the hex text of SHA-256("0") (the module stores the literal, never computes it).
    assert base64.b64decode(crypto.INITIAL_PIH).decode() == hashlib.sha256(b"0").hexdigest()


def test_r14_signature_over_raw_hash_verifies_and_is_der():
    key = crypto.generate_private_key()
    raw = hashlib.sha256(b"x").digest()
    sig = crypto.sign_hash(key, raw)
    assert sig[0] == 0x30 and 68 <= len(sig) <= 72
    assert crypto.verify_hash_signature(key.public_key(), sig, raw)
    assert not crypto.verify_hash_signature(key.public_key(), sig, base64.b64encode(raw))
    with pytest.raises(ValueError):
        crypto.sign_hash(key, base64.b64encode(raw))  # only the raw 32 bytes are signed


def _values(**over):
    v = dict(seller_name="شركة", vat_number="311111111100003", timestamp="2026-10-15T07:00:00Z", total="115.00",
             vat_total="15.00", invoice_hash="A" * 44, signature_b64="B" * 96, public_key_der=b"\x30" * 88,
             certificate_signature=b"\x30" * 71, signature_r_s=(5, 7))
    v.update(over)
    return v


def test_r15_dtg_layout_tags_1_to_9(settings):
    decoded = qr.decode(qr.encode(qr.simplified_values(**_values())))
    assert sorted(decoded) == list(range(1, 10))
    assert decoded[6] == b"A" * 44  # tag 6 = the base64 hash as text (SDK findings §5)
    assert decoded[7] == b"B" * 96 and decoded[8] == b"\x30" * 88 and decoded[9] == b"\x30" * 71


def test_r15_old_sdk_layout_only_by_setting(settings):
    settings.ZATCA_QR_TAGS_7_9_LAYOUT = qr.LAYOUT_SDK_203
    decoded = qr.decode(qr.encode(qr.simplified_values(**_values())))
    assert decoded[7] == b"\x30" * 88 and decoded[8] == b"\x05" and decoded[9] == b"\x07"
    settings.ZATCA_QR_TAGS_7_9_LAYOUT = "something-else"
    with pytest.raises(ImproperlyConfigured):
        qr.layout()
    settings.ZATCA_QR_TAGS_7_9_LAYOUT = qr.LAYOUT_DTG
    settings.ZATCA_QR_TLV_LENGTH_FORM = "ber"
    with pytest.raises(ImproperlyConfigured):
        qr.tlv(1, b"x")


def test_r15_value_cap_127_bytes():
    assert qr.tlv(1, b"x" * 127)[:2] == bytes([1, 127])
    with pytest.raises(qr.QrValueTooLong):
        qr.tlv(1, b"x" * 128)


def test_at39_qr_length_700_passes_longer_refused():
    """R-37: at most 700 base64 characters. Base64 lengths are multiples of 4, so the first refused length is
    704 (the spec's "701" cannot occur)."""
    values = [(t, b"x" * 127) for t in range(1, 5)] + [(5, b"y" * 7)]  # 4 * 129 + 9 = 525 bytes
    assert len(qr.encode(values)) == 700
    with pytest.raises(qr.QrTooLong) as exc:
        qr.encode(values[:4] + [(5, b"y" * 8)])  # 526 bytes -> 704 characters
    assert exc.value.length == 704


@pytest.mark.parametrize("environment, name", [
    ("production", "ZATCA-Code-Signing"), ("simulation", "PREZATCA-Code-Signing"),
    ("sandbox", "TSTZATCA-Code-Signing")])
def test_oq26_csr_template_name_per_environment(environment, name):
    """§3.2: SDK R3.4.8 -csr / -sim / -nonprod (findings §3.6)."""
    assert crypto.template_name(environment) == name
    key = crypto.generate_private_key()
    csr = crypto.build_csr(key, common_name="TST-1-311111111100003", org_unit="Branch", org_name="Fake Co",
                           serial_number="1-a|2-b|3-c", vat_number="311111111100003", invoice_type="1100",
                           location="ABCD1234", industry="Real estate", environment=environment)
    from cryptography import x509
    from cryptography.x509.oid import ObjectIdentifier

    ext = csr.extensions.get_extension_for_oid(ObjectIdentifier("1.3.6.1.4.1.311.20.2")).value.value
    assert ext == bytes([0x0C, len(name)]) + name.encode()


def test_r7_otp_format():
    assert rules.otp_ok("123456")
    for bad in ("12345", "12a456", "1234567", "١٢٣٤٥٦", 123456, None):
        assert not rules.otp_ok(bad)


def test_at59_archive_file_name():
    assert rules.archive_file_name("311111111100003", "2026-10-15", "07:00:00", "TST1-INV-2026-000001") == \
        "311111111100003_20261015_070000_TST1-INV-2026-000001.xml"
    assert rules.archive_file_name("311111111100003", "2026-10-15", "07:00:00", "A/B 1") == \
        "311111111100003_20261015_070000_A-B-1.xml"


def test_r38_retention_rules():
    from datetime import date

    assert rules.retention_until("standard", date(2027, 3, 10), "monthly") == date(2033, 3, 31)
    assert rules.retention_until("standard", date(2027, 3, 10), "quarterly") == date(2033, 3, 31)
    assert rules.retention_until("standard", date(2027, 2, 10), "quarterly") == date(2033, 3, 31)
    assert rules.retention_until("capital_asset_real_estate", date(2027, 3, 10), "monthly") == date(2042, 3, 31)
    # AT-58: max(2033-03-31, 2027-03-10 + 5 + 5 years)
    assert rules.retention_until("movable_capital_asset", date(2027, 3, 10), "monthly",
                                 acquisition_date=date(2027, 3, 10), adjustment_years=5) == date(2037, 3, 10)
    # never earlier than the standard date; no adjustment period configured -> the standard date (OQ-31)
    assert rules.retention_until("movable_capital_asset", date(2027, 3, 10), "monthly",
                                 acquisition_date=date(2020, 1, 1), adjustment_years=1) == date(2033, 3, 31)
    assert rules.retention_until("movable_capital_asset", date(2027, 3, 10), "monthly",
                                 acquisition_date=date(2027, 3, 10)) == date(2033, 3, 31)


def test_r48_seller_id_priority():
    assert rules.seller_id("1010000001", "7000000001") == ("CRN", "1010000001")
    assert rules.seller_id("", "7000000002") == ("700", "7000000002")
    assert rules.seller_id("", "", "MOM", "M123") == ("MOM", "M123")
    assert rules.seller_id("1010000001", "", "MOM", "M123") == ("CRN", "1010000001")  # CRN comes first
    assert rules.seller_id("", "") == ("", "")
