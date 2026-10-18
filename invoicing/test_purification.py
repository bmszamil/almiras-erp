"""Purification of non-compliant income (invoicing R6, R54, E14, E15, D31b; AT-45 amended, AT-71; CLAUDE.md rule 5).

Transfer (Dr 4900 / Cr 2600), funding (Dr 1140 / Cr operating bank, D31b) and payout (Dr 2600 / Cr 1140 to an
approved charity recipient, D31b) are recorded by one person and posted by a second; agents and system users never.
Amounts, IBANs and names are made up.
"""
from datetime import date

import pytest

from banking import services as banking
from banking.testing import make_iban
from core import access
from core.access.testing import make_agent, make_user, member
from invoicing import services
from invoicing.conftest import D, final, invoice
from invoicing.models import InvoicingError, PurificationInstruction, PurificationTransfer
from ledger import services as ledger
from ledger.models import Account, FiscalPeriod, JournalLine

pytestmark = pytest.mark.django_db
PURIFY_IBAN = make_iban("20", "000000000000501111")
CHARITY_IBAN = make_iban("80", "000000000000502222")


def flagged_invoice(w, amount="1200.00"):
    return final(w, invoice(w, [("other", "1", amount, {"income_account": "4900"})]))


def purification_account(w, entity=None, *, preparer=None, approver=None, iban=PURIFY_IBAN, code="RIYAD-PUR"):
    """D31b: the entity's purification account (SAR, Saudi IBAN, GL 1140), drafted and approved by two people."""
    entity = entity or w.alm
    account = banking.create_bank_account(
        entity, user=preparer or w.accountant, code=code, name_en="Purification account", name_ar="حساب التطهير",
        bank="riyad", currency="SAR", gl_account=Account.objects.get(entity=entity, code="1140"),
        sharia_basis="islamic", statement_format="mt940", opening_statement_date=date(2027, 1, 1),
        opening_statement_balance=D("0.00"), active_from=date(2027, 1, 1), iban=iban, is_purification_account=True)
    return banking.approve_bank_account(account, user=approver or w.cfo)


def managing_partner(w):
    if not hasattr(w, "mp"):
        w.mp = make_user("mp")
        member(w.mp, w.alm, "managing_partner")
    return w.mp


def recipient(w, entity=None, *, iban=CHARITY_IBAN, approve=True, adder=None, name_en="Test Charity",
              name_ar="جمعية خيرية تجريبية"):
    """D31b: a charity recipient added by the accountant and (by default) approved by the Managing Partner."""
    rec = services.create_charity_recipient(entity or w.alm, user=adder or w.accountant, name_en=name_en,
                                            name_ar=name_ar, bank="alrajhi", iban=iban,
                                            platform_reference="IHSAN-TEST-0001")
    return board_approve(w, rec) if approve else rec


def board_approve(w, rec, *, user=None, on=None, minute="BOARD-2026-07"):
    """C5: the Managing Partner records the board's approval of ``rec`` (minute reference and date, default today)."""
    from django.utils import timezone

    return services.approve_charity_recipient(rec, user=user or managing_partner(w), board_minute_reference=minute,
                                              board_approved_on=on or timezone.localdate())


def ready(w, amount="1000.00", on=date(2027, 1, 31), *, fund=True):
    """Flagged income of ``amount``, transferred to 2600 and (by default) funded into the purification account."""
    flagged_invoice(w, amount)
    _posted(w, "transfer", amount, on)
    account = purification_account(w)
    if fund:
        _posted(w, "funding", amount, on, bank_account="1100")
    return account, recipient(w)


def balance(w, code):
    lines = JournalLine.objects.filter(entry__entity=w.alm, account__code=code, entry__status__in=ledger.BOOKED)
    return sum((l.credit - l.debit for l in lines), D("0"))


def cash(w):
    """D31b: what the purification account holds in the books (1140, debit balance)."""
    return -balance(w, "1140")


def je(doc):
    doc.refresh_from_db()
    return sorted((l.account.code, l.debit, l.credit) for l in doc.journal_entry.lines.select_related("account"))


