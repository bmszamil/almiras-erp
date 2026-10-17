"""Billing hooks (invoicing R60, R73; billing.md R13, R43, R46c, R52 to R56; AT-53, AT-67), and the posting of
auto-issued invoices on the CFO's contract approval (billing R53).

These run billing against the real invoicing services (not ``billing.testing.FakeInvoicing``), with the billing
fixtures of ``billing.conftest`` (anonymised entities ALM and SUB, made-up numbers, R70).
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from billing import invoicing_port
from billing import services as billing
from billing.conftest import agent, approved, clear, clock, period, run, set_auto_issue, version, w  # noqa: F401
from billing.models import BillingAdjustment
from core import access
from core.access.models import AccessLog
from invoicing import services as inv
from invoicing.models import DocumentSequence, InvoicingError, SalesDocument, SalesDocumentLine
from ledger import documents
from ledger import services as ledger
from ledger.models import FiscalPeriod

D = Decimal
pytestmark = pytest.mark.django_db


@pytest.fixture
def bw(w, monkeypatch):
    """The billing world with the real invoicing hooks (no fake)."""
    monkeypatch.setattr(invoicing_port, "_override", None)
    return w


def _invoice(w, c, day):
    run(w, day)
    return SalesDocument.objects.get(pk=period(c, day).sales_document_id)


def _frozen(w, c, day):
    """The period of ``day`` with its lines frozen, as billing does before calling the hook (R52)."""
    w.clock.set(day)
    p = period(c, day)
    p.invoice_lines, p.billed_version_id = billing._invoice_lines(p, c), c.current_version_id
    billing._save(p)
    return p


# --- R60 / AT-53: invoices --------------------------------------------------------------------------------------

def test_at53_approved_contract_issues_without_per_invoice_approval(bw):
    w = bw
    c = approved(w, start=date(2027, 3, 1), due_days=30)
    doc = _invoice(w, c, date(2027, 3, 1))
    assert (doc.status, doc.source, doc.created_by, doc.submitted_by) == ("issued", "system:billing", w.sys, w.sys)
    assert doc.approved_by == w.c and doc.number == "ALM-INV-2027-000001"
    assert doc.due_date == date(2027, 3, 31)  # billed_version.due_days (billing R52, port item 6)
    assert (doc.supply_date, doc.supply_end_date) == (date(2027, 3, 1), date(2027, 3, 31))
    assert [(l.quantity, l.unit_price) for l in doc.lines.all()] == [(D("1.000"), D("10000.000"))]
    assert not AccessLog.objects.filter(permission="invoicing.approve_sales_document").exists()  # nobody as the CFO
    issued = AccessLog.objects.get(actor=w.sys, details__action="issue_from_contract")
    assert issued.details["approved_by"] == w.c.pk


def test_at53_repeat_call_returns_the_same_document(bw):
    w = bw
    c = approved(w, start=date(2027, 3, 1))
    doc = _invoice(w, c, date(2027, 3, 1))
    again = inv.create_from_billing(period(c, date(2027, 3, 1)), w.sys)
    assert again.pk == doc.pk and SalesDocument.objects.filter(billing_item=doc.billing_item_id).count() == 1
    assert DocumentSequence.objects.get(entity=w.alm, doc_type="INV").last_number == 1


def test_r60_database_allows_one_live_document_per_period(bw):
    w = bw
    c = approved(w, start=date(2027, 3, 1))
    doc = _invoice(w, c, date(2027, 3, 1))
    twin = SalesDocument(entity=w.alm, doc_type="invoice", customer=doc.customer, issue_date=doc.issue_date,
                         currency_id="SAR", created_by=w.sys, billing_item_id=doc.billing_item_id)
    with pytest.raises(IntegrityError), transaction.atomic():
        twin.save()


@pytest.mark.parametrize("who", ["s", "c", "agent"])
def test_at53_only_the_billing_system_user_calls_it(bw, agent, who):  # noqa: F811
    w = bw
    c = approved(w, start=date(2027, 3, 1))
    p = _frozen(w, c, date(2027, 3, 1))
    user = agent if who == "agent" else getattr(w, who)
    with pytest.raises(access.AccessDenied):
        inv.create_from_billing(p, user)
    assert not SalesDocument.objects.exists()


def test_at53_switch_off_leaves_it_submitted_for_the_cfo(bw):
    w = bw
    set_auto_issue(w.alm, False)
    c = approved(w, start=date(2027, 3, 1))
    doc = _invoice(w, c, date(2027, 3, 1))
    assert (doc.status, doc.number, doc.approved_by) == ("submitted", "", None)
    issued = inv.approve_sales_document(doc, user=w.c)  # the normal CFO approval (R57)
    final = clear(w, issued)
    assert (final.status, final.journal_entry.posted_by) == ("final", w.c)


def test_r60_unapproved_version_waits_submitted(bw):
    """A period whose billed version is not approved is never issued by the hook (R60, AT-53 second case)."""
    w = bw
    c = approved(w, start=date(2027, 3, 1))
    p = _frozen(w, c, date(2027, 3, 1))
    v = version(w, c, effective=date(2027, 4, 1), amount="11000.00")  # submitted, not approved
    p.billed_version_id = v.pk
    from django.db.models import QuerySet

    QuerySet.update(type(p).objects.filter(pk=p.pk), billed_version_id=v.pk)  # fixture: a period on that version
    p.refresh_from_db()
    doc = inv.create_from_billing(p, w.sys)
    assert (doc.status, doc.number) == ("submitted", "")


def test_r60_refusals_create_nothing_and_use_no_number(bw):
    w = bw
    c = approved(w, start=date(2027, 3, 1))
    p = _frozen(w, c, date(2027, 3, 1))
    FiscalPeriod.objects.filter(entity=w.alm, name="2027-03").update(status="closed")
    with pytest.raises(InvoicingError) as exc, transaction.atomic():
        inv.create_from_billing(p, w.sys)
    assert exc.value.code == "period_closed" and "مقفلة" in " ".join(exc.value.messages)
    FiscalPeriod.objects.filter(entity=w.alm, name="2027-03").update(status="open")
    type(w.alm).objects.filter(pk=w.alm.pk).update(vat_number="")
    p.entity.refresh_from_db()
    with pytest.raises(InvoicingError) as exc, transaction.atomic():
        inv.create_from_billing(p, w.sys)
    assert exc.value.code == "entity_not_vat_registered"
    assert not SalesDocument.objects.exists()
    assert not DocumentSequence.objects.exclude(last_number=0).exists()


# --- billing R53: posting on the contract approval --------------------------------------------------------------

def test_r53_auto_issued_invoice_posts_on_the_contract_approval(bw):
    w = bw
    c = approved(w, start=date(2027, 3, 1))
    doc = clear(w, _invoice(w, c, date(2027, 3, 1)))
    assert (doc.status, doc.journal_entry.posted_by) == ("final", w.c)
    row = AccessLog.objects.get(details__action="post_on_clearance", details__posted_document=[
        "invoicing.salesdocument", doc.pk])
    assert row.actor == w.queue and row.details["posted_by"] == w.c.pk
    assert row.details["approval"] == {"permission": "billing.approve_contract", "document": ["billing.contract", c.pk]}


def test_r53_a_later_version_counts_with_its_own_approver(bw):
    w = bw
    c = approved(w, start=date(2027, 3, 1))
    clear(w, _invoice(w, c, date(2027, 3, 1)))
    w.clock.set(date(2027, 3, 10))
    v = billing.approve_version(version(w, c, effective=date(2027, 4, 1), amount="11000.00"), user=w.c_b)
    doc = _invoice(w, c, date(2027, 4, 1))
    assert doc.approved_by == w.c_b
    doc = clear(w, doc)
    assert doc.journal_entry.posted_by == w.c_b
    row = AccessLog.objects.get(details__action="post_on_clearance", details__posted_document=[
        "invoicing.salesdocument", doc.pk])
    assert row.details["approval"] == {"permission": "billing.approve_version",
                                       "document": ["billing.contractversion", v.pk]}


def test_r53_no_one_else_is_accepted_as_the_approver(bw):
    w = bw
    c = approved(w, start=date(2027, 3, 1))
    doc = _invoice(w, c, date(2027, 3, 1))
    w.zatca.respond(doc, "pending")
    from invoicing.models import SalesDocument as SD

    stored = SD.objects.get(pk=doc.pk)  # fixture: cleared, not yet posted
    stored.zatca_status = "cleared"
    stored._service_write = True
    stored.save(update_fields=["zatca_status"])
    grant = access.require(w.queue, w.alm, "invoicing.post_on_clearance", document=stored)
    for other in (w.c_b, w.s, w.sys):
        with pytest.raises(access.AccessDenied) as exc, inv._posting_as(other):
            ledger.create_entry(w.alm, stored.issue_date, "x", None, user=w.queue, reference=stored.number,
                                post=True, grant=grant, posted_by=other, document=stored)
        assert exc.value.code == "posted_by_not_approver"
    assert SD.objects.get(pk=doc.pk).journal_entry_id is None


def test_r53_ledger_checks_the_named_approval_itself(bw):
    """The hook only names an approval: the ledger refuses a wrong permission, another approver, another entity's
    record or an approval logged after the recorded time (billing R53)."""
    w = bw
    c = approved(w, start=date(2027, 3, 1))
    doc = _invoice(w, c, date(2027, 3, 1))
    doc_type = documents.get("invoicing.salesdocument")
    good = inv._contract_approval(doc)
    assert good == documents.RecordedApproval("billing.approve_contract", c, w.c.pk, c.versions.get(
        version_no=1).approved_at)
    assert ledger._contract_approval(w.alm, w.c, doc, doc_type) is not None
    import dataclasses

    from ledger.documents import DocumentType

    cases = [
        dataclasses.replace(good, permission="invoicing.approve_bill"),
        dataclasses.replace(good, approver_id=w.c_b.pk),
        dataclasses.replace(good, approved_at=good.approved_at - timedelta(days=1)),
        dataclasses.replace(good, target=w.sub),  # not a record of ALM
    ]
    for ref in cases:
        fake = DocumentType(**{**{f.name: getattr(doc_type, f.name) for f in dataclasses.fields(DocumentType)},
                               "contract_approval": lambda d, ref=ref: ref})
        assert ledger._contract_approval(w.alm, w.c, doc, fake) is None
    assert ledger._contract_approval(w.alm, w.c_b, doc, doc_type) is None


def test_r53_hook_is_only_for_sales_documents():
    from testdocs.models import Bill

    with pytest.raises(documents.DocumentRegistryError):
        with documents._registered_for_tests(Bill, post_permissions={"invoicing.approve_bill"},
                                             build_lines=lambda d: [], contract_approval=lambda d: None):
            pass


def test_r53_human_or_manual_documents_have_no_contract_approval(bw):
    w = bw
    c = approved(w, start=date(2027, 3, 1))
    doc = _invoice(w, c, date(2027, 3, 1))
    note = SalesDocument(source="human", billing_item_id=doc.billing_item_id, doc_type="invoice",
                         approved_by_id=w.c.pk)
    assert inv._contract_approval(note) is None
    note.source, note.doc_type = "system:billing", "credit_note"
    assert inv._contract_approval(note) is None


# --- R73 / AT-67: correction drafts ------------------------------------------------------------------------------

def _release(adj):
    """What billing does before it discards a draft (R46c): the adjustment lets go of the document first."""
    adj.refresh_from_db()
    adj.status, adj.document = "discarded", None
    billing._save(adj)


def _note(w):
    c = approved(w, start=date(2027, 3, 1))
    clear(w, _invoice(w, c, date(2027, 3, 1)))
    w.clock.set(date(2027, 3, 10))
    billing.approve_version(version(w, c, effective=date(2027, 3, 1), amount="9000.00"), user=w.c)
    adj = BillingAdjustment.objects.get(contract=c)
    return c, adj, SalesDocument.objects.get(pk=adj.document_id)


def test_at67_correction_is_a_draft_linked_to_its_adjustment(bw):
    w = bw
    _, adj, note = _note(w)
    assert (note.doc_type, note.status, note.source, note.created_by) == ("credit_note", "draft", "system:billing", w.sys)
    assert note.billing_adjustment_id == adj.pk and note.original_document_id == adj.original_document_id
    assert note.reason_ar == "تعديل القيمة الإيجارية" and note.adjustment_event_date is None
    assert [l.unit_price for l in note.lines.all()] == [D("1000.000")]
    assert inv.create_adjustment_from_billing(adj, w.sys).pk == note.pk  # idempotent


def test_r73_only_the_billing_system_user_creates_correction_drafts(bw):
    w = bw
    _, adj, note = _note(w)
    _release(adj)
    inv.discard_adjustment_draft(note, w.sys)
    for user in (w.s, w.c):
        with pytest.raises(access.AccessDenied):
            inv.create_adjustment_from_billing(adj, user)
    assert not SalesDocument.objects.filter(billing_adjustment=adj).exists()


def test_at67_system_user_cannot_submit_reviewer_completes_cfo_approves(bw):
    w = bw
    _, _, note = _note(w)
    with pytest.raises(access.AccessDenied):
        inv.submit_sales_document(note, user=w.sys)
    with pytest.raises(InvoicingError):  # R18: payment means code still missing
        inv.submit_sales_document(note, user=w.s)
    inv.update_sales_document(note, user=w.s, payment_means_code="30")
    inv.submit_sales_document(note, user=w.s)
    with pytest.raises(access.AccessDenied):
        inv.approve_sales_document(note, user=w.s)
    issued = inv.approve_sales_document(note, user=w.c)
    final = clear(w, issued)
    assert final.status == "final" and final.journal_entry.date == final.issue_date == date(2027, 3, 10)


def test_r56_lines_of_billing_documents_are_read_only_for_everyone(bw):
    w = bw
    _, _, note = _note(w)
    line = note.lines.get()
    for user in (w.s, w.c, w.r):
        with pytest.raises((InvoicingError, access.AccessDenied)):
            inv.add_sales_line(note, user=user, income_type="rent_commercial", quantity="1", unit_price="1.00",
                               description_ar="بند")
        with pytest.raises((InvoicingError, access.AccessDenied)):
            inv.delete_sales_line(line, user=user)
    with pytest.raises(InvoicingError) as exc:
        inv.update_sales_document(note, user=w.s, supply_date=date(2027, 3, 2))
    assert exc.value.code == "billing_header_read_only"
    for attempt in (lambda: SalesDocumentLine.objects.filter(pk=line.pk).update(unit_price=D("1")),
                    lambda: SalesDocumentLine.objects.filter(pk=line.pk).delete(), line.delete,
                    lambda: setattr(line, "unit_price", D("2")) or line.save()):
        with pytest.raises(InvoicingError) as exc:
            attempt()
        assert exc.value.code == "billing_lines_read_only"
    with pytest.raises(InvoicingError) as exc:
        inv.delete_sales_draft(note, user=w.s)
    assert exc.value.code == "billing_document"
    assert note.lines.get().unit_price == D("1000.000")


def test_r56_billing_invoice_lines_are_read_only_too(bw):
    w = bw
    set_auto_issue(w.alm, False)
    c = approved(w, start=date(2027, 3, 1))
    doc = inv.return_to_draft(_invoice(w, c, date(2027, 3, 1)), user=w.c)
    with pytest.raises(InvoicingError) as exc:
        inv.add_sales_line(doc, user=w.s, income_type="rent_commercial", quantity="1", unit_price="1.00",
                           description_ar="بند")
    assert exc.value.code == "billing_lines_read_only"


def test_r46c_discard_deletes_drafts_and_returns_submitted_ones_first(bw):
    w = bw
    _, adj, note = _note(w)
    with pytest.raises(access.AccessDenied):  # only the billing system user
        inv.discard_adjustment_draft(note, w.s)
    inv.update_sales_document(note, user=w.s, payment_means_code="30")
    inv.submit_sales_document(note, user=w.s)
    _release(adj)
    inv.discard_adjustment_draft(note, w.sys)
    assert not SalesDocument.objects.filter(pk=note.pk).exists()
    assert not SalesDocumentLine.objects.filter(document_id=note.pk).exists()


def test_r46c_discard_never_touches_issued_notes_or_other_drafts(bw):
    w = bw
    c, adj, note = _note(w)
    inv.update_sales_document(note, user=w.s, payment_means_code="30")
    inv.submit_sales_document(note, user=w.s)
    issued = inv.approve_sales_document(note, user=w.c)
    with pytest.raises(InvoicingError) as exc:
        inv.discard_adjustment_draft(issued, w.sys)
    assert exc.value.code == "not_draft" and SalesDocument.objects.get(pk=note.pk).status == "issued"
    manual = inv.create_sales_document(w.alm, user=w.s, customer=w.customer, issue_date=date(2027, 3, 10))
    with pytest.raises(InvoicingError) as exc:
        inv.discard_adjustment_draft(manual, w.sys)
    assert exc.value.code == "not_billing_draft"


def test_r73_note_waits_for_a_final_original(bw):
    w = bw
    c = approved(w, start=date(2027, 3, 1))
    doc = _invoice(w, c, date(2027, 3, 1))  # issued, not cleared
    adj = BillingAdjustment(entity=w.alm, contract=c, version=c.current_version, trigger="version",
                            billing_period=period(c, date(2027, 3, 1)), original_document=doc, kind="credit",
                            lines=[], supply_start=date(2027, 3, 1), supply_end=date(2027, 3, 31))
    adj._service_write = True
    adj.save()
    with pytest.raises(InvoicingError) as exc:
        inv.create_adjustment_from_billing(adj, w.sys)
    assert exc.value.code == "original_not_final"
    assert not SalesDocument.objects.filter(billing_adjustment=adj).exists()
