"""Posting hardening (access.md R14, R23, R30, R31; CLAUDE.md rules 1, 3, 4, 5; ledger-fx R23).

Grants tied to one document and used once, segregation including submitter
and last modifier, the posting lock for bulk and raw writes, purification
flags at posting, and the period lock while posting. PostgreSQL-only tests
are skipped on SQLite.
"""
import threading
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.db import DatabaseError, connection, connections, transaction

from core import access
from core.access import services as access_services
from core.access.models import GrantUse
from core.access.testing import make_agent, make_user, member
from core.models import Entity
from ledger import services
from ledger.models import Account, FiscalPeriod, JournalEntry, JournalLine, LedgerError
from ledger.services_setup import create_monthly_periods, install_chart_of_accounts
from testdocs.models import Bill, Receipt, SalesDocument

pytestmark = pytest.mark.django_db
postgres_only = pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL-only database guard")
CASH = [{"account": "1100", "debit": 10}, {"account": "3100", "credit": 10}]
D = Decimal


def setup_entity(code, consolidation="full", parent=None, **extra):
    e = Entity.objects.create(code=code, name_en=code, name_ar=f"كيان {code}", consolidation=consolidation, parent=parent, **extra)
    install_chart_of_accounts(e)
    create_monthly_periods(e, 2027)
    return e


def doc(model, entity, created_by=None):
    """A stored test document (testdocs; invoicing is not built yet), registered in ledger.documents."""
    return model.objects.create(entity=entity, created_by=created_by)


def approve(user, entity, document):
    """A sales-document approval that was given and used (as invoicing would use it on approval)."""
    grant = access.require(user, entity, "invoicing.approve_sales_document", document=document)
    access.use_grant(grant, target=document)
    return grant


@pytest.fixture
def w():
    alm = setup_entity("ALM", "parent")
    sub = setup_entity("SUB", "full", alm)
    clerk, cfo, mp = make_user("clerk"), make_user("cfo"), make_user("mp")
    member(clerk, alm, "accountant")
    member(clerk, sub, "accountant")
    member(cfo, alm, "approver")
    member(mp, alm, "managing_partner")
    return SimpleNamespace(alm=alm, sub=sub, clerk=clerk, cfo=cfo, mp=mp)


def draft(w, lines=CASH, d=date(2027, 1, 10), entity=None, user=None):
    return services.create_entry(entity or w.alm, d, "Manual journal", lines, user=user or w.clerk)


def status(entry):
    return JournalEntry.objects.get(pk=entry.pk).status


def post(w, entry, approver=None):
    return services.post_entry(entry, access.require(approver or w.cfo, entry.entity, "ledger.post_entry", document=entry))


# --- R23 / R30: which grant posts what -------------------------------------------------

def test_accountant_with_post_receipt_grant_cannot_post_own_manual_journal(w):
    """P1: a sub-ledger P grant never lets one accountant book a journal alone."""
    entry = draft(w)
    own_receipt = doc(Receipt, w.alm, created_by=w.clerk)
    for document in (own_receipt, entry):
        grant = access.require(w.clerk, w.alm, "invoicing.post_receipt", document=document)
        with pytest.raises(access.AccessDenied) as exc:  # not through post_entry
            services.post_entry(entry, grant)
        assert exc.value.code == "grant_wrong_permission"
    before = JournalEntry.objects.count()
    grant = access.require(w.clerk, w.alm, "invoicing.post_receipt", document=own_receipt)
    with pytest.raises(access.AccessDenied) as exc:  # nor for a receipt the accountant made (segregation, R14)
        services.create_entry(w.alm, date(2027, 1, 10), "Receipt", CASH, user=w.clerk, post=True, grant=grant,
                              document=own_receipt)
    assert exc.value.code == "self_approval"
    grant = access.require(w.clerk, w.alm, "invoicing.post_receipt", document=entry)
    with pytest.raises(access.AccessDenied) as exc:  # nor with the manual draft itself as "document"
        services.create_entry(w.alm, date(2027, 1, 10), "Receipt", CASH, user=w.clerk, post=True, grant=grant, document=entry)
    assert exc.value.code == "document_not_registered"
    assert JournalEntry.objects.count() == before and status(entry) == "draft"
    # A receipt someone else recorded is posted by the accountant, once.
    receipt = doc(Receipt, w.alm, created_by=w.cfo)
    grant = access.require(w.clerk, w.alm, "invoicing.post_receipt", document=receipt)
    booked = services.create_entry(w.alm, date(2027, 1, 10), "Receipt 3", CASH, user=w.clerk, post=True, grant=grant,
                                   document=receipt)
    assert booked.status == "posted" and booked.document_ref == ("testdocs.receipt", receipt.pk) and booked.posted_by == w.clerk


def test_p1_tuple_documents_never_post(w):
    """P1 (reviewer probe): a grant bound to a made-up (type, id) tuple posted any journal, repeatedly."""
    with pytest.raises(access.AccessUsageError):
        access.require(w.clerk, w.alm, "invoicing.post_receipt", document=("invoicing.receipt", 1))
    receipt = doc(Receipt, w.alm, created_by=w.cfo)
    grant = access.require(w.clerk, w.alm, "invoicing.post_receipt", document=receipt)
    with pytest.raises((access.AccessDenied, access.AccessUsageError)):
        services.create_entry(w.alm, date(2027, 1, 5), "anything", CASH, user=w.clerk, post=True, grant=grant,
                              document=("testdocs.receipt", receipt.pk))
    stand_in = SimpleNamespace(pk=receipt.pk, _meta=receipt._meta, created_by_id=None, submitted_by_id=None, entity_id=w.alm.pk)
    with pytest.raises(access.AccessDenied) as exc:  # a look-alike object is not the document either
        services.create_entry(w.alm, date(2027, 1, 5), "anything", CASH, user=w.clerk, post=True, grant=grant,
                              document=stand_in)
    assert exc.value.code == "grant_needs_document"
    assert not JournalEntry.objects.exists()
    assert not GrantUse.objects.filter(log_id=grant.log_id).exists()


