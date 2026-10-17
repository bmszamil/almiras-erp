"""Treaty certificates (invoicing R62, 3.2; review findings 4 and 6): written only through the services, every edit
records its author, a second person (neither creator nor modifier) verifies, the scan is a PDF, PNG or JPG of at
most 10 MB, the country is the vendor's residence and the rate never exceeds the statutory one. Made-up data."""
from datetime import date

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from core import access
from core.access.testing import make_user, member
from invoicing import services
from invoicing.conftest import D
from invoicing.models import InvoicingError, TreatyCertificate, WhtDeduction
from invoicing.test_withholding_tax import nr_bill, nr_vendor, pay

pytestmark = pytest.mark.django_db

PDF = b"%PDF-1.7\n% made-up certificate\n"


def cert(w, vendor=None, **fields):
    values = dict(country="GB", treaty_rate="5.00", valid_from=date(2027, 1, 1), valid_to=date(2027, 6, 30),
                  received_on=date(2027, 1, 2))
    return services.add_treaty_certificate(vendor or nr_vendor(w), user=w.ap, **{**values, **fields})


def second_accountant(w):
    user = make_user("accountant2")
    member(user, w.alm, "accountant")
    return user


# --- finding 4: guarded writes, recorded modifier, verification by a third person --------------------------------

def test_bulk_update_and_delete_are_refused_like_wht_rows(w):
    """Reviewer probe: QuerySet.update set a verified certificate's rate to 0.00."""
    c = services.verify_treaty_certificate(cert(w), user=w.accountant)
    for attempt in (lambda: TreatyCertificate.objects.filter(pk=c.pk).update(treaty_rate=D("0.00")),
                    lambda: TreatyCertificate.objects.filter(pk=c.pk).delete()):
        with pytest.raises(InvoicingError):
            attempt()
    c.refresh_from_db()
    assert c.treaty_rate == D("5.00")
    unverified = cert(w, vendor=c.vendor)
    with pytest.raises(InvoicingError) as exc:  # unverified ones too: only through the services
        TreatyCertificate.objects.filter(pk=unverified.pk).update(treaty_rate=D("0.00"))
    assert exc.value.code == "service_only"


def test_direct_save_is_refused_and_edits_record_the_modifier(w):
    """Reviewer probe: an ORM save changed an unverified certificate with no modifier recorded, then it was
    verified. Now only update_treaty_certificate changes it, and the modifier cannot verify it."""
    c = cert(w)
    c.treaty_rate = D("0.00")
    with pytest.raises(InvoicingError) as exc:
        c.save()
    assert exc.value.code == "service_only"
    assert TreatyCertificate.objects.get(pk=c.pk).treaty_rate == D("5.00")
    c = services.update_treaty_certificate(c, user=w.accountant, treaty_rate="0.00")
    assert (c.treaty_rate, c.modified_by) == (D("0.00"), w.accountant) and c.modified_at is not None
    with pytest.raises(access.AccessDenied) as denied:  # the modifier never verifies it
        services.verify_treaty_certificate(c, user=w.accountant)
    assert denied.value.code == "self_approval"
    with pytest.raises(access.AccessDenied):  # nor the creator (who holds no verify right anyway)
        services.verify_treaty_certificate(c, user=w.ap)
    c = services.verify_treaty_certificate(c, user=second_accountant(w))
    with pytest.raises(InvoicingError) as exc:
        services.update_treaty_certificate(c, user=w.ap, treaty_rate="1.00")
    assert exc.value.code == "certificate_verified"
    with pytest.raises(InvoicingError) as exc:
        services.update_treaty_certificate(cert(w, vendor=c.vendor), user=w.ap, vendor=w.vendor)
    assert exc.value.code == "field_not_editable"


