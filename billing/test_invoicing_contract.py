"""What the real invoicing hooks must satisfy: the contract in ``billing.invoicing_port`` (billing.md R13, R43, R46c,
R52 to R56; review finding 5).

These tests run billing against the **real** port (``InvoicingServicesPort`` -> ``invoicing.services``), not the
fake. They are skipped until invoicing implements the hooks (today ``create_from_billing`` and
``create_adjustment_from_billing`` are stubs and ``discard_adjustment_draft`` does not exist). When invoicing
builds them, remove nothing here: the skip lifts by itself once ``invoicing.services.discard_adjustment_draft``
exists, and every test below must pass. Item numbers refer to the port contract.
"""
from datetime import date
from decimal import Decimal

import pytest
from django.db import transaction

from billing import invoicing_port, services
from billing.conftest import approved, clear, period, run, set_auto_issue, version
from billing.models import BillingAdjustment
from core import access
from core.access.models import AccessLog
from invoicing import services as inv
from invoicing.models import InvoicingError, SalesDocument
from ledger.models import FiscalPeriod

D = Decimal


def _hooks_built():
    return hasattr(inv, "discard_adjustment_draft")


pytestmark = pytest.mark.skipif(not _hooks_built(), reason="invoicing billing hooks not built yet (billing.md "
                                                           "OQ-22); this module is the contract they must satisfy")


@pytest.fixture
def real(w, monkeypatch):
    """The billing world with the real invoicing port instead of the fake."""
    monkeypatch.setattr(invoicing_port, "_override", None)
    assert isinstance(invoicing_port.get_port(), invoicing_port.InvoicingServicesPort)
    return w


def _built(call):
    try:
        return call()
    except NotImplementedError:
        pytest.skip("invoicing hook still a stub")


# --- items 1 to 4, 6, 8: invoices -------------------------------------------------------------------------------

def test_invoice_by_the_system_user_linked_to_its_period_never_approved_as_the_cfo(real):
    w = real
    c = approved(w, start=date(2027, 3, 1), due_days=10)
    _built(lambda: run(w, date(2027, 3, 1)))
    p = period(c, date(2027, 3, 1))
    doc = SalesDocument.objects.get(pk=p.sales_document_id)
    assert (doc.source, doc.created_by, doc.status) == ("system:billing", w.sys, "issued")  # items 1, 8
    assert doc.billing_item_id == p.pk  # item 3
    assert doc.approved_by == w.c  # recorded from billed_version (R53) ...
    assert not AccessLog.objects.filter(actor=w.c, permission="invoicing.approve_sales_document").exists()  # item 2
    assert doc.due_date == date(2027, 3, 11)  # item 6: billed_version.due_days
    assert [line.net_amount for line in doc.lines.all()] == [D(r["net"]) for r in p.invoice_lines]
    assert clear(w, doc).journal_entry.posted_by == w.c  # R53: the contract approval is the recorded approval


def test_invoice_is_idempotent_and_unique_per_period_in_the_database(real):
    w = real
    c = approved(w, start=date(2027, 3, 1))
    _built(lambda: run(w, date(2027, 3, 1)))
    p = period(c, date(2027, 3, 1))
    port = invoicing_port.get_port()
    assert port.create_from_billing(p, w.sys).pk == p.sales_document_id  # item 4: second call, same document
    doc = SalesDocument.objects.get(pk=p.sales_document_id)
    unique = [con for con in SalesDocument._meta.constraints
              if getattr(con, "fields", ()) == ("billing_item",) and con.condition is not None]
    assert unique, "item 4: a partial unique constraint on billing_item (non-rejected documents)"
    w.zatca.reject(doc)
    run(w, date(2027, 3, 2))
    services.release_period(period(c, date(2027, 3, 1)), user=w.s)
    run(w, date(2027, 3, 3))
    assert SalesDocument.objects.filter(billing_item=p.pk).count() == 2  # a rejection frees the period (AT-41)


def test_only_the_billing_system_user_calls_the_hooks(real):
    w = real
    c = approved(w, start=date(2027, 3, 1))
    p = period(c, date(2027, 3, 1))
    with pytest.raises(access.AccessDenied):  # item 1
        _built(lambda: invoicing_port.get_port().create_from_billing(p, w.s))