def test_p1_reversal_grant_never_posts_a_new_entry(w):
    """P1: invoicing.reverse_document used to post new entries through create_entry(post=True)."""
    from core.access import catalogue

    assert "invoicing.reverse_document" not in catalogue.DOCUMENT_POST_PERMISSIONS
    bill = doc(Bill, w.alm, created_by=w.cfo)
    grant = access.require(w.clerk, w.alm, "invoicing.reverse_document", document=bill)
    with pytest.raises(access.AccessDenied) as exc:
        services.create_entry(w.alm, date(2027, 1, 5), "via reverse grant", CASH, user=w.clerk, post=True, grant=grant,
                              document=bill)
    assert exc.value.code == "grant_wrong_permission" and not JournalEntry.objects.exists()


def test_unregistered_missing_and_other_entity_documents_refused(w):
    from testdocs.models import Unregistered

    before = JournalEntry.objects.count()
    unregistered = doc(Unregistered, w.alm, created_by=w.cfo)
    gone = doc(Receipt, w.alm, created_by=w.cfo)
    other = doc(Receipt, w.sub, created_by=w.cfo)
    for document, code in ((unregistered, "document_not_registered"), (gone, "document_not_found"),
                           (other, "document_other_entity")):
        grant = access.require(w.clerk, w.alm, "invoicing.post_receipt", document=document)
        if document is gone:
            Receipt.objects.filter(pk=gone.pk).delete()
        with pytest.raises(access.AccessDenied) as exc:
            services.create_entry(w.alm, date(2027, 1, 5), "x", CASH, user=w.clerk, post=True, grant=grant, document=document)
        assert exc.value.code == code
    assert JournalEntry.objects.count() == before


def test_document_permission_must_be_registered_for_its_type(w):
    bill = doc(Bill, w.alm, created_by=w.clerk)
    grant = access.require(w.clerk, w.alm, "invoicing.post_receipt", document=bill)  # receipts' right, on a bill
    with pytest.raises(access.AccessDenied) as exc:
        services.create_entry(w.alm, date(2027, 1, 5), "x", CASH, user=w.cfo, post=True, grant=grant, document=bill)
    assert exc.value.code == "grant_wrong_permission" and not JournalEntry.objects.exists()


def test_segregation_uses_the_stored_document(w):
    """The stored creator / submitter / last modifier counts, not attributes of the object passed in."""
    receipt = Receipt.objects.create(entity=w.alm, created_by=w.cfo, submitted_by=w.clerk)
    receipt.submitted_by = None  # in memory only
    grant = access.require(w.clerk, w.alm, "invoicing.post_receipt", document=receipt)
    with pytest.raises(access.AccessDenied) as exc:
        services.create_entry(w.alm, date(2027, 1, 5), "x", CASH, user=w.clerk, post=True, grant=grant, document=receipt)
    assert exc.value.code == "self_approval"
    modified = Receipt.objects.create(entity=w.alm, created_by=w.cfo, modified_by=w.clerk)
    grant = access.require(w.clerk, w.alm, "invoicing.post_receipt", document=modified)
    with pytest.raises(access.AccessDenied):
        services.create_entry(w.alm, date(2027, 1, 5), "x", CASH, user=w.clerk, post=True, grant=grant, document=modified)
    assert not JournalEntry.objects.exists()


def test_a_document_books_once_also_with_a_second_grant(w):
    bill = doc(Bill, w.alm, created_by=w.clerk)
    first = services.create_entry(w.alm, date(2027, 1, 10), "Bill", CASH, user=w.clerk, post=True, document=bill,
                                  grant=access.require(w.cfo, w.alm, "invoicing.approve_bill", document=bill))
    second = access.require(w.mp, w.alm, "invoicing.approve_bill", document=bill)
    with pytest.raises(LedgerError, match="already booked"):
        services.create_entry(w.alm, date(2027, 1, 10), "Bill again", CASH, user=w.clerk, post=True, grant=second,
                              document=bill)
    assert not GrantUse.objects.filter(log_id=second.log_id).exists()
    services.reverse_entry(first, access.require(w.clerk, w.alm, "ledger.reverse_entry", document=first))
    with pytest.raises(LedgerError, match="already booked"):  # also after its reversal: correct by a new document
        services.create_entry(w.alm, date(2027, 1, 10), "Bill again", CASH, user=w.clerk, post=True, grant=second,
                              document=bill)
    assert JournalEntry.objects.filter(document_type="testdocs.bill", reversal_of__isnull=True).count() == 1


def test_database_refuses_a_second_entry_for_one_document(w):
    from django.db import IntegrityError

    bill = doc(Bill, w.alm, created_by=w.clerk)
    services.create_entry(w.alm, date(2027, 1, 10), "Bill", CASH, user=w.clerk, post=True, document=bill,
                          grant=access.require(w.cfo, w.alm, "invoicing.approve_bill", document=bill))
    with pytest.raises(IntegrityError), transaction.atomic():
        JournalEntry.objects.create(entity=w.alm, date=date(2027, 1, 10), description="dup", document_type="testdocs.bill",
                                    document_id=bill.pk)


def test_documents_without_post_are_refused(w):
    bill = doc(Bill, w.alm, created_by=w.clerk)
    with pytest.raises(LedgerError):  # a draft never claims a document (it would block the real booking)
        services.create_entry(w.alm, date(2027, 1, 10), "x", CASH, user=w.clerk, document=bill)
    assert not JournalEntry.objects.exists()


def test_document_registry_validates_registrations():
    from ledger import documents
    from ledger.models import JournalEntry as Entry
    from testdocs.models import Unregistered

    with pytest.raises(documents.DocumentRegistryError):
        documents.register(Entry, post_permissions={"invoicing.approve_bill"})
    for perms in (set(), {"ledger.post_entry"}, {"invoicing.reverse_document"}, {"invoicing.view"}):
        with pytest.raises(documents.DocumentRegistryError):
            documents.register(Unregistered, post_permissions=perms)
    with pytest.raises(documents.DocumentRegistryError):
        documents.register(Unregistered, post_permissions={"invoicing.post_receipt"}, reverse_permissions={"ledger.reverse_entry"})
    with pytest.raises(documents.DocumentRegistryError):  # another registration for a registered type
        documents.register(Bill, post_permissions={"invoicing.post_receipt"})
    assert documents.get("testdocs.unregistered") is None
    assert all(label.startswith("testdocs.") for label in documents.registered())  # nothing else registers yet
    try:
        documents.register(Unregistered, post_permissions={"invoicing.post_receipt"})
        assert documents.get("testdocs.unregistered").post_permissions == {"invoicing.post_receipt"}
    finally:
        documents.unregister(Unregistered)


