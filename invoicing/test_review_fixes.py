"""Regression tests for the slice-1 code review (review items 1 to 17; reviewer probes probe4 p1 to p5)."""
from datetime import date, time
from decimal import Decimal
from unittest import mock

import pytest
from django.db import connection

from core import access
from invoicing import services
from invoicing.conftest import D, approve, final, invoice, usd_rate
from invoicing.models import InvoicingError, SalesDocument
from invoicing.test_purchases import bill
from invoicing.testing import approval_day
from ledger import services as ledger
from ledger.models import FiscalPeriod

pytestmark = pytest.mark.django_db


def approve_on(w, doc, day, at=time(9, 30), user=None):
    """CFO approval on ``day`` in Riyadh, running the after-commit ZATCA hand-off like ``conftest.approve``."""
    before = len(connection.run_on_commit)
    with approval_day(day, at):
        result = services.approve_sales_document(doc, user=user or w.cfo)
    for _, callback, _ in connection.run_on_commit[before:]:
        callback()
    del connection.run_on_commit[before:]
    stored = SalesDocument.objects.get(pk=result.pk)
    stored.warnings = result.warnings
    return stored


# --- 1. over-allocation: applications are summed per target --------------------------------------

def test_p1_two_receipt_applications_cannot_overallocate_one_invoice(w):
    inv = final(w, invoice(w))  # gross 1,150.00
    r = services.create_receipt(w.alm, user=w.clerk, customer=w.customer, date=date(2027, 1, 20), amount="2300.00")
    services.add_receipt_application(r, inv, "1150.00", user=w.clerk)
    with pytest.raises(InvoicingError) as exc:
        services.add_receipt_application(r, inv, "1150.00", user=w.clerk)
    assert exc.value.code == "application_invalid"
    with pytest.raises(InvoicingError):
        services.add_receipt_application(r, inv, "0.01", user=w.clerk)  # nothing more fits
    services.post_receipt(r, user=w.clerk2)
    assert services.open_amount(inv, user=w.clerk) == D("0.00")


def test_p1_posting_sums_applications_per_target(w):
    """Applications that each fit, but together exceed the open amount, are refused when the receipt posts."""
    inv = final(w, invoice(w))
    r = services.create_receipt(w.alm, user=w.clerk, customer=w.customer, date=date(2027, 1, 20), amount="2300.00")
    services.add_receipt_application(r, inv, "600.00", user=w.clerk)
    services.add_receipt_application(r, inv, "550.00", user=w.clerk)
    other = services.create_receipt(w.alm, user=w.clerk, customer=w.customer, date=date(2027, 1, 20), amount="600")
    services.add_receipt_application(other, inv, "600.00", user=w.clerk)
    services.post_receipt(other, user=w.clerk2)  # the invoice now has 550.00 open; r plans 1,150.00 on it
    with pytest.raises(InvoicingError) as exc:
        services.post_receipt(r, user=w.clerk2)
    assert exc.value.code == "document_invalid"
    r.refresh_from_db()
    assert (r.status, r.number) == ("draft", "")
    assert services.open_amount(inv, user=w.clerk) == D("550.00")


def test_p1b_two_payment_applications_cannot_overallocate_one_bill(w):
    b = services.approve_purchase_document(bill(w), user=w.cfo)
    b.refresh_from_db()
    gross = b.total_gross
    p = services.create_vendor_payment(w.alm, user=w.ap, vendor=b.vendor, date=date(2027, 1, 25), amount=str(gross * 2))
    services.add_payment_application(p, b, str(gross), user=w.ap)
    with pytest.raises(InvoicingError) as exc:
        services.add_payment_application(p, b, str(gross), user=w.ap)
    assert exc.value.code == "application_invalid"
    services.submit_vendor_payment(p, user=w.ap)
    services.approve_vendor_payment(p, user=w.cfo)
    assert services.open_amount(b, user=w.ap) == D("0.00")


def test_p1b_payment_posting_sums_applications_per_target(w):
    b = services.approve_purchase_document(bill(w), user=w.cfo)  # gross 1,150.00
    p = services.create_vendor_payment(w.alm, user=w.ap, vendor=w.vendor, date=date(2027, 1, 25), amount="2000")
    services.add_payment_application(p, b, "700.00", user=w.ap)
    services.add_payment_application(p, b, "450.00", user=w.ap)
    first = services.create_vendor_payment(w.alm, user=w.ap, vendor=w.vendor, date=date(2027, 1, 25), amount="100")
    services.add_payment_application(first, b, "100.00", user=w.ap)
    services.submit_vendor_payment(first, user=w.ap)
    services.approve_vendor_payment(first, user=w.cfo)
    services.submit_vendor_payment(p, user=w.ap)
    with pytest.raises(InvoicingError):
        services.approve_vendor_payment(p, user=w.cfo)
    p.refresh_from_db()
    assert (p.status, p.number) == ("submitted", "")


