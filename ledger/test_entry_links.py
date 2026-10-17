"""Reversal and document links, concurrent document bookings (verification review).

Regression tests for the reviewer's probes (probe3):

- D: ``reversal_of`` set on a draft by ``QuerySet.update`` let a
  Sharia-prohibited draft post as a "reversal", skipping the Sharia and rate
  checks (CLAUDE.md rules 1, 3, 5).
- I: a forged document reference on a draft blocked the real booking of
  that document (access.md R30, R36).
- conc: two concurrent bookings of one document ended in an IntegrityError
  instead of the bilingual ``document_booked`` refusal (R36).

PostgreSQL-only tests are skipped on SQLite.
"""
import threading
from datetime import date

import pytest
from django.db import DatabaseError, connection, connections, transaction

from core import access
from core.access.testing import make_user, member
from ledger import services
from ledger.models import Account, FiscalPeriod, JournalEntry, JournalLine, LedgerError
from ledger.test_posting_hardening import CASH, doc, draft, post, postgres_only, status, w  # noqa: F401
from testdocs.models import Bill, Receipt

pytestmark = pytest.mark.django_db
SHARIA_LINES = [{"account": "1100", "debit": 10}, {"account": "2410", "credit": 10}]  # 2410: interest-bearing loan


def _raw(sql, params):
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql, params)


def _service_like_entry(w, **links):
    """A draft written the way ledger.services writes one, with the given links (bypasses no database check)."""
    entry = JournalEntry(entity=w.alm, date=date(2027, 1, 10), description="forged", created_by=w.clerk, **links)
    entry._allow_links()
    entry.save()
    return entry


def _add_lines(entry, specs):
    for code, debit, credit in specs:
        JournalLine(entry=entry, account=Account.objects.get(entity=entry.entity, code=code), debit=debit,
                    credit=credit).save()


# --- item 1: reversal_of / document links are written once, by ledger.services ---------------------

def test_probe_d_forged_reversal_link_by_bulk_update_is_refused(w):
    original = post(w, draft(w))
    forged = draft(w, lines=SHARIA_LINES)
    for kwargs in ({"reversal_of": original}, {"reversal_of_id": original.pk}):
        with pytest.raises(LedgerError, match="links"):
            JournalEntry.objects.filter(pk=forged.pk).update(**kwargs)
    forged.reversal_of = original
    with pytest.raises(LedgerError, match="links"):  # bulk_update goes through QuerySet.update
        JournalEntry.objects.bulk_update([forged], ["reversal_of"])
    with pytest.raises(LedgerError, match="links"):  # nor save() on the draft
        forged.save()
    forged = JournalEntry.objects.get(pk=forged.pk)
    assert forged.reversal_of_id is None
    with pytest.raises(LedgerError, match="Sharia-prohibited"):  # so the Sharia check still applies
        post(w, forged)
    assert status(forged) == "draft" and status(original) == "posted"


def test_probe_i_forged_document_link_on_a_draft_is_refused(w):
    receipt = doc(Receipt, w.alm, created_by=w.cfo)
    entry = draft(w)
    for kwargs in ({"document_type": "testdocs.receipt", "document_id": receipt.pk}, {"document_id": receipt.pk},
                   {"document_type": ""}):
        with pytest.raises(LedgerError, match="links"):
            JournalEntry.objects.filter(pk=entry.pk).update(**kwargs)
    entry.document_type, entry.document_id = "testdocs.receipt", receipt.pk
    with pytest.raises(LedgerError, match="links"):
        entry.save()
    grant = access.require(w.clerk, w.alm, "invoicing.post_receipt", document=receipt)
    booked = services.create_entry(w.alm, date(2027, 1, 5), "Receipt", CASH, user=w.clerk, post=True, grant=grant,
                                   document=receipt)
    assert booked.status == "posted" and booked.document_ref == ("testdocs.receipt", receipt.pk)
    with pytest.raises(LedgerError, match="links"):  # nor on a booked entry, of course
        JournalEntry.objects.filter(pk=booked.pk).update(document_id=receipt.pk + 1)


