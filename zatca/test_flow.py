"""End-to-end flow: onboarding, issue in invoicing, stamping, clearance / reporting, posting (§5, R-20 to R-26)."""
import base64

import pytest

from invoicing.models import SalesDocument
from zatca import crypto, qr, services, ubl
from zatca.conftest import NOW, einvoice_of, issue, onboard, run
from zatca.models import EgsUnit, EInvoice, SubmissionAttempt, ValidationMessage
from zatca.testing import FakeZatcaClient, zatca_error, zatca_warning

pytestmark = pytest.mark.django_db


def test_at17_standard_cleared_and_posted(zw):
    egs = onboard(zw, zw.tst1, zw.admin1)
    assert egs.status == "active"
    doc = issue(zw, zw.tst1)
    einv = einvoice_of(doc)
    assert einv.status == "pending" and einv.api == "clearance"
    assert not services.is_issued(doc)
    run(zw)
    einv.refresh_from_db()
    assert einv.status == "cleared" and einv.cleared_xml and einv.icv == 1
    assert services.is_issued(doc)
    doc.refresh_from_db()
    assert doc.status == SalesDocument.Status.FINAL and doc.journal_entry_id  # invoicing posted it (D-8)
    assert doc.zatca_status == "cleared" and doc.zatca_icv == 1 and doc.zatca_hash == einv.invoice_hash
    assert doc.zatca_qr == ubl.qr_from_xml(einv.cleared_xml.encode())  # R-15: the cleared QR


def test_at20_simplified_issued_at_stamping_then_reported(zw):
    onboard(zw, zw.tst1, zw.admin1)
    doc = issue(zw, zw.tst1, simplified=True)
    einv = einvoice_of(doc)
    assert einv.reporting_deadline == einv.issue_datetime + __import__("datetime").timedelta(hours=24)
    zw.fakes.client.script["reporting"] = [FakeZatcaClient.timeout()]
    run(zw)
    einv.refresh_from_db()
    assert einv.status == "failed_retryable" and einv.icv == 1
    assert services.is_issued(doc)  # R-23: issued at stamping, before reporting
    doc.refresh_from_db()
    assert doc.zatca_status == "pending" and doc.zatca_qr == einv.qr_tlv and doc.status == "issued"
    run(zw, NOW + __import__("datetime").timedelta(minutes=5))
    einv.refresh_from_db()
    assert einv.status == "reported"
    doc.refresh_from_db()
    assert doc.status == "final" and doc.journal_entry_id