def test_p1_foreign_currency_applications_are_summed_per_target(w):
    usd_rate(w, "3.75", on=date(2027, 1, 10))
    inv = final(w, invoice(w, [("rent_commercial", "1", "1000.00")], currency="USD"))  # USD 1,150.00
    r = services.create_receipt(w.alm, user=w.clerk, customer=w.customer, date=date(2027, 1, 12), amount="2300.00",
                                currency="USD")
    services.add_receipt_application(r, inv, "1000.00", user=w.clerk)
    with pytest.raises(InvoicingError):
        services.add_receipt_application(r, inv, "151.00", user=w.clerk)
    services.add_receipt_application(r, inv, "150.00", user=w.clerk)
    services.post_receipt(r, user=w.clerk2)
    assert services.open_amount(inv, user=w.clerk) == D("0.00")


# --- 5. issue date = the approval day in Riyadh (BR-KSA-04, compliance 6b) ------------------------

def test_6b_issue_date_is_the_approval_day_and_the_draft_date_is_kept_as_requested(w):
    backdated = invoice(w, issue_date=date(2027, 1, 5))
    issued = approve_on(w, backdated, date(2027, 1, 10), time(14, 5, 7))
    assert (issued.issue_date, issued.issue_time, issued.requested_issue_date) == (
        date(2027, 1, 10), time(14, 5, 7), date(2027, 1, 5))
    assert any("2027-01-05" in warning for warning in issued.warnings)
    posted = w.zatca.clear(issued)
    assert posted.journal_entry.date == date(2027, 1, 10)


def test_6b_a_future_draft_date_is_never_the_issue_date(w):
    future = invoice(w, issue_date=date(2027, 2, 1), supply_date=date(2027, 1, 8))
    issued = approve_on(w, future, date(2027, 1, 10))
    assert issued.issue_date == date(2027, 1, 10) and issued.requested_issue_date == date(2027, 2, 1)
    assert issued.number == "ALM-INV-2027-000001"


def test_6b_issue_time_follows_the_riyadh_clock(w):
    """The real clock: issue date and time come from the same moment in Asia/Riyadh."""
    from datetime import datetime, timezone as tz

    moment = datetime(2027, 1, 10, 21, 30, tzinfo=tz.utc)  # 00:30 on 11 January in Riyadh
    with mock.patch("invoicing.services.timezone.now", return_value=moment):
        now = services._riyadh_now()
    assert (now.date(), now.time()) == (date(2027, 1, 11), time(0, 30))


# --- 3. stale approvals: a grant checked before the document last changed is refused ---------------

def _race(permission, action):
    """Patch access.require so ``action`` runs right after the ``permission`` grant is taken (before the lock)."""
    real = access.require

    def require_then_race(user, entity, perm, **kw):
        grant = real(user, entity, perm, **kw)
        if perm == permission:
            action()
        return grant

    return mock.patch.object(services.access, "require", side_effect=require_then_race)


def test_p2_approval_refused_when_the_document_changed_after_the_grant(w):
    doc = invoice(w)

    def edit():
        services.return_to_draft(doc, user=w.clerk2)
        services.update_sales_document(doc, user=w.clerk2, notes_en="changed", notes_ar="تغير")
        services.submit_sales_document(doc, user=w.clerk2)

    with _race("invoicing.approve_sales_document", edit), pytest.raises(access.AccessDenied) as exc:
        approve(w, doc)
    assert exc.value.code == "grant_document_changed"
    doc.refresh_from_db()
    assert (doc.status, doc.number) == ("submitted", "")
    posted = final(w, doc)  # a fresh approval after the change issues it and clearance posts it
    assert posted.status == "final" and posted.journal_entry.status == "posted"


def test_p2_receipt_posting_refused_when_an_application_was_added_after_the_grant(w):
    inv = final(w, invoice(w))
    r = services.create_receipt(w.alm, user=w.clerk, customer=w.customer, date=date(2027, 1, 20), amount="1150.00")

    def add():
        services.add_receipt_application(r, inv, "1150.00", user=w.clerk)

    with _race("invoicing.post_receipt", add), pytest.raises(access.AccessDenied) as exc:
        services.post_receipt(r, user=w.clerk2)
    assert exc.value.code == "grant_document_changed"
    assert services.post_receipt(r, user=w.clerk2).status == "posted"


@pytest.mark.parametrize("kind", ["bill", "payment"])
def test_p2_bill_and_payment_approval_refused_when_changed_after_the_grant(w, kind):
    from django.db import models
    from django.utils import timezone

    if kind == "bill":
        doc, permission = bill(w), "invoicing.approve_bill"
        approve_ = services.approve_purchase_document
    else:
        doc = services.create_vendor_payment(w.alm, user=w.ap, vendor=w.vendor, date=date(2027, 1, 25), amount="100")
        doc = services.submit_vendor_payment(doc, user=w.ap)
        permission, approve_ = "invoicing.approve_vendor_payment", services.approve_vendor_payment

    def change():  # a write that bypasses the services, after the grant
        models.QuerySet.update(type(doc)._base_manager.filter(pk=doc.pk), modified_at=timezone.now())

    with _race(permission, change), pytest.raises(access.AccessDenied) as exc:
        approve_(doc, user=w.cfo)
    assert exc.value.code == "grant_document_changed"
    doc.refresh_from_db()
    assert (doc.status, doc.number) == ("submitted", "")