def test_approve_bill_grant_for_bill_7_cannot_post_an_unrelated_draft(w):
    bill7 = doc(Bill, w.alm, created_by=w.clerk)
    grant = access.require(w.cfo, w.alm, "invoicing.approve_bill", document=bill7)
    unrelated = draft(w)
    with pytest.raises(access.AccessDenied) as exc:
        services.post_entry(unrelated, grant)
    assert exc.value.code == "grant_wrong_permission" and status(unrelated) == "draft"
    before = JournalEntry.objects.count()
    for other, code in ((doc(Bill, w.alm, created_by=w.clerk), "grant_wrong_document"),
                        (doc(Receipt, w.alm), "grant_wrong_permission"),
                        (unrelated, "document_not_registered")):
        with pytest.raises(access.AccessDenied) as exc:
            services.create_entry(w.alm, date(2027, 1, 10), "Bill", CASH, user=w.clerk, post=True, grant=grant, document=other)
        assert exc.value.code == code
    assert JournalEntry.objects.count() == before
    assert not GrantUse.objects.filter(log_id=grant.log_id).exists()


def test_grant_used_twice_refused(w):
    entry = draft(w)
    grant = access.require(w.cfo, w.alm, "ledger.post_entry", document=entry)
    services.post_entry(entry, grant)
    use = GrantUse.objects.get(log_id=grant.log_id)
    assert (use.used_by, use.entity, use.target_type, use.target_id) == (w.cfo, w.alm, "ledger.journalentry", entry.pk)
    with pytest.raises(access.AccessDenied) as exc:
        services.post_entry(entry, grant)
    assert exc.value.code == "grant_used"

    bill7 = doc(Bill, w.alm, created_by=w.clerk)
    bill_grant = access.require(w.cfo, w.alm, "invoicing.approve_bill", document=bill7)
    services.create_entry(w.alm, date(2027, 1, 10), "Bill 7", CASH, user=w.clerk, post=True, grant=bill_grant, document=bill7)
    before = JournalEntry.objects.count()
    with pytest.raises(access.AccessDenied) as exc:  # the same approval does not book the bill twice
        services.create_entry(w.alm, date(2027, 1, 10), "Bill 7 again", CASH, user=w.clerk, post=True, grant=bill_grant,
                              document=bill7)
    assert exc.value.code == "grant_used" and JournalEntry.objects.count() == before


def test_reversal_grant_used_once(w):
    posted = post(w, draft(w))
    grant = access.require(w.clerk, w.alm, "ledger.reverse_entry", document=posted)
    services.reverse_entry(posted, grant)
    with pytest.raises(access.AccessDenied) as exc:
        services.reverse_entry(posted, grant)
    assert exc.value.code == "grant_used"


def test_reverse_document_grant_reverses_only_its_documents_entry(w):
    bill7 = doc(Bill, w.alm, created_by=w.clerk)
    booked = services.create_entry(
        w.alm, date(2027, 1, 10), "Bill 7", CASH, user=w.clerk, post=True, document=bill7,
        grant=access.require(w.cfo, w.alm, "invoicing.approve_bill", document=bill7),
    )
    manual = post(w, draft(w))
    grant = access.require(w.clerk, w.alm, "invoicing.reverse_document", document=bill7)
    with pytest.raises(access.AccessDenied) as exc:
        services.reverse_entry(manual, grant)
    assert exc.value.code == "grant_wrong_document" and status(manual) == "posted"
    rev = services.reverse_entry(booked, grant)
    assert status(booked) == "reversed" and rev.document_ref == ("testdocs.bill", bill7.pk)


def test_reverse_document_grant_needs_a_registered_reversal_right(w):
    from ledger import documents

    receipt = doc(Receipt, w.alm, created_by=w.cfo)
    booked = services.create_entry(w.alm, date(2027, 1, 10), "Receipt", CASH, user=w.clerk, post=True, document=receipt,
                                   grant=access.require(w.clerk, w.alm, "invoicing.post_receipt", document=receipt))
    original = documents.get("testdocs.receipt")
    documents.unregister(Receipt)
    try:
        documents.register(Receipt, post_permissions={"invoicing.post_receipt"})  # no reversal right registered
        with pytest.raises(access.AccessDenied) as exc:
            services.reverse_entry(booked, access.require(w.clerk, w.alm, "invoicing.reverse_document", document=receipt))
        assert exc.value.code == "grant_wrong_permission"
        documents.unregister(Receipt)
        with pytest.raises(access.AccessDenied) as exc:
            services.reverse_entry(booked, access.require(w.clerk, w.alm, "invoicing.reverse_document", document=receipt))
        assert exc.value.code == "document_not_registered"
    finally:
        documents.unregister(Receipt)
        documents.register(Receipt, post_permissions=original.post_permissions,
                           reverse_permissions=original.reverse_permissions)
    assert status(booked) == "posted"


def test_zatca_queue_grant_cannot_post_a_manual_journal(w):
    queue = access.system_user("zatca_queue")
    member(queue, w.alm, "system_zatca_queue")
    invoice = doc(SalesDocument, w.alm, created_by=w.clerk)
    approve(w.cfo, w.alm, invoice)
    entry = draft(w)
    for grant in (
        access.require(queue, w.alm, "invoicing.post_on_clearance", document=invoice),
        access.require(queue, w.alm, "invoicing.post_on_clearance", document=entry),
        access.require(queue, w.alm, "zatca.process_queue"),
    ):
        with pytest.raises(access.AccessDenied) as exc:
            services.post_entry(entry, grant, posted_by=w.cfo)
        assert exc.value.code == "grant_wrong_permission"
    with pytest.raises(access.AccessDenied):  # the queue holds no ledger.post_entry at all
        access.require(queue, w.alm, "ledger.post_entry", document=entry)
    assert status(entry) == "draft"


