"""Purification of non-compliant income (invoicing R6, R54, E14, E15; AT-45, AT-71; CLAUDE.md rule 5).

Transfer (Dr 4900 / Cr 2600) and payout (Dr 2600 / Cr bank) are recorded by one person and posted by a second;
agents and system users never. Amounts are made up.
"""
from datetime import date

import pytest

from core import access
from core.access.testing import make_agent
from invoicing import services
from invoicing.conftest import D, final, invoice
from invoicing.models import InvoicingError, PurificationTransfer
from ledger import services as ledger
from ledger.models import FiscalPeriod, JournalLine

pytestmark = pytest.mark.django_db


def flagged_invoice(w, amount="1200.00"):
    return final(w, invoice(w, [("other", "1", amount, {"income_account": "4900"})]))


def balance(w, code):
    lines = JournalLine.objects.filter(entry__entity=w.alm, account__code=code, entry__status__in=ledger.BOOKED)
    return sum((l.credit - l.debit for l in lines), D("0"))


def je(doc):
    doc.refresh_from_db()
    return sorted((l.account.code, l.debit, l.credit) for l in doc.journal_entry.lines.select_related("account"))


def test_at45_transfer_then_payout_by_a_second_person(w):
    flagged_invoice(w)
    assert services.purification_position(w.alm, user=w.accountant)["to_transfer"] == D("1200.00")
    t = services.create_purification_transfer(w.alm, user=w.accountant, kind="transfer", date=date(2027, 1, 31),
                                              amount="1200.00", reference_ar="تطهير يناير")
    with pytest.raises(access.AccessDenied) as exc:  # the recorder never posts it
        services.post_purification_transfer(t, user=w.accountant)
    assert exc.value.code == "self_approval"
    t = services.post_purification_transfer(t, user=w.cfo)
    assert (t.status, t.number, t.posted_by) == ("posted", "ALM-PUR-2027-000001", w.cfo)
    assert je(t) == [("2600", D("0"), D("1200.00")), ("4900", D("1200.00"), D("0"))]
    assert t.journal_entry.reference == t.number and t.journal_entry.posted_by == w.cfo
    p = services.create_purification_transfer(w.alm, user=w.accountant, kind="payout", date=date(2027, 2, 5),
                                              amount="1200.00", bank_account="1100", recipient_ar="جمعية خيرية تجريبية",
                                              recipient_en="Test Charity")
    with pytest.raises(access.AccessDenied):  # the accountant does not approve payouts
        services.post_purification_transfer(p, user=w.accountant)
    p = services.post_purification_transfer(p, user=w.cfo)
    assert je(p) == [("1100", D("0"), D("1200.00")), ("2600", D("1200.00"), D("0"))]
    assert balance(w, "4900") == balance(w, "2600") == D("0")  # 4900 and 2600 net to zero for that amount
    # AT-71: the E14 debit on 4900 is not flagged, so the flagged amount is still 1,200.00; 2600 shows the transfer
    assert ledger.purification_amount(w.alm, user=w.accountant) == D("1200.00")
    assert services.purification_position(w.alm, user=w.accountant) == {
        "flagged": D("1200.00"), "transferred": D("1200.00"), "paid_out": D("1200.00"), "to_transfer": D("0.00"),
        "payable": D("0.00")}


def test_r54_never_more_than_flagged_or_payable(w):
    flagged_invoice(w, "500.00")
    with pytest.raises(InvoicingError) as exc:
        services.create_purification_transfer(w.alm, user=w.accountant, kind="transfer", date=date(2027, 1, 31),
                                              amount="500.01")
    assert exc.value.code == "purification_invalid" and "500.00" in str(exc.value)
    with pytest.raises(InvoicingError):  # nothing transferred yet: nothing to pay out
        services.create_purification_transfer(w.alm, user=w.accountant, kind="payout", date=date(2027, 1, 31),
                                              amount="1.00", recipient_ar="جمعية")
    first = services.create_purification_transfer(w.alm, user=w.accountant, kind="transfer", date=date(2027, 1, 31),
                                                  amount="300.00")
    second = services.create_purification_transfer(w.alm, user=w.accountant, kind="transfer",
                                                   date=date(2027, 1, 31), amount="300.00")
    services.post_purification_transfer(first, user=w.cfo)
    with pytest.raises(InvoicingError):  # checked again at posting: only 200.00 is left
        services.post_purification_transfer(second, user=w.cfo)
    assert PurificationTransfer.objects.get(pk=second.pk).status == "draft"
    with pytest.raises(InvoicingError):  # a payout names its recipient in Arabic
        services.create_purification_transfer(w.alm, user=w.accountant, kind="payout", date=date(2027, 1, 31),
                                              amount="100.00")


def test_r54_agents_system_users_and_other_roles_are_refused(w):
    flagged_invoice(w)
    for user in (make_agent("purifier"), w.queue, w.clerk):
        with pytest.raises(access.AccessDenied):
            services.create_purification_transfer(w.alm, user=user, kind="transfer", date=date(2027, 1, 31),
                                                  amount="100.00")
    assert not PurificationTransfer.objects.exists()


