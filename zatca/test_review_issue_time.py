"""Review of 838e89b, finding 4 (probe p4): IssueDate/IssueTime in Riyadh time, QR tag 3 from them,
SigningTime in UTC, archive names in Riyadh time."""
from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone
from types import SimpleNamespace

import pytest

from invoicing import services as invoicing_services
from invoicing.conftest import invoice
from invoicing.testing import approval_day
from zatca import qr, rules, services, ubl
from zatca.conftest import NOW, TODAY, einvoice_of, onboard, run, xml_text
from zatca.testing import FakeZatcaClient

pytestmark = pytest.mark.django_db
T = FakeZatcaClient


@pytest.fixture
def live(zw):
    zw.egs1 = onboard(zw, zw.tst1, zw.admin1)
    return zw


# --- finding 4 (probe p4): issue date and time in Riyadh -------------------------------------------------------

def _issue_at(zw, entity, day, at, simplified=False):
    """An issued document approved at ``at`` o'clock Riyadh time on ``day`` (invoicing R78)."""
    from django.db import connection

    customer = zw.customers[entity.code + ("-B2C" if simplified else "")]
    w = SimpleNamespace(alm=entity, clerk=zw.clerk, customer=customer, cfo=zw.cfo)
    fields = {"zatca_profile": "simplified"} if simplified else {}
    doc = invoice(w, (("management_fee", "1", "100.00"),), customer=customer, user=zw.clerk, issue_date=day,
                  entity=entity, **fields)
    before = len(connection.run_on_commit)
    with approval_day(day, at=at):
        doc = invoicing_services.approve_sales_document(doc, user=zw.cfo)
    for _, callback, _ in connection.run_on_commit[before:]:
        callback()
    del connection.run_on_commit[before:]
    return doc


@pytest.mark.parametrize("at", [time(0, 0, 0), time(1, 30, 0), time(2, 59, 59)])
def test_f4_early_morning_riyadh_keeps_the_riyadh_date(live, at):
    doc = _issue_at(live, live.tst1, TODAY, at, simplified=True)
    einv = einvoice_of(doc)
    assert einv.issue_datetime.astimezone(dt_timezone.utc).date() == TODAY - timedelta(days=1)  # stored in UTC
    run(live)
    einv.refresh_from_db()
    xml = einv.signed_xml.encode()
    assert xml_text(xml, "/*/cbc:IssueDate") == TODAY.isoformat()
    assert xml_text(xml, "/*/cbc:IssueTime") == at.strftime("%H:%M:%S")  # KSA-25 in AST, no suffix
    assert qr.decode(einv.qr_tlv)[3].decode() == f"{TODAY.isoformat()}T{at:%H:%M:%S}"  # R-15: no "Z" (§7.3)
    assert xml_text(xml, "//xades:SigningTime") == NOW.strftime("%Y-%m-%dT%H:%M:%SZ")  # SigningTime stays UTC
    name, _ = services.export_archive(einv, user=live.accountant)
    assert name.startswith(f"311111111100003_{TODAY:%Y%m%d}_{at:%H%M%S}_")


def test_f4_month_end_in_riyadh():
    moment = services._issue_moment(SimpleNamespace(issue_date=date(2026, 11, 1), issue_time=time(0, 15)))
    assert moment == datetime(2026, 10, 31, 21, 15, tzinfo=dt_timezone.utc)
    data = SimpleNamespace(issue=moment)
    for name in ("issue_local", "issue_date_text", "issue_time_text", "qr_timestamp"):
        setattr(data, name, getattr(ubl.InvoiceData, name).fget(data))
    assert (data.issue_date_text, data.issue_time_text) == ("2026-11-01", "00:15:00")
    assert data.qr_timestamp == "2026-11-01T00:15:00"
    assert rules.archive_file_name("3" * 15, data.issue_date_text, data.issue_time_text, "INV-1") == \
        "333333333333333_20261101_001500_INV-1.xml"


def test_f4_month_end_document_xml(live):
    day = date(2026, 10, 1)
    doc = _issue_at(live, live.tst1, day, time(0, 15))
    run(live)
    einv = einvoice_of(doc)
    assert einv.status == "cleared"
    assert xml_text(einv.signed_xml, "/*/cbc:IssueDate") == "2026-10-01"  # UTC would say 2026-09-30
    assert xml_text(einv.signed_xml, "/*/cbc:IssueTime") == "00:15:00"
    assert qr.decode(einv.qr_tlv)[3] == b"2026-10-01T00:15:00"  # ZATCA's QR (fake) from the same fields, no "Z"