def test_clearance_posted_by_must_be_a_recorded_human_approver(w):
    queue = access.system_user("zatca_queue")
    member(queue, w.alm, "system_zatca_queue")
    invoice = doc(SalesDocument, w.alm, created_by=w.clerk)
    other_invoice = doc(SalesDocument, w.alm, created_by=w.clerk)
    approve(w.cfo, w.alm, other_invoice)  # approved another one
    grant = access.require(queue, w.alm, "invoicing.post_on_clearance", document=invoice)

    def clear(posted_by):
        return services.create_entry(w.alm, date(2027, 1, 10), "Invoice 11", CASH, user=queue, post=True, grant=grant,
                                     document=invoice, posted_by=posted_by)

    for candidate in (w.cfo, queue, make_agent()):  # not the approver of this invoice / not a human
        with pytest.raises(access.AccessDenied) as exc:
            clear(candidate)
        assert exc.value.code == "posted_by_not_approver"
    access.require(w.cfo, w.alm, "invoicing.approve_sales_document", document=invoice)  # issued, never used
    with pytest.raises(access.AccessDenied) as exc:
        clear(w.cfo)
    assert exc.value.code == "posted_by_not_approver"
    approve(w.cfo, w.alm, invoice)
    access_services.set_user_active(w.mp, w.cfo, False, "Left the company")
    posted = clear(w.cfo)  # the right was checked when the CFO approved (owner default)
    assert posted.posted_by == w.cfo
    assert not JournalEntry.objects.filter(description="Invoice 11").exclude(posted_by=w.cfo).exists()
    from core.access.models import AccessLog

    row = AccessLog.objects.get(details__action="post_on_clearance")
    assert (row.actor, row.permission, row.details["posted_by"], row.details["entry"]) == (
        queue, "invoicing.post_on_clearance", w.cfo.pk, posted.pk)


def test_clearance_approval_given_before_the_last_change_does_not_count(w):
    queue = access.system_user("zatca_queue")
    member(queue, w.alm, "system_zatca_queue")
    invoice = doc(SalesDocument, w.alm, created_by=w.clerk)
    approve(w.cfo, w.alm, invoice)
    SalesDocument.objects.filter(pk=invoice.pk).update(modified_at=access_services.now() + timedelta(seconds=1))
    grant = access.require(queue, w.alm, "invoicing.post_on_clearance", document=invoice)
    with pytest.raises(access.AccessDenied) as exc:
        services.create_entry(w.alm, date(2027, 1, 10), "Invoice", CASH, user=queue, post=True, grant=grant,
                              document=invoice, posted_by=w.cfo)
    assert exc.value.code == "posted_by_not_approver" and not JournalEntry.objects.exists()


def test_create_entry_post_true_needs_grant_user_or_create_right(w):  # item 5
    bill7 = doc(Bill, w.alm, created_by=w.clerk)
    grant = access.require(w.cfo, w.alm, "invoicing.approve_bill", document=bill7)
    outsider = make_user("outsider")
    member(outsider, w.sub, "accountant")
    with pytest.raises(access.AccessDenied) as exc:
        services.create_entry(w.alm, date(2027, 1, 10), "Bill 7", CASH, user=outsider, post=True, grant=grant, document=bill7)
    assert exc.value.code == "no_membership"
    assert not GrantUse.objects.filter(log_id=grant.log_id).exists()
    e = services.create_entry(w.alm, date(2027, 1, 10), "Bill 7", CASH, user=w.cfo, post=True, grant=grant, document=bill7)
    assert e.created_by == e.posted_by == w.cfo  # the grant's own user needs no ledger.create_entry


# --- R31: forged, altered and stale grants ------------------------------------------------

def test_at30_altered_grant_refused(w):
    entry, other = draft(w), draft(w)
    grant = access.require(w.cfo, w.alm, "ledger.post_entry", document=entry)
    other_grant = access.require(w.cfo, w.alm, "ledger.post_entry", document=other)
    mp_grant = access.require(w.mp, w.alm, "ledger.post_entry", document=entry)
    for altered in (
        replace(grant, log_id=other_grant.log_id),  # another real row
        replace(grant, log_id=None),
        replace(grant, log_id=grant.log_id + 10_000),
        replace(other_grant, document_ref=grant.document_ref),  # re-pointed at this draft
        replace(grant, user=w.mp),  # MP may post, but did not get this grant
        replace(mp_grant, log_id=grant.log_id),
        replace(grant, checked_at=grant.checked_at + timedelta(seconds=1)),
        replace(grant, membership_ids=()),
    ):
        with pytest.raises(access.AccessDenied) as exc:
            services.post_entry(entry, altered)
        assert exc.value.code == "grant_forged", altered
    assert status(entry) == "draft"
    assert services.post_entry(entry, grant).posted_by == w.cfo


def test_grant_expires_after_configured_age(w, monkeypatch, settings):
    entry = draft(w)
    grant = access.require(w.cfo, w.alm, "ledger.post_entry", document=entry)
    real_now = access_services.now
    monkeypatch.setattr(access_services, "now", lambda: real_now() + timedelta(minutes=16))
    with pytest.raises(access.AccessDenied) as exc:
        services.post_entry(entry, grant)
    assert exc.value.code == "grant_expired"
    settings.ACCESS_GRANT_MAX_AGE = timedelta(hours=1)
    assert services.post_entry(entry, grant).status == "posted"


def test_grant_refused_by_a_ledger_rule_stays_unused(w):
    entry = draft(w, [{"account": "1100", "debit": 10}, {"account": "3100", "credit": 9}])
    grant = access.require(w.cfo, w.alm, "ledger.post_entry", document=entry)
    with pytest.raises(LedgerError, match="does not balance"):
        services.post_entry(entry, grant)
    assert not GrantUse.objects.filter(log_id=grant.log_id).exists()
    # Corrected by someone else after the approval: the approval no longer covers the draft.
    services.update_draft(entry, user=w.clerk, lines=CASH)
    with pytest.raises(access.AccessDenied) as exc:
        services.post_entry(entry, grant)
    assert exc.value.code == "grant_document_changed"
    assert post(w, entry).status == "posted"


# --- R14: submitter and last modifier cannot approve ---------------------------------------