def test_links_are_only_written_when_ledger_services_create_the_entry(w):
    original = post(w, draft(w))
    bill = doc(Bill, w.alm, created_by=w.clerk)
    for links in ({"reversal_of": original}, {"document_type": "testdocs.bill", "document_id": bill.pk}):
        with pytest.raises(LedgerError, match="links"):
            JournalEntry.objects.create(entity=w.alm, date=date(2027, 1, 10), description="x", **links)
        with pytest.raises(LedgerError, match="links"):
            JournalEntry.objects.bulk_create([JournalEntry(entity=w.alm, date=date(2027, 1, 10), description="x", **links)])
    assert JournalEntry.objects.count() == 1
    reversal = services.reverse_entry(original, access.require(w.clerk, w.alm, "ledger.reverse_entry", document=original))
    assert reversal.reversal_of_id == original.pk and status(original) == "reversed"


def test_a_reversal_must_mirror_its_original(w):
    """validate_entry: a reversal that does not undo its original line by line is refused (rules 1, 5)."""
    original = post(w, draft(w))  # Dr 1100 10 / Cr 3100 10
    for specs in (
        [("1100", 0, 10), ("2410", 10, 0)],  # another (Sharia-prohibited) account
        [("1100", 0, 10), ("3100", 10, 0), ("1100", 5, 0), ("3100", 0, 5)],  # extra lines
        [("1100", 0, 20), ("3100", 20, 0)],  # other amounts
        [("1100", 10, 0), ("3100", 0, 10)],  # same side as the original
    ):
        forged = _service_like_entry(w, reversal_of=original)
        _add_lines(forged, specs)
        assert any("mirrors every line" in p for p in services.validate_entry(forged))
        with pytest.raises(LedgerError, match="mirrors every line"):
            services._post(forged, w.cfo)
        assert status(forged) == "draft"
        forged.delete()
    assert status(original) == "posted"


def test_a_reversal_reverses_a_posted_entry_only(w):
    unposted = draft(w)
    with pytest.raises((LedgerError, DatabaseError)), transaction.atomic():  # PostgreSQL: refused at insert
        forged = _service_like_entry(w, reversal_of=unposted)
        _add_lines(forged, [("1100", 0, 10), ("3100", 10, 0)])
        services._post(forged, w.cfo)
    assert status(unposted) == "draft" and JournalEntry.objects.count() == 1


def test_post_entry_refuses_a_draft_with_links(w):
    """Reversals post in reverse_entry, document entries in create_entry(post=True); never via post_entry."""
    original = post(w, draft(w))
    forged = _service_like_entry(w, reversal_of=original)
    _add_lines(forged, [("1100", 0, 10), ("3100", 10, 0)])  # even a correct mirror
    with pytest.raises(LedgerError) as exc:
        post(w, forged)
    assert exc.value.code == "draft_has_links" and status(forged) == "draft" and status(original) == "posted"


@postgres_only
def test_database_refuses_changing_links_on_a_draft(w):
    original = post(w, draft(w))
    bill = doc(Bill, w.alm, created_by=w.clerk)
    entry = draft(w, lines=SHARIA_LINES)
    for sql, params in (
        ("UPDATE ledger_journalentry SET reversal_of_id = %s WHERE id = %s", [original.pk, entry.pk]),
        ("UPDATE ledger_journalentry SET document_type = 'testdocs.bill', document_id = %s WHERE id = %s",
         [bill.pk, entry.pk]),
    ):
        with pytest.raises(DatabaseError, match="links never change"):
            _raw(sql, params)
    assert JournalEntry.objects.get(pk=entry.pk).reversal_of_id is None


@postgres_only
def test_database_refuses_a_raw_reversal_that_does_not_mirror(w):
    original = post(w, draft(w))
    with pytest.raises(DatabaseError, match="posted entry"):  # insert: the original must be posted
        _raw("INSERT INTO ledger_journalentry (entity_id, number, date, description, reference, status, reversal_of_id, "
             "created_at, source, document_type) VALUES (%s, '', '2027-01-10', 'x', '', 'draft', %s, now(), 'human', '')",
             [w.alm.pk, draft(w).pk])
    _raw("INSERT INTO ledger_journalentry (entity_id, number, date, description, reference, status, reversal_of_id, "
         "created_at, source, document_type) VALUES (%s, '', '2027-01-10', 'x', '', 'draft', %s, now(), 'human', '')",
         [w.alm.pk, original.pk])
    forged = JournalEntry.objects.get(reversal_of=original)
    _add_lines(forged, [("1100", 0, 10), ("2410", 10, 0)])
    period = FiscalPeriod.objects.get(entity=w.alm, name="2027-01")
    with pytest.raises(DatabaseError, match="do not mirror"):
        _raw("UPDATE ledger_journalentry SET status = 'posted', number = 'X-1', period_id = %s, posted_at = now(), "
             "posted_by_id = %s WHERE id = %s", [period.pk, w.cfo.pk, forged.pk])
    assert status(forged) == "draft"