def test_auto_issue_switch_off_leaves_the_invoice_submitted(real):
    w = real
    set_auto_issue(w.alm, False)
    c = approved(w, start=date(2027, 3, 1))
    _built(lambda: run(w, date(2027, 3, 1)))
    p = period(c, date(2027, 3, 1))
    assert (p.status, p.sales_document.status) == ("generated", "submitted")  # item 8


# --- item 5: refusals by code, all-or-nothing ---------------------------------------------------------------------

def _refusal(w, c, day):
    p = period(c, day)
    with pytest.raises((InvoicingError, access.AccessDenied)) as exc:
        with transaction.atomic():
            p.invoice_lines, p.billed_version_id = services._invoice_lines(p, c), c.current_version_id
            services._save(p)
            _built(lambda: invoicing_port.get_port().create_from_billing(p, w.sys))
    assert not SalesDocument.objects.filter(billing_item=p.pk).exists()  # nothing created, no number used
    return getattr(exc.value, "code", None)


def test_refusals_have_distinct_codes(real):
    from billing.test_runs import _rate

    w = real
    eur = approved(w, start=date(2027, 3, 1), currency="EUR", lines=(("rent_commercial", "1000.00"),))
    w.clock.set(date(2027, 3, 1))
    assert _refusal(w, eur, date(2027, 3, 1)) == "rate_required"
    _rate(w, "KWD", "12.2", date(2027, 1, 15))
    kwd = approved(w, start=date(2027, 1, 15), currency="KWD", lines=(("rent_commercial", "1000.000"),))
    w.clock.set(date(2027, 1, 15))
    assert _refusal(w, kwd, date(2027, 1, 15)) == "third_decimal_pending"  # 548.387 (AT-45)
    sar = approved(w, start=date(2027, 3, 1))
    FiscalPeriod.objects.filter(entity=w.alm, name="2027-03").update(status="closed")
    w.clock.set(date(2027, 3, 1))
    code = _refusal(w, sar, date(2027, 3, 1))
    assert code and code not in ("invalid", "rate_required", "third_decimal_pending")  # an open-period code


# --- items 3, 7, 9: correction notes --------------------------------------------------------------------------

def test_correction_draft_read_only_lines_and_discard(real):
    w = real
    c = approved(w, start=date(2027, 3, 1))
    _built(lambda: run(w, date(2027, 3, 1)))
    clear(w, period(c, date(2027, 3, 1)).sales_document)
    v = _built(lambda: services.approve_version(version(w, c, effective=date(2027, 3, 1), amount="9000.00"),
                                                user=w.c))
    (adj,) = BillingAdjustment.objects.filter(contract=c)
    note = SalesDocument.objects.get(pk=adj.document_id)
    assert (note.doc_type, note.status, note.source, note.billing_adjustment_id) == (
        "credit_note", "draft", "system:billing", adj.pk)
    assert note.adjustment_event_date is None and v.status == "approved"
    assert invoicing_port.get_port().create_adjustment_from_billing(adj, w.sys).pk == note.pk  # idempotent
    with pytest.raises(InvoicingError):  # item 7: amounts read-only for every user
        inv.add_sales_line(note, user=w.s, income_type="rent_commercial", quantity="1", unit_price="1.00",
                           description_ar="بند")
    inv.update_sales_document(note, user=w.s, payment_means_code="30")  # header fields: accepted
    inv.submit_sales_document(note, user=w.s)
    services.approve_version(version(w, c, effective=date(2027, 3, 1), amount="9500.00"), user=w.c)
    assert not SalesDocument.objects.filter(pk=note.pk).exists()  # item 9: submitted draft returned and deleted
    issued_adj = BillingAdjustment.objects.filter(contract=c, status="drafted").get()
    issued = SalesDocument.objects.get(pk=issued_adj.document_id)
    inv.update_sales_document(issued, user=w.s, payment_means_code="30")
    inv.submit_sales_document(issued, user=w.s)
    issued = inv.approve_sales_document(SalesDocument.objects.get(pk=issued.pk), user=w.c)
    with pytest.raises((InvoicingError, access.AccessDenied)):  # item 9: never an issued note
        invoicing_port.get_port().discard_adjustment_draft(issued, w.sys)
