"""Reconciliation (banking R36 to R41; AT-22, AT-23, AT-28, AT-30). Reconciliation posts nothing."""
import re
from datetime import date
from decimal import Decimal as D

import pytest

from banking import services
from banking.conftest import CUTOVER, IBANS, journal, receipt, statement, vendor_payment
from banking.models import BankingError, BankingEvent, Match, Reconciliation
from banking.testing import make_iban, mt940
from core import access
from core.access.testing import make_user, member
from ledger.models import FiscalPeriod, JournalEntry

JAN1, JAN31 = date(2027, 1, 1), date(2027, 1, 31)
ARABIC = re.compile(r"[؀-ۿ]")


def period(w, name, entity=None):
    return FiscalPeriod.objects.get(entity=entity or w.alm, name=name)


def preparer_and_approver(w):
    both = make_user("prep_approver")
    member(both, w.alm, "accountant")
    member(both, w.alm, "approver")
    return both


@pytest.fixture
def january(w, accounts):
    """AT-22: RIYAD-SAR January with a matched receipt, an unpresented payment and an unmatched bank credit."""
    receipt(w, "11475.00", date(2027, 1, 14), bank_reference="TRX88")
    imp = statement(w, accounts.riyad, [(date(2027, 1, 15), "C", "11475.00", "NTRF", "", "TRX88", "Incoming"),
                                        (date(2027, 1, 28), "C", "500.00", "NTRF", "", "BR77", "Unknown deposit")],
                    start=JAN1, end=JAN31)
    payment = vendor_payment(w, "4000.00", date(2027, 1, 30))
    return imp, payment


def test_at22_prepare_approve_lock_and_reopen(w, accounts, january):
    imp, payment = january
    both = preparer_and_approver(w)
    entries = JournalEntry.objects.count()
    rec = services.prepare_reconciliation(accounts.riyad, period(w, "2027-01"), user=both)
    assert (rec.bank_balance, rec.book_balance, rec.outstanding_book_credits, rec.unmatched_bank_credits,
            rec.difference) == (D("111975.000"), D("107475.000"), D("4000.000"), D("500.000"), D("0"))
    assert sorted((i["type"], i["amount"]) for i in rec.items) == [("bank", "500.000"), ("book", "4000.000")]
    unmatched = imp.lines.get(line_no=2)
    services.add_reconciliation_note(rec, user=both, statement_line=unmatched, note_en="Customer to be identified",
                                     note_ar="عميل قيد التحديد")
    services.submit_reconciliation(rec, user=w.a2)
    with pytest.raises(access.AccessDenied) as exc:
        services.approve_reconciliation(rec, user=both)  # the preparer does not approve
    assert exc.value.code == "self_approval"
    with pytest.raises(access.AccessDenied):
        services.approve_reconciliation(rec, user=w.a2)  # neither does the submitter (no right either)
    rec = services.approve_reconciliation(rec, user=w.p)
    assert rec.status == "approved" and JournalEntry.objects.count() == entries  # R41: posts nothing
    match = Match.objects.get(bank_account=accounts.riyad, status="confirmed")
    assert match.reconciliation == rec
    assert any(i.get("note_en") == "Customer to be identified" for i in rec.items)  # frozen with its notes
    assert services.statement_line_state(imp.lines.get(line_no=1), user=w.a) == "locked"
    with pytest.raises(BankingError, match="locked"):
        services.undo_match(match, user=w.a, reason="try")
    late = mt940(statement_id="LATE", sequence=9, currency="SAR", opening="111975.00", opening_date=date(2027, 1, 25),
                 closing_date=JAN31, lines=[], account=IBANS["RIYAD-SAR"])
    with pytest.raises(BankingError, match="reconciled"):  # R14
        services.import_statement(accounts.riyad, late, user=w.a)
    # A payment dated in January but posted after the approval appears in February, flagged.
    late_payment = vendor_payment(w, "300.00", date(2027, 1, 29))
    statement(w, accounts.riyad, [], start=date(2027, 2, 1), end=date(2027, 2, 28))
    feb = services.prepare_reconciliation(accounts.riyad, period(w, "2027-02"), user=w.a)
    flagged = [i for i in feb.items if i.get("posted_after_reconciliation")]
    assert [i["amount"] for i in flagged] == ["300.000"] and feb.difference == D("0")
    assert late_payment.journal_entry.number == flagged[0]["reference"]
    assert Reconciliation.objects.get(pk=rec.pk).book_balance == D("107475.000")  # January unchanged
    with pytest.raises(access.AccessDenied):
        services.reopen_reconciliation(rec, user=w.t, reason="x")  # group treasury has no reopen right
    with pytest.raises(BankingError, match="reason"):
        services.reopen_reconciliation(rec, user=w.p, reason=" ")
    reopened = services.reopen_reconciliation(rec, user=w.p, reason="Late payment belongs to January")
    assert reopened.status == "superseded" and reopened.reopened_by == w.p
    match.refresh_from_db()
    assert match.reconciliation_id is None  # locks released
    assert BankingEvent.objects.filter(event="reconciliation_reopened", object_id=rec.pk).exists()
    fresh = services.prepare_reconciliation(accounts.riyad, period(w, "2027-01"), user=w.a)
    assert fresh.pk != rec.pk and fresh.outstanding_book_credits == D("4300.000")


def test_r39_period_by_period(w, accounts, january):
    statement(w, accounts.riyad, [], start=date(2027, 2, 1), end=date(2027, 2, 28))
    with pytest.raises(BankingError, match="2027-01 first"):
        services.prepare_reconciliation(accounts.riyad, period(w, "2027-02"), user=w.a)