def test_r54_closed_period_refused_and_posted_rows_are_locked(w):
    flagged_invoice(w)
    t = services.create_purification_transfer(w.alm, user=w.accountant, kind="transfer", date=date(2027, 1, 31),
                                              amount="100.00")
    FiscalPeriod.objects.filter(entity=w.alm, name="2027-01").update(status="closed")
    with pytest.raises(InvoicingError):
        services.post_purification_transfer(t, user=w.cfo)
    stored = PurificationTransfer.objects.get(pk=t.pk)
    assert (stored.status, stored.number) == ("draft", "")
    FiscalPeriod.objects.filter(entity=w.alm, name="2027-01").update(status="open")
    t = services.post_purification_transfer(t, user=w.cfo)
    t.amount = D("1.00")
    with pytest.raises(InvoicingError):
        t.save()
    with pytest.raises(InvoicingError):
        t.delete()
    with pytest.raises(InvoicingError):
        PurificationTransfer.objects.filter(pk=t.pk).update(amount=D("1.00"))


# --- review finding 1: limits hold on the document date and on every later date --------------------------------

def test_r54_backdated_transfer_cannot_exceed_what_was_flagged(w):
    """Reviewer probe: a transfer dated before an existing one passed the as-of-date check, so 2,000.00 was
    transferred for 1,000.00 flagged. The limit now also holds on every later date (all-time)."""
    flagged_invoice(w, "1000.00")
    late = services.create_purification_transfer(w.alm, user=w.accountant, kind="transfer", date=date(2027, 2, 20),
                                                 amount="1000.00")
    services.post_purification_transfer(late, user=w.cfo)
    with pytest.raises(InvoicingError) as exc:
        services.create_purification_transfer(w.alm, user=w.accountant, kind="transfer", date=date(2027, 1, 31),
                                              amount="1000.00")
    assert exc.value.code == "purification_invalid" and "2027-02-20" in str(exc.value)
    pos = services.purification_position(w.alm, user=w.accountant)
    assert (pos["transferred"], pos["to_transfer"]) == (D("1000.00"), D("0.00"))
    assert balance(w, "4900") == D("0.00")


def test_r54_backdated_transfer_drafted_earlier_is_refused_at_posting(w):
    flagged_invoice(w, "1000.00")
    early = services.create_purification_transfer(w.alm, user=w.accountant, kind="transfer", date=date(2027, 1, 31),
                                                  amount="1000.00")
    late = services.create_purification_transfer(w.alm, user=w.accountant, kind="transfer", date=date(2027, 2, 20),
                                                 amount="1000.00")
    services.post_purification_transfer(late, user=w.cfo)
    with pytest.raises(InvoicingError) as exc:
        services.post_purification_transfer(early, user=w.cfo)
    assert exc.value.code == "purification_invalid"
    assert PurificationTransfer.objects.get(pk=early.pk).status == "draft"


def test_r54_backdated_payout_cannot_exceed_what_was_transferred(w):
    """Reviewer probe: two payouts of 1,000.00 for one 1,000.00 transfer (the earlier-dated one passed its date)."""
    flagged_invoice(w, "1000.00")
    t = services.create_purification_transfer(w.alm, user=w.accountant, kind="transfer", date=date(2027, 1, 31),
                                              amount="1000.00")
    services.post_purification_transfer(t, user=w.cfo)
    late = services.create_purification_transfer(w.alm, user=w.accountant, kind="payout", date=date(2027, 2, 20),
                                                 amount="1000.00", bank_account="1100", recipient_ar="جمعية")
    services.post_purification_transfer(late, user=w.cfo)
    with pytest.raises(InvoicingError) as exc:
        services.create_purification_transfer(w.alm, user=w.accountant, kind="payout", date=date(2027, 2, 1),
                                              amount="1000.00", bank_account="1100", recipient_ar="جمعية")
    assert exc.value.code == "purification_invalid" and "2600" in str(exc.value)
    pos = services.purification_position(w.alm, user=w.accountant)
    assert (pos["paid_out"], pos["payable"]) == (D("1000.00"), D("0.00"))
    assert balance(w, "2600") == D("0.00")


def test_r54_a_payout_before_its_transfer_is_refused_even_if_paid_later(w):
    flagged_invoice(w, "1000.00")
    t = services.create_purification_transfer(w.alm, user=w.accountant, kind="transfer", date=date(2027, 2, 10),
                                              amount="1000.00")
    services.post_purification_transfer(t, user=w.cfo)
    with pytest.raises(InvoicingError):  # on 2027-02-01 nothing was on 2600 yet
        services.create_purification_transfer(w.alm, user=w.accountant, kind="payout", date=date(2027, 2, 1),
                                              amount="1.00", bank_account="1100", recipient_ar="جمعية")
    ok = services.create_purification_transfer(w.alm, user=w.accountant, kind="payout", date=date(2027, 2, 10),
                                               amount="400.00", bank_account="1100", recipient_ar="جمعية")
    assert services.post_purification_transfer(ok, user=w.cfo).status == "posted"


def test_r54_posting_locks_the_entity_row(w):
    """Concurrent posts serialise on the entity row (FOR NO KEY UPDATE on PostgreSQL)."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    flagged_invoice(w, "1000.00")
    t = services.create_purification_transfer(w.alm, user=w.accountant, kind="transfer", date=date(2027, 1, 31),
                                              amount="100.00")
    with CaptureQueriesContext(connection) as queries:
        services.post_purification_transfer(t, user=w.cfo)
    entity_reads = [q["sql"] for q in queries.captured_queries if 'FROM "core_entity"' in q["sql"]]
    if connection.vendor == "postgresql":
        assert any("FOR NO KEY UPDATE" in sql for sql in entity_reads)
    else:
        assert entity_reads  # the lock read runs (SQLite has no row locks)