def test_submitter_and_last_modifier_cannot_post(w):
    member(w.cfo, w.alm, "accountant")  # the CFO may also draft
    entry = draft(w)
    services.update_draft(entry, user=w.cfo, description="Edited by the CFO")
    assert JournalEntry.objects.get(pk=entry.pk).modified_by == w.cfo
    with pytest.raises(access.AccessDenied) as exc:
        access.require(w.cfo, w.alm, "ledger.post_entry", document=JournalEntry.objects.get(pk=entry.pk))
    assert exc.value.code == "self_approval"

    second = draft(w)
    grant = access.require(w.cfo, w.alm, "ledger.post_entry", document=second)  # before submitting it
    services.submit_entry(second, user=w.cfo)
    with pytest.raises(access.AccessDenied) as exc:
        services.post_entry(second, grant)
    assert exc.value.code == "self_approval" and status(second) == "draft"
    assert post(w, second, approver=w.mp).posted_by == w.mp


def test_rerate_makes_the_user_last_modifier(w):
    member(w.cfo, w.alm, "accountant")
    entry = draft(w)
    services.rerate_entry(entry, user=w.cfo)
    with pytest.raises(access.AccessDenied):
        access.require(w.cfo, w.alm, "ledger.post_entry", document=JournalEntry.objects.get(pk=entry.pk))


def test_update_draft_refuses_posted_entries_other_entities_and_outsiders(w):
    posted = post(w, draft(w))
    with pytest.raises(LedgerError):
        services.update_draft(posted, user=w.clerk, description="x")
    entry = draft(w)
    viewer = make_user("viewer")
    member(viewer, w.alm, "viewer")
    with pytest.raises(access.AccessDenied):
        services.update_draft(entry, user=viewer, description="x")
    sub_cash = Account.objects.get(entity=w.sub, code="1100")
    with pytest.raises(LedgerError, match="another entity"):
        services.update_draft(entry, user=w.clerk, lines=[{"account": sub_cash, "debit": 10}, {"account": "3100", "credit": 10}])
    assert JournalEntry.objects.get(pk=entry.pk).description == "Manual journal"


# --- rule 1: lines and bulk writes ---------------------------------------------------------

def test_line_cannot_move_off_or_onto_a_posted_entry(w):
    posted = post(w, draft(w))
    open_draft = draft(w)
    booked_line = posted.lines.first()
    booked_line.entry = open_draft
    with pytest.raises(LedgerError):
        booked_line.save()
    draft_line = open_draft.lines.first()
    draft_line.entry = posted
    with pytest.raises(LedgerError):
        draft_line.save()
    booked_line = JournalLine.objects.get(pk=booked_line.pk)
    booked_line.description = "edited"
    with pytest.raises(LedgerError):
        booked_line.save()
    booked_line.entry_id = open_draft.pk  # in memory only: delete still checks the stored entry
    with pytest.raises(LedgerError):
        booked_line.delete()
    assert JournalLine.objects.filter(entry=posted).count() == 2 and JournalLine.objects.filter(entry=open_draft).count() == 2


def test_bulk_update_and_delete_refused_for_posted_rows(w):
    posted = post(w, draft(w))
    open_draft = draft(w)
    entries = JournalEntry.objects.filter(pk=posted.pk)
    lines = JournalLine.objects.filter(entry=posted)
    for attempt in (
        lambda: entries.update(description="x"),
        lambda: entries.delete(),
        lambda: JournalEntry.objects.all().delete(),
        lambda: JournalEntry.objects.filter(pk=open_draft.pk).update(status="posted"),  # rule 3
        lambda: lines.update(debit=D("11")),
        lambda: lines.delete(),
        lambda: JournalLine.objects.filter(entry=open_draft).update(entry=posted),
        lambda: JournalLine.objects.filter(entry=open_draft).update(entry_id=posted.pk),
    ):
        with pytest.raises(LedgerError):
            attempt()
    assert status(posted) == "posted" and status(open_draft) == "draft"
    assert sorted(JournalLine.objects.filter(entry=posted).values_list("debit", flat=True)) == [D("0"), D("10")]
    # Drafts stay editable in bulk.
    assert JournalEntry.objects.filter(pk=open_draft.pk).update(description="bulk") == 1
    assert JournalLine.objects.filter(entry=open_draft).update(description="bulk") == 2


def _raw(sql, params):
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql, params)


@postgres_only
def test_database_rejects_raw_changes_to_posted_entries_and_lines(w):
    posted = post(w, draft(w))
    open_draft = draft(w)
    line = posted.lines.first()
    for sql, params in (
        ("UPDATE ledger_journalentry SET description = 'x' WHERE id = %s", [posted.pk]),
        ("UPDATE ledger_journalentry SET status = 'draft' WHERE id = %s", [posted.pk]),
        ("UPDATE ledger_journalentry SET status = 'reversed', description = 'x' WHERE id = %s", [posted.pk]),
        ("DELETE FROM ledger_journalentry WHERE id = %s", [posted.pk]),
        ("UPDATE ledger_journalline SET debit = debit + 1 WHERE id = %s", [line.pk]),
        ("UPDATE ledger_journalline SET entry_id = %s WHERE id = %s", [open_draft.pk, line.pk]),
        ("UPDATE ledger_journalline SET entry_id = %s WHERE entry_id = %s", [posted.pk, open_draft.pk]),
        ("DELETE FROM ledger_journalline WHERE id = %s", [line.pk]),
        (
            "INSERT INTO ledger_journalline (entry_id, account_id, description, debit, credit, currency_id, txn_amount, "
            "fx_rate, rate_source, manual_rate_reason, line_kind, requires_purification) "
            "SELECT entry_id, account_id, '', debit, credit, currency_id, txn_amount, fx_rate, rate_source, '', line_kind, false "
            "FROM ledger_journalline WHERE id = %s",
            [line.pk],
        ),
    ):
        with pytest.raises(DatabaseError):
            _raw(sql, params)
    # posted -> reversed alone is not enough: the posted reversal must exist (what reverse_entry writes).
    with pytest.raises(DatabaseError):
        _raw("UPDATE ledger_journalentry SET status = 'reversed' WHERE id = %s", [posted.pk])
    services.reverse_entry(posted, access.require(w.clerk, w.alm, "ledger.reverse_entry", document=posted))
    with pytest.raises(DatabaseError):
        _raw("UPDATE ledger_journalentry SET status = 'posted' WHERE id = %s", [posted.pk])
    _raw("UPDATE ledger_journalline SET description = 'ok' WHERE entry_id = %s", [open_draft.pk])  # drafts are free


