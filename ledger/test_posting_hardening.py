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

pytestmark = pytest.mark.django_db
postgres_only = pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL-only database guard")
CASH = [{"account": "1100", "debit": 10}, {"account": "3100", "credit": 10}]
D = Decimal


def setup_entity(code, consolidation="full", parent=None, **extra):
    e = Entity.objects.create(code=code, name_en=code, name_ar=f"كيان {code}", consolidation=consolidation, parent=parent, **extra)
    install_chart_of_accounts(e)
    create_monthly_periods(e, 2027)
    return e


def doc(label, pk, created_by=None):
    """Stand-in for a sub-ledger document (invoicing not built yet)."""
    return SimpleNamespace(pk=pk, _meta=SimpleNamespace(label_lower=label), created_by_id=getattr(created_by, "pk", None),
                           submitted_by_id=None)


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
    entry = draft(w)
    receipt = doc("invoicing.receipt", 3, created_by=w.clerk)
    for document in (receipt, entry):
        grant = access.require(w.clerk, w.alm, "invoicing.post_receipt", document=document)
        with pytest.raises(access.AccessDenied) as exc:
            services.post_entry(entry, grant)
        assert exc.value.code == "grant_wrong_permission"
    assert status(entry) == "draft"
    # The receipt grant does post the receipt's own entry, once.
    grant = access.require(w.clerk, w.alm, "invoicing.post_receipt", document=receipt)
    booked = services.create_entry(w.alm, date(2027, 1, 10), "Receipt 3", CASH, user=w.clerk, post=True, grant=grant,
                                   document=receipt)
    assert booked.status == "posted" and booked.document_ref == ("invoicing.receipt", 3) and booked.posted_by == w.clerk


def test_approve_bill_grant_for_bill_7_cannot_post_an_unrelated_draft(w):
    bill7 = doc("invoicing.bill", 7, created_by=w.clerk)
    grant = access.require(w.cfo, w.alm, "invoicing.approve_bill", document=bill7)
    unrelated = draft(w)
    with pytest.raises(access.AccessDenied) as exc:
        services.post_entry(unrelated, grant)
    assert exc.value.code == "grant_wrong_permission" and status(unrelated) == "draft"
    before = JournalEntry.objects.count()
    for other in (doc("invoicing.bill", 8, created_by=w.clerk), doc("invoicing.receipt", 7), unrelated):
        with pytest.raises(access.AccessDenied) as exc:
            services.create_entry(w.alm, date(2027, 1, 10), "Bill", CASH, user=w.clerk, post=True, grant=grant, document=other)
        assert exc.value.code == "grant_wrong_document"
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

    bill7 = doc("invoicing.bill", 7, created_by=w.clerk)
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
    bill7 = doc("invoicing.bill", 7, created_by=w.clerk)
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
    assert status(booked) == "reversed" and rev.document_ref == ("invoicing.bill", 7)


def test_zatca_queue_grant_cannot_post_a_manual_journal(w):
    queue = access.system_user("zatca_queue")
    member(queue, w.alm, "system_zatca_queue")
    invoice = doc("invoicing.salesdocument", 11, created_by=w.clerk)
    access.require(w.cfo, w.alm, "invoicing.approve_sales_document", document=invoice)
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


def test_clearance_posted_by_must_be_a_recorded_active_human_approver(w):
    queue = access.system_user("zatca_queue")
    member(queue, w.alm, "system_zatca_queue")
    invoice = doc("invoicing.salesdocument", 11, created_by=w.clerk)
    other_invoice = doc("invoicing.salesdocument", 12, created_by=w.clerk)
    access.require(w.cfo, w.alm, "invoicing.approve_sales_document", document=other_invoice)  # approved another one
    grant = access.require(queue, w.alm, "invoicing.post_on_clearance", document=invoice)

    def clear(posted_by):
        return services.create_entry(w.alm, date(2027, 1, 10), "Invoice 11", CASH, user=queue, post=True, grant=grant,
                                     document=invoice, posted_by=posted_by)

    for candidate in (w.cfo, queue, make_agent()):  # not the approver of invoice 11 / not a human
        with pytest.raises(access.AccessDenied) as exc:
            clear(candidate)
        assert exc.value.code == "posted_by_not_approver"
    access.require(w.cfo, w.alm, "invoicing.approve_sales_document", document=invoice)
    w.cfo.is_active = False
    with pytest.raises(access.AccessDenied):
        clear(w.cfo)
    w.cfo.is_active = True
    assert clear(w.cfo).posted_by == w.cfo
    assert not JournalEntry.objects.filter(description="Invoice 11").exclude(posted_by=w.cfo).exists()


def test_create_entry_post_true_needs_grant_user_or_create_right(w):  # item 5
    bill7 = doc("invoicing.bill", 7, created_by=w.clerk)
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
    # The one change a posted row allows: posted -> reversed, nothing else (what reverse_entry writes).
    _raw("UPDATE ledger_journalentry SET status = 'reversed' WHERE id = %s", [posted.pk])
    with pytest.raises(DatabaseError):
        _raw("UPDATE ledger_journalentry SET status = 'posted' WHERE id = %s", [posted.pk])
    _raw("UPDATE ledger_journalline SET description = 'ok' WHERE entry_id = %s", [open_draft.pk])  # drafts are free


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
    # A reversal's lines are debits on 4900: not flagged; the original keeps its flag.
    rev = services.reverse_entry(posted, access.require(w.clerk, w.alm, "ledger.reverse_entry", document=posted))
    assert not JournalLine.objects.filter(entry=rev, requires_purification=True).exists()
    assert JournalLine.objects.filter(entry=posted, requires_purification=True).count() == 1


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
    bill7 = doc("invoicing.bill", 7, created_by=clerk)
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