def test_upload_type_and_size_are_validated(w, settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    vendor = nr_vendor(w)
    cases = [
        (SimpleUploadedFile("certificate.exe", PDF), "certificate_file_type"),
        (SimpleUploadedFile("certificate.pdf", b"MZ\x90\x00 not a pdf"), "certificate_file_type"),
        (SimpleUploadedFile("certificate.png", PDF), "certificate_file_type"),
        (SimpleUploadedFile("certificate.pdf", PDF + b"0" * (10 * 1024 * 1024)), "certificate_file_size"),
    ]
    for upload, code in cases:
        with pytest.raises(InvoicingError) as exc:
            cert(w, vendor=vendor, document=upload)
        assert exc.value.code == code
    assert not TreatyCertificate.objects.exists()
    ok = cert(w, vendor=vendor, document=SimpleUploadedFile("certificate.pdf", PDF))
    assert ok.document.name.endswith(".pdf")
    png = cert(w, vendor=vendor, document=SimpleUploadedFile("scan.PNG", b"\x89PNG\r\n\x1a\n" + b"0" * 32))
    jpg = services.update_treaty_certificate(png, user=w.ap, document=SimpleUploadedFile(
        "scan.jpg", b"\xff\xd8\xff\xe0" + b"0" * 32))
    assert jpg.document.name.endswith(".jpg")
    with pytest.raises(InvoicingError) as exc:
        services.update_treaty_certificate(png, user=w.ap, document=SimpleUploadedFile("scan.gif", b"GIF89a"))
    assert exc.value.code == "certificate_file_type"


# --- finding 6: country of residence and statutory cap ------------------------------------------------------------

def test_country_must_be_the_vendors_residence(w):
    vendor = nr_vendor(w)  # resident in GB
    with pytest.raises(InvoicingError) as exc:
        cert(w, vendor=vendor, country="FR")
    assert exc.value.code == "treaty_country_mismatch" and "الإقامة" in str(exc.value)
    c = cert(w, vendor=vendor, country="gb")
    assert c.country == "GB"
    with pytest.raises(InvoicingError) as exc:
        services.update_treaty_certificate(c, user=w.ap, country="DE")
    assert exc.value.code == "treaty_country_mismatch"
    services.update_vendor(vendor, user=w.ap, residence_country="DE")  # moved after the certificate was entered
    with pytest.raises(InvoicingError) as exc:  # checked again at verification
        services.verify_treaty_certificate(c, user=w.accountant)
    assert exc.value.code == "treaty_country_mismatch"
    assert TreatyCertificate.objects.get(pk=c.pk).verified_at is None


def test_treaty_rate_never_above_the_statutory_rate(w):
    vendor = nr_vendor(w)
    with pytest.raises(InvoicingError) as exc:  # technical services: 5% (OQ-8 reference row)
        cert(w, vendor=vendor, treaty_rate="6.00", wht_category="technical_services")
    assert exc.value.code == "treaty_rate_above_statutory" and "5.00" in str(exc.value) and "النظامية" in str(exc.value)
    assert cert(w, vendor=vendor, treaty_rate="10.00", wht_category="royalties").treaty_rate == D("10.00")  # 15%
    with pytest.raises(InvoicingError) as exc:  # all categories: never above the lowest rate (5%)
        cert(w, vendor=vendor, treaty_rate="7.50")
    assert exc.value.code == "treaty_rate_above_statutory"
    c = cert(w, vendor=vendor, treaty_rate="2.50")
    with pytest.raises(InvoicingError) as exc:
        services.update_treaty_certificate(c, user=w.ap, treaty_rate="5.01")
    assert exc.value.code == "treaty_rate_above_statutory"


def test_payment_never_applies_more_than_the_statutory_rate(w):
    """Defence in depth: even a certificate above the statutory rate would not raise the WHT."""
    vendor = nr_vendor(w)
    c = services.verify_treaty_certificate(cert(w, vendor=vendor, treaty_rate="5.00"), user=w.accountant)
    p = pay(w, vendor, "950.00", [(nr_bill(w, vendor, "1000.00"), "1000.00")], on=date(2027, 1, 25))
    row = WhtDeduction.objects.get(payment=p)
    assert (row.rate_applied, row.treaty_certificate) == (D("5.00"), c)