def test_at45_transfer_funding_then_payout_by_a_second_person(w):
    """AT-45 (amended D31b): E14, then the funding of the purification account, then E15 from 1140 to an approved
    charity recipient, each posted by a second person; 4900, 2600 and 1140 net to zero for that amount."""
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
    account = purification_account(w)
    f = services.create_purification_transfer(w.alm, user=w.accountant, kind="funding", date=date(2027, 2, 1),
                                              amount="1200.00", bank_account="1100", reference_en="Fund January")
    with pytest.raises(access.AccessDenied):
        services.post_purification_transfer(f, user=w.accountant)
    f = services.post_purification_transfer(f, user=w.cfo)
    assert je(f) == [("1100", D("0"), D("1200.00")), ("1140", D("1200.00"), D("0"))]
    assert f.purification_account == account
    charity = recipient(w)
    p = services.create_purification_transfer(w.alm, user=w.accountant, kind="payout", date=date(2027, 2, 5),
                                              amount="1200.00", recipient=charity)
    assert (p.recipient, p.recipient_ar, p.bank_account.code) == (charity, "جمعية خيرية تجريبية", "1140")
    with pytest.raises(access.AccessDenied):  # the accountant does not approve payouts
        services.post_purification_transfer(p, user=w.accountant)
    p = services.post_purification_transfer(p, user=w.cfo)
    assert je(p) == [("1140", D("0"), D("1200.00")), ("2600", D("1200.00"), D("0"))]
    assert balance(w, "4900") == balance(w, "2600") == balance(w, "1140") == D("0")
    instruction = PurificationInstruction.objects.get(payout=p)
    assert (instruction.amount, instruction.currency_id, instruction.status, instruction.recipient) == (
        D("1200.00"), "SAR", "pending", charity)
    assert instruction.beneficiary_iban == CHARITY_IBAN and instruction.reference.startswith(p.number)
    # AT-71: the E14 debit on 4900 is not flagged, so the flagged amount is still 1,200.00; 2600 shows the transfer
    assert ledger.purification_amount(w.alm, user=w.accountant) == D("1200.00")
    assert services.purification_position(w.alm, user=w.accountant) == {
        "flagged": D("1200.00"), "transferred": D("1200.00"), "funded": D("1200.00"), "paid_out": D("1200.00"),
        "to_transfer": D("0.00"), "to_fund": D("0.00"), "payable": D("0.00"),
        "purification_account_balance": D("0.00")}
    for user in (make_agent("purifier"), w.queue):  # AT-45: agents and system users are refused
        with pytest.raises(access.AccessDenied):
            services.create_purification_transfer(w.alm, user=user, kind="payout", date=date(2027, 2, 5),
                                                  amount="1.00", recipient=charity)


def test_r54_never_more_than_flagged_or_payable(w):
    flagged_invoice(w, "500.00")
    with pytest.raises(InvoicingError) as exc:
        services.create_purification_transfer(w.alm, user=w.accountant, kind="transfer", date=date(2027, 1, 31),
                                              amount="500.01")
    assert exc.value.code == "purification_invalid" and "500.00" in str(exc.value)
    purification_account(w)
    charity = recipient(w)
    with pytest.raises(InvoicingError):  # nothing transferred yet: nothing to pay out
        services.create_purification_transfer(w.alm, user=w.accountant, kind="payout", date=date(2027, 1, 31),
                                              amount="1.00", recipient=charity)
    first = services.create_purification_transfer(w.alm, user=w.accountant, kind="transfer", date=date(2027, 1, 31),
                                                  amount="300.00")
    second = services.create_purification_transfer(w.alm, user=w.accountant, kind="transfer",
                                                   date=date(2027, 1, 31), amount="300.00")
    services.post_purification_transfer(first, user=w.cfo)
    with pytest.raises(InvoicingError):  # checked again at posting: only 200.00 is left
        services.post_purification_transfer(second, user=w.cfo)
    assert PurificationTransfer.objects.get(pk=second.pk).status == "draft"
    with pytest.raises(InvoicingError) as exc:  # a payout names an approved recipient (D31b)
        services.create_purification_transfer(w.alm, user=w.accountant, kind="payout", date=date(2027, 1, 31),
                                              amount="100.00")
    assert exc.value.code == "purification_recipient_not_approved"


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
    _, charity = ready(w, "1000.00")
    _posted(w, "payout", "1000.00", date(2027, 2, 20), recipient=charity)
    with pytest.raises(InvoicingError) as exc:
        services.create_purification_transfer(w.alm, user=w.accountant, kind="payout", date=date(2027, 2, 1),
                                              amount="1000.00", recipient=charity)
    assert exc.value.code == "purification_invalid" and "2600" in str(exc.value)
    pos = services.purification_position(w.alm, user=w.accountant)
    assert (pos["paid_out"], pos["payable"]) == (D("1000.00"), D("0.00"))
    assert balance(w, "2600") == D("0.00")