# --- item 5: concurrent bookings of one document -------------------------------------------------

def test_unique_document_violation_becomes_document_booked(w, monkeypatch):
    """If the service check misses a booking that committed meanwhile, the constraint error is translated (R36)."""
    receipt = doc(Receipt, w.alm, created_by=w.cfo)
    first = access.require(w.clerk, w.alm, "invoicing.post_receipt", document=receipt)
    services.create_entry(w.alm, date(2027, 1, 5), "Receipt", CASH, user=w.clerk, post=True, grant=first, document=receipt)
    monkeypatch.setattr(services, "_document_booked", lambda entity, document: False)  # as in a race
    clerk2 = make_user("clerk2")
    member(clerk2, w.alm, "accountant")
    second = access.require(clerk2, w.alm, "invoicing.post_receipt", document=receipt)
    with pytest.raises(LedgerError) as exc:
        services.create_entry(w.alm, date(2027, 1, 5), "Receipt", CASH, user=clerk2, post=True, grant=second,
                              document=receipt)
    assert exc.value.code == "document_booked" and "مرحّل مسبقًا" in str(exc.value)
    assert JournalEntry.objects.filter(document_id=receipt.pk).count() == 1


@postgres_only
@pytest.mark.django_db(transaction=True, databases=["default", "access_log"])
def test_concurrent_bookings_of_one_document(request):
    from core.access.models import Role, RolePermission
    from core.access.seed import apply_seed
    from core.currency_seed import apply_currency_seed
    from core.models import Currency

    apply_seed(Role, RolePermission)
    apply_currency_seed(Currency)
    world = request.getfixturevalue("w")
    clerk2 = make_user("clerk2")
    member(clerk2, world.alm, "accountant")
    receipt = doc(Receipt, world.alm, created_by=world.cfo)
    users = (world.clerk, clerk2)
    grants = [access.require(u, world.alm, "invoicing.post_receipt", document=receipt) for u in users]
    results, barrier = [], threading.Barrier(2)

    def book(grant, user):
        try:
            barrier.wait()
            services.create_entry(world.alm, date(2027, 1, 5), "dup", CASH, user=user, post=True, grant=grant,
                                  document=receipt)
            results.append("ok")
        except LedgerError as exc:
            results.append(exc.code)
        except Exception as exc:  # noqa: BLE001 - recorded and asserted below
            results.append(type(exc).__name__)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=book, args=pair) for pair in zip(grants, users)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(results) == ["document_booked", "ok"]
    assert JournalEntry.objects.filter(document_type="testdocs.receipt", document_id=receipt.pk).count() == 1


@postgres_only
@pytest.mark.parametrize("column, value", [("line_kind", "'rounding'"), ("rate_source", "'manual'")])
def test_l2_database_mirror_check_compares_rate_source_and_line_kind(w, column, value):
    """Spec-writer follow-up L2: a raw "reversal" that mirrors amounts but not the line kind or rate source."""
    original = post(w, draft(w))  # Dr 1100 10 / Cr 3100 10
    _raw("INSERT INTO ledger_journalentry (entity_id, number, date, description, reference, status, reversal_of_id, "
         "created_at, source, document_type) VALUES (%s, '', '2027-01-10', 'x', '', 'draft', %s, now(), 'human', '')",
         [w.alm.pk, original.pk])
    forged = JournalEntry.objects.get(reversal_of=original)
    _add_lines(forged, [("1100", 0, 10), ("3100", 10, 0)])  # exactly mirrored amounts
    _raw(f"UPDATE ledger_journalline SET {column} = {value} WHERE entry_id = %s AND debit > 0", [forged.pk])
    period = FiscalPeriod.objects.get(entity=w.alm, name="2027-01")
    with pytest.raises(DatabaseError, match="do not mirror"):
        _raw("UPDATE ledger_journalentry SET status = 'posted', number = 'X-1', period_id = %s, posted_at = now(), "
             "posted_by_id = %s WHERE id = %s", [period.pk, w.cfo.pk, forged.pk])
    assert status(forged) == "draft"