# --- rule 3: status changes only through ledger.services (P2) ------------------------------

def test_p2_draft_cannot_be_posted_or_reversed_by_save(w):
    """P2 (reviewer probe): draft.status = "posted"; save() booked an unnumbered entry; "reversed" too."""
    for target in ("posted", "reversed"):
        entry = draft(w)
        entry.status = target
        with pytest.raises(LedgerError, match="only through ledger.services"):
            entry.save()
        assert status(entry) == "draft"
    entry = draft(w)
    entry.status, entry.number, entry.posted_by, entry.posted_at = "posted", "ALM-JE-2027-999999", w.cfo, access_services.now()
    entry.period = FiscalPeriod.objects.get(entity=w.alm, name="2027-01")
    with pytest.raises(LedgerError):  # all the fields of a posting, still not through the service
        entry.save()
    assert status(entry) == "draft"
    assert services._trial_balance(w.alm, date(2027, 12, 31)) == []


def test_entries_are_created_as_drafts_only(w):
    with pytest.raises(LedgerError):
        JournalEntry.objects.create(entity=w.alm, date=date(2027, 1, 10), description="x", status="posted")
    with pytest.raises(LedgerError):
        JournalEntry.objects.bulk_create([JournalEntry(entity=w.alm, date=date(2027, 1, 10), description="x", status="reversed")])
    assert not JournalEntry.objects.exists()


def test_posted_entry_marked_reversed_only_by_reverse_entry(w):
    posted = post(w, draft(w))
    posted.status = "reversed"
    with pytest.raises(LedgerError):
        posted.save(update_fields=["status"])
    posted._allow_transition("draft", "posted")  # a flag for another transition does not help
    with pytest.raises(LedgerError):
        posted.save(update_fields=["status"])
    assert status(posted) == "posted"
    services.reverse_entry(posted, access.require(w.clerk, w.alm, "ledger.reverse_entry", document=posted))
    assert status(posted) == "reversed"


@postgres_only
def test_p2_database_rejects_status_changes_outside_the_services(w):
    entry = draft(w)
    unbalanced = draft(w, [{"account": "1100", "debit": 10}, {"account": "3100", "credit": 9}])
    jan = FiscalPeriod.objects.get(entity=w.alm, name="2027-01")
    for sql, params in (
        ("UPDATE ledger_journalentry SET status = 'posted' WHERE id = %s", [entry.pk]),
        ("UPDATE ledger_journalentry SET status = 'reversed' WHERE id = %s", [entry.pk]),
        ("UPDATE ledger_journalentry SET status = 'posted', number = 'X-1', period_id = %s, posted_at = now() "
         "WHERE id = %s", [jan.pk, entry.pk]),  # no posted_by
        ("UPDATE ledger_journalentry SET status = 'posted', number = 'X-2', period_id = %s, posted_at = now(), "
         "posted_by_id = %s WHERE id = %s", [jan.pk, w.cfo.pk, unbalanced.pk]),  # does not balance
        ("INSERT INTO ledger_journalentry (entity_id, number, date, description, reference, status, created_at, source, "
         "document_type) VALUES (%s, 'X-3', '2027-01-10', 'x', '', 'posted', now(), 'human', '')", [w.alm.pk]),
    ):
        with pytest.raises(DatabaseError):
            _raw(sql, params)
    assert status(entry) == status(unbalanced) == "draft"
    assert post(w, entry).status == "posted"  # the service path passes the trigger


@postgres_only
@pytest.mark.django_db(transaction=True)
def test_line_write_waits_for_a_posting_in_flight():
    """Item 8: a draft-line edit racing a posting waits for it (FOR SHARE) and is then refused."""
    from core.access.models import Role, RolePermission
    from core.access.seed import apply_seed
    from core.currency_seed import apply_currency_seed
    from core.models import Currency

    apply_seed(Role, RolePermission)
    apply_currency_seed(Currency)
    alm = setup_entity("ALM", "parent")
    clerk = make_user("clerk")
    member(clerk, alm, "accountant")
    entry = services.create_entry(alm, date(2027, 1, 10), "x", CASH, user=clerk)
    line = entry.lines.first()
    result = {}

    def editor():
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("UPDATE ledger_journalline SET description = 'late edit' WHERE id = %s", [line.pk])
            result["outcome"] = "edited"
        except DatabaseError as exc:
            result["outcome"] = str(exc)
        finally:
            connections.close_all()

    with transaction.atomic():
        JournalEntry.objects.select_for_update().get(pk=entry.pk)  # as _post holds it
        thread = threading.Thread(target=editor)
        thread.start()
        thread.join(timeout=1.5)
        assert thread.is_alive(), "the line write should wait for the entry lock"
        with connection.cursor() as cursor:  # stands in for the posting's own write (all the fields _post sets)
            cursor.execute(
                "UPDATE ledger_journalentry SET status = 'posted', number = 'ALM-JE-2027-000001', period_id = %s, "
                "posted_at = now(), posted_by_id = %s WHERE id = %s",
                [FiscalPeriod.objects.get(entity=alm, name="2027-01").pk, clerk.pk, entry.pk],
            )
    thread.join(timeout=10)
    assert "posted entry" in result["outcome"]
    assert JournalLine.objects.get(pk=line.pk).description == ""


@postgres_only
def test_database_allows_the_service_flows(w):
    """Posting, reversing, revaluation-style drafts and draft deletion all pass the triggers."""
    entry = post(w, draft(w))
    services.reverse_entry(entry, access.require(w.clerk, w.alm, "ledger.reverse_entry", document=entry))
    gone = draft(w)
    JournalEntry.objects.filter(pk=gone.pk).delete()
    assert not JournalEntry.objects.filter(pk=gone.pk).exists()


# --- rule 5: purification flags -------------------------------------------------------------