def test_r54_a_payout_before_its_transfer_is_refused_even_if_paid_later(w):
    _, charity = ready(w, "1000.00", date(2027, 2, 10))
    with pytest.raises(InvoicingError):  # on 2027-02-01 nothing was on 2600 yet
        services.create_purification_transfer(w.alm, user=w.accountant, kind="payout", date=date(2027, 2, 1),
                                              amount="1.00", recipient=charity)
    ok = services.create_purification_transfer(w.alm, user=w.accountant, kind="payout", date=date(2027, 2, 10),
                                               amount="400.00", recipient=charity)
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


# --- review finding 8: reversal of transfers and payouts --------------------------------------------------------

def _posted(w, kind, amount, on, **extra):
    doc = services.create_purification_transfer(w.alm, user=w.accountant, kind=kind, date=on, amount=amount, **extra)
    return services.post_purification_transfer(doc, user=w.cfo)


def test_r54_reversed_transfer_goes_through_the_ledger_and_stops_counting(w):
    flagged_invoice(w, "1000.00")
    t = _posted(w, "transfer", "1000.00", date(2027, 1, 31))
    with pytest.raises(InvoicingError) as exc:
        services.reverse_purification_transfer(t, user=w.accountant, reason=" ")
    assert exc.value.code == "reason_required"
    for user in (w.clerk, w.cfo, make_agent("purifier")):  # invoicing.reverse_document, people only
        with pytest.raises(access.AccessDenied):
            services.reverse_purification_transfer(t, user=user, reason="خطأ في المبلغ")
    r = services.reverse_purification_transfer(t, user=w.accountant, reason="خطأ في المبلغ")
    assert (r.status, r.reversed_by, r.reversal_reason) == ("reversed", w.accountant, "خطأ في المبلغ")
    assert r.reversal_entry.reversal_of_id == r.journal_entry_id and r.reversal_entry.status == "posted"
    assert r.reversal_entry.date == date(2027, 1, 31)
    assert balance(w, "4900") == D("1000.00") and balance(w, "2600") == D("0")  # back on 4900
    pos = services.purification_position(w.alm, user=w.accountant)
    assert (pos["transferred"], pos["to_transfer"]) == (D("0"), D("1000.00"))
    assert _posted(w, "transfer", "1000.00", date(2027, 2, 1)).status == "posted"  # the room is back
    with pytest.raises(InvoicingError) as exc:
        services.reverse_purification_transfer(r, user=w.accountant, reason="مرة أخرى")
    assert exc.value.code == "not_posted"
    stored = PurificationTransfer.objects.get(pk=t.pk)
    stored.reversal_reason = "x"
    with pytest.raises(InvoicingError):
        stored.save()


def test_r54_a_transfer_with_payouts_or_fundings_after_it_is_not_reversed_first(w):
    _, charity = ready(w, "1000.00")
    t = PurificationTransfer.objects.get(kind="transfer")
    f = PurificationTransfer.objects.get(kind="funding")
    p = _posted(w, "payout", "600.00", date(2027, 2, 5), recipient=charity)
    with pytest.raises(InvoicingError) as exc:
        services.reverse_purification_transfer(t, user=w.accountant, reason="خطأ")
    assert exc.value.code == "purification_invalid" and "600.00" in str(exc.value)
    assert PurificationTransfer.objects.get(pk=t.pk).status == "posted"
    p = services.reverse_purification_transfer(p, user=w.accountant, reason="صرف مكرر")
    assert p.status == "reversed" and balance(w, "2600") == D("1000.00") and cash(w) == D("1000.00")
    assert services.purification_position(w.alm, user=w.accountant)["payable"] == D("1000.00")
    with pytest.raises(InvoicingError) as exc:  # D31b: the funding of 1,000.00 still rests on this transfer
        services.reverse_purification_transfer(t, user=w.accountant, reason="خطأ")
    assert exc.value.code == "purification_invalid" and "fundings" in str(exc.value)
    services.reverse_purification_transfer(f, user=w.accountant, reason="تمويل خاطئ")
    assert services.reverse_purification_transfer(t, user=w.accountant, reason="خطأ").status == "reversed"
    assert balance(w, "1140") == balance(w, "2600") == D("0") and balance(w, "4900") == D("1000.00")


def test_r54_reversal_in_a_closed_period_moves_to_the_next_open_one(w):
    flagged_invoice(w, "1000.00")
    t = _posted(w, "transfer", "300.00", date(2027, 1, 31))
    FiscalPeriod.objects.filter(entity=w.alm, name="2027-01").update(status="closed")
    r = services.reverse_purification_transfer(t, user=w.accountant, reason="خطأ")
    assert r.reversal_entry.date == date(2027, 2, 1)