def test_r36_missing_statement_dates_are_named(w, accounts):
    statement(w, accounts.riyad, [], start=JAN1, end=date(2027, 1, 20))
    with pytest.raises(BankingError, match="2027-01-21..2027-01-31"):
        services.prepare_reconciliation(accounts.riyad, period(w, "2027-01"), user=w.a)
    with pytest.raises(BankingError, match="No statement"):
        services.prepare_reconciliation(accounts.rajhi, period(w, "2027-01"), user=w.a)


def submit_refused(rec, user, text):
    with pytest.raises(BankingError, match=text):
        services.submit_reconciliation(rec, user=user)


def test_at23_difference_missing_note_pending_document_and_changed_snapshot(w, accounts, january):
    imp, _ = january
    unmatched = imp.lines.get(line_no=2)
    rec = services.prepare_reconciliation(accounts.riyad, period(w, "2027-01"), user=w.a)
    submit_refused(rec, w.a, "need a note")
    services.add_reconciliation_note(rec, user=w.a, statement_line=unmatched, note_ar="إيداع غير معروف")
    txn = services.create_bank_transaction(accounts.riyad, user=w.a, kind="conventional_interest_credit",
                                           statement_line=unmatched)
    services.submit_bank_transaction(txn, user=w.a)
    submit_refused(rec, w.a, "bank transactions")
    services.discard_bank_transaction(txn, user=w.a, reason="Not interest")
    services.submit_reconciliation(rec, user=w.a)
    vendor_payment(w, "10.00", date(2027, 1, 31))  # a posting after preparation
    with pytest.raises(BankingError, match="re-prepare"):
        services.approve_reconciliation(rec, user=w.p)
    rec = services.prepare_reconciliation(accounts.riyad, period(w, "2027-01"), user=w.a)
    assert rec.status == "draft" and rec.difference == D("0")
    # A book movement outside the matched range (before the account's active dates) leaves a difference.
    journal(w, w.alm, CUTOVER, [{"account": "5700", "debit": D("25.00")}, {"account": "1101", "credit": D("25.00")}])
    rec = services.prepare_reconciliation(accounts.riyad, period(w, "2027-01"), user=w.a)
    assert rec.difference == D("-25.000")
    submit_refused(rec, w.a, "must be 0")


def test_at30_interest_debit_stays_outstanding_as_a_sharia_exception(w, accounts):
    imp = statement(w, accounts.riyad, [(date(2027, 1, 25), "D", "80.00", "NINT", "", "", "INTEREST CHARGED")],
                    start=JAN1, end=JAN31)
    rec = services.prepare_reconciliation(accounts.riyad, period(w, "2027-01"), user=w.a)
    item = next(i for i in rec.items if i["type"] == "bank")
    assert item["sharia_exception"] and rec.difference == D("0") and rec.unmatched_bank_debits == D("80.000")
    services.add_reconciliation_note(rec, user=w.a, statement_line=imp.lines.get(), sharia_exception=True,
                                     note_en="Interest debited by the bank; not bookable until OQ-7",
                                     note_ar="فائدة خصمها البنك؛ لا تُقيد حتى البت في OQ-7")
    services.submit_reconciliation(rec, user=w.a)
    rec = services.approve_reconciliation(rec, user=w.p)
    text = services.reconciliation_statement(rec, user=w.v)
    assert "Sharia exception / استثناء شرعي" in text


def test_at28_arabic_messages_and_bilingual_statement(w, accounts, january):
    ar = make_user("arabic_user", language="ar")
    member(ar, w.alm, "accountant")
    member(ar, w.alm, "approver")
    good = make_iban("20", "000000000000166666")
    wrong = good[:2] + ("00" if good[2:4] != "00" else "01") + good[4:]
    with pytest.raises(BankingError) as exc:
        services.create_bank_account(
            w.alm, user=ar, code="AR-1", name_en="x", name_ar="س", bank="riyad", currency="SAR",
            gl_account=w.gl.riyad, sharia_basis="islamic", statement_format="mt940",
            opening_statement_date=JAN1, opening_statement_balance=D("0"), active_from=JAN1, iban=wrong)
    assert ARABIC.search(exc.value.messages[0])
    bad = mt940(statement_id="AR", sequence=5, currency="SAR", opening="1.00", opening_date=date(2027, 2, 1),
                closing_date=date(2027, 2, 2), lines=[], account=IBANS["RIYAD-SAR"])
    with pytest.raises(BankingError) as exc:
        services.import_statement(accounts.riyad, bad, user=ar)
    assert ARABIC.search(" ".join(exc.value.messages))
    txn = services.create_bank_transaction(accounts.riyad, user=ar, kind="bank_charge", amount=D("5.00"),
                                           date=date(2027, 1, 5))
    services.submit_bank_transaction(txn, user=ar)
    with pytest.raises(access.AccessDenied) as denied:
        services.approve_bank_transaction(txn, user=ar)
    assert ARABIC.search(str(denied.value))  # the access layer answers in the user's language
    services.discard_bank_transaction(txn, user=ar, reason="test")
    rec = services.prepare_reconciliation(accounts.riyad, period(w, "2027-01"), user=ar)
    with pytest.raises(access.AccessDenied) as denied:
        services.approve_reconciliation(rec, user=ar)
    assert ARABIC.search(str(denied.value))
    text = services.reconciliation_statement(rec, user=w.a)
    for heading in ("Bank reconciliation", "التسوية البنكية", "Riyad Bank", "بنك الرياض", "Balance per books",
                    "الرصيد حسب الدفاتر"):
        assert heading in text
    assert IBANS["RIYAD-SAR"] not in text and IBANS["RIYAD-SAR"][-4:] in text