def test_purification_flag_set_at_posting(w):
    lines = [{"account": "1100", "debit": 1200}, {"account": "4900", "credit": 1000}, {"account": "4110", "credit": 200}]
    entry = draft(w, lines)
    assert not JournalLine.objects.filter(entry=entry, requires_purification=True).exists()  # drafts are not flagged
    posted = post(w, entry)
    flagged = set(JournalLine.objects.filter(entry=posted, requires_purification=True).values_list("account__code", flat=True))
    assert flagged == {"4900"}
    # The purification transfer itself (Dr 4900 / Cr 2600, invoicing E14) is not income to purify.
    transfer = post(w, draft(w, [{"account": "4900", "debit": 1000}, {"account": "2600", "credit": 1000}]))
    assert not JournalLine.objects.filter(entry=transfer, requires_purification=True).exists()
    assert services.purification_amount(w.alm, user=w.clerk) == D("1000")
    # A reversal flags the mirror of the flagged line, a debit, so the income nets to zero; the original keeps its flag.
    rev = services.reverse_entry(posted, access.require(w.clerk, w.alm, "ledger.reverse_entry", document=posted))
    mirrored = JournalLine.objects.get(entry=rev, requires_purification=True)
    assert (mirrored.account.code, mirrored.debit) == ("4900", D("1000.00"))
    assert JournalLine.objects.filter(entry=posted, requires_purification=True).count() == 1
    assert services.purification_amount(w.alm, user=w.clerk) == D("0")


def test_p7_reversing_a_purification_transfer_creates_no_new_income(w):
    """P7 (reviewer probe): the reversal of Dr 4900 / Cr 2600 was a Cr 4900 line flagged as new income."""
    post(w, draft(w, [{"account": "1100", "debit": 100}, {"account": "4900", "credit": 100}]))
    transfer = post(w, draft(w, [{"account": "4900", "debit": 100}, {"account": "2600", "credit": 100}]))
    rev = services.reverse_entry(transfer, access.require(w.clerk, w.alm, "ledger.reverse_entry", document=transfer))
    assert not JournalLine.objects.filter(entry=rev, requires_purification=True).exists()
    assert services.purification_amount(w.alm, user=w.clerk) == D("100")
    assert services.purification_amount(w.alm, user=w.clerk, as_of=date(2026, 12, 31)) == D("0")
    viewer = make_user("outsider")
    member(viewer, w.sub, "viewer")
    with pytest.raises(access.AccessDenied):
        services.purification_amount(w.alm, user=viewer)


def test_purification_flags_are_set_only_at_posting(w):
    entry = draft(w, [{"account": "1100", "debit": 10}, {"account": "3100", "credit": 10}])
    JournalLine.objects.filter(entry=entry).update(requires_purification=True)  # a draft line flagged by hand
    posted = post(w, entry)
    assert not JournalLine.objects.filter(entry=posted, requires_purification=True).exists()
    assert services.signed_purification([
        JournalLine(debit=D("0"), credit=D("5"), requires_purification=True),
        JournalLine(debit=D("2"), credit=D("0"), requires_purification=True),
        JournalLine(debit=D("0"), credit=D("9"), requires_purification=False),
    ]) == D("3")


def test_prohibited_income_flagged_where_it_can_post(w):
    conv = setup_entity("CONV", "none", w.alm, sharia_enforced=False)
    member(w.clerk, conv, "accountant")
    member(w.cfo, conv, "approver")
    Account.objects.create(entity=conv, code="4950", name_en="Interest income", name_ar="إيرادات فوائد", type="income",
                           sharia_prohibited=True)
    entry = draft(w, [{"account": "1100", "debit": 50}, {"account": "4950", "credit": 50}], entity=conv)
    posted = post(w, entry)
    assert list(JournalLine.objects.filter(entry=posted, requires_purification=True).values_list("account__code", flat=True)) == ["4950"]
    # In a Sharia-enforcing entity the same account is blocked (rule 5), so nothing is flagged there.
    Account.objects.create(entity=w.alm, code="4950", name_en="Interest income", name_ar="إيرادات فوائد", type="income",
                           sharia_prohibited=True)
    blocked = draft(w, [{"account": "1100", "debit": 50}, {"account": "4950", "credit": 50}])
    with pytest.raises(LedgerError, match="Sharia-prohibited"):
        post(w, blocked)


# --- gateway rule 4 / item 19: source --------------------------------------------------------

def test_entry_source_follows_the_user(w):
    agent = make_agent("drafter")
    from core.access.models import Membership, Role, RolePermission

    role = Role.objects.create(code="agent_drafter", name_en="Agent drafter", name_ar="وكيل مسودات", scope="entity", kind="agent")
    RolePermission.objects.create(role=role, codename="ledger.create_entry")
    Membership.objects.create(user=agent, entity=w.alm, role=role, valid_from=access_services.now() - timedelta(hours=1),
                              granted_by=w.mp, reason="test")
    assert draft(w, user=agent).source == "agent"
    assert draft(w).source == "human"
    assert services.create_entry(w.alm, date(2027, 1, 10), "Opening", CASH, user=w.clerk, source="import").source == "import"
    with pytest.raises(LedgerError):  # an agent cannot label its draft as a person's import
        services.create_entry(w.alm, date(2027, 1, 10), "x", CASH, user=agent, source="import")
    with pytest.raises(LedgerError):
        services.create_entry(w.alm, date(2027, 1, 10), "x", CASH, user=w.clerk, source="system")


# --- rule 4: posting locks the period ----------------------------------------------------------

@postgres_only
@pytest.mark.django_db(transaction=True)
def test_posting_waits_for_period_lock_and_sees_the_close():
    """A period closed while a posting is in flight: the posting waits for the lock and is refused."""
    from core.access.models import Role, RolePermission
    from core.access.seed import apply_seed
    from core.currency_seed import apply_currency_seed
    from core.models import Currency

    apply_seed(Role, RolePermission)
    apply_currency_seed(Currency)
    alm = setup_entity("ALM", "parent")
    clerk, cfo = make_user("clerk"), make_user("cfo")
    member(clerk, alm, "accountant")
    member(cfo, alm, "approver")
    bill7 = Bill.objects.create(entity=alm, created_by=clerk)
    grant = access.require(cfo, alm, "invoicing.approve_bill", document=bill7)
    jan = FiscalPeriod.objects.get(entity=alm, name="2027-01")
    result = {}

    def poster():
        try:
            services.create_entry(alm, date(2027, 1, 10), "Bill 7", CASH, user=clerk, post=True, grant=grant, document=bill7)
            result["outcome"] = "posted"
        except LedgerError as exc:
            result["outcome"] = str(exc)
        finally:
            connections.close_all()

    with transaction.atomic():
        FiscalPeriod.objects.select_for_update().get(pk=jan.pk)  # as close_period holds it
        thread = threading.Thread(target=poster)
        thread.start()
        thread.join(timeout=1.5)
        assert thread.is_alive(), "the posting should wait for the period lock"
        FiscalPeriod.objects.filter(pk=jan.pk).update(status=FiscalPeriod.Status.CLOSED)
    thread.join(timeout=10)
    assert "closed" in result["outcome"]
    assert not JournalEntry.objects.filter(status="posted").exists()


def test_period_lock_query_is_for_update(w):
    from django.test.utils import CaptureQueriesContext

    with transaction.atomic(), CaptureQueriesContext(connection) as ctx:
        services._find_open_period(w.alm, date(2027, 1, 10), lock=True)
    sql = ctx.captured_queries[-1]["sql"].upper()
    if connection.features.has_select_for_update:
        assert "FOR UPDATE" in sql
    else:  # SQLite: no row locks; writes are serialised by the database lock
        assert "LEDGER_FISCALPERIOD" in sql


@postgres_only
@pytest.mark.django_db(transaction=True)
def test_concurrent_settlements_cannot_over_settle():
    """Item 3: two settlements of one item in different periods; the second waits for the first and is refused."""
    from core.access.models import Role, RolePermission
    from core.access.seed import apply_seed
    from core.currency_seed import apply_currency_seed
    from core.models import Currency
    from ledger.models import ExchangeRate

    apply_seed(Role, RolePermission)
    apply_currency_seed(Currency)
    alm = setup_entity("ALM", "parent")
    clerk, cfo = make_user("clerk"), make_user("cfo")
    member(clerk, alm, "accountant")
    member(cfo, alm, "approver")
    for d in (date(2027, 1, 15), date(2027, 2, 1), date(2027, 3, 1)):
        ExchangeRate.objects.create(date=d, from_currency_id="EUR", to_currency_id="SAR", rate=D("4.1"), created_by=clerk)
    inv = services.create_entry(alm, date(2027, 1, 15), "Invoice", [
        {"account": "1200", "currency": "EUR", "txn_debit": "100.00"},
        {"account": "4100", "currency": "EUR", "txn_credit": "100.00"}], user=clerk)
    services.post_entry(inv, access.require(cfo, alm, "ledger.post_entry", document=inv))
    item = JournalLine.objects.get(entry=inv, account__code="1200")
    feb, mar = (
        services.create_entry(alm, d, "Receipt", services.fx_settlement_lines(alm, d, item, "100.00", "1100", user=clerk),
                              user=clerk)
        for d in (date(2027, 2, 1), date(2027, 3, 1))
    )
    feb_grant = access.require(cfo, alm, "ledger.post_entry", document=feb)
    mar_grant = access.require(cfo, alm, "ledger.post_entry", document=mar)
    result = {}

    def second():
        try:
            services.post_entry(mar, mar_grant)
            result["outcome"] = "posted"
        except LedgerError as exc:
            result["outcome"] = " ".join(exc.messages)
        finally:
            connections.close_all()

    with transaction.atomic():
        services.post_entry(feb, feb_grant)  # holds the item's lock until this block commits
        thread = threading.Thread(target=second)
        thread.start()
        thread.join(timeout=1.5)
        assert thread.is_alive(), "the second settlement should wait for the item lock"
    thread.join(timeout=10)
    assert "is open" in result["outcome"], result
    assert JournalEntry.objects.get(pk=mar.pk).status == "draft"
    assert services._open_item(item) == (D("0.000"), D("0.00"))


# --- item 16: reversals are never blocked by the Sharia check ---------------------------------------

def test_reversal_of_an_entry_booked_before_sharia_enforcement_is_allowed(w):
    from core import services as core_services

    conv = setup_entity("CONV", "full", w.alm, sharia_enforced=False)
    member(w.clerk, conv, "accountant")
    member(w.cfo, conv, "approver")
    Account.objects.create(entity=conv, code="4950", name_en="Interest income", name_ar="إيرادات فوائد", type="income",
                           sharia_prohibited=True)
    interest = post(w, draft(w, [{"account": "1100", "debit": 50}, {"account": "4950", "credit": 50}], entity=conv))
    core_services.change_entity_settings(conv, user=w.mp, reason="Now Sharia-compliant", sharia_enforced=True)
    new = draft(w, [{"account": "1100", "debit": 5}, {"account": "4950", "credit": 5}], entity=conv)
    with pytest.raises(LedgerError, match="Sharia-prohibited"):  # new postings are blocked (rule 5)
        post(w, new)
    rev = services.reverse_entry(interest, access.require(w.clerk, conv, "ledger.reverse_entry", document=interest))
    assert rev.status == "posted" and status(interest) == "reversed"  # undoing the old income is allowed


# --- R14 / nit: changes outside the services still count as changes of the draft ------------------

@pytest.mark.parametrize("change", [
    lambda e: JournalEntry.objects.filter(pk=e.pk).update(description="changed in bulk"),
    lambda e: JournalEntry.objects.filter(pk=e.pk).update(description="backdated", modified_at=None),
    lambda e: JournalLine.objects.filter(entry=e).update(description="changed in bulk"),
    lambda e: JournalLine.objects.filter(entry=e, debit__gt=0).update(debit=D("10.00")),
    lambda e: JournalLine.objects.filter(entry=e, account__code="4110").delete(),
    lambda e: _edit_one_line(e),
])
def test_draft_changed_after_approval_needs_a_new_approval(w, change):
    entry = draft(w, [{"account": "1100", "debit": 12}, {"account": "3100", "credit": 10}, {"account": "4110", "credit": 2}])
    grant = access.require(w.cfo, w.alm, "ledger.post_entry", document=entry)
    change(entry)
    assert JournalEntry.objects.get(pk=entry.pk).modified_at > grant.checked_at
    with pytest.raises(access.AccessDenied) as exc:
        services.post_entry(entry, grant)
    assert exc.value.code == "grant_document_changed" and status(entry) == "draft"


def _edit_one_line(entry):
    line = JournalLine.objects.filter(entry=entry).first()
    line.description = "edited through the ORM"
    line.save()
