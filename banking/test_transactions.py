"""Bank transactions (banking R25 to R35; AT-15 to AT-21, AT-26, AT-27). Fake amounts only."""
from datetime import date
from decimal import Decimal as D

import pytest

from banking import services
from banking.conftest import doc_bank_line, journal, open_account, rate, receipt, statement
from banking.models import BankingError, BankingEvent, BankTransaction, Match
from core import access
from core.access.models import GrantUse
from core.access.testing import make_user, member
from ledger import documents
from ledger import services as ledger
from ledger.models import FiscalPeriod, JournalEntry, JournalLine, LedgerError

JAN1, JAN31 = date(2027, 1, 1), date(2027, 1, 31)


def jan(w, account, lines, **kwargs):
    return statement(w, account, lines, start=JAN1, end=JAN31, **kwargs)


def lines_of(txn):
    return sorted((l.account.code, l.debit, l.credit, l.currency_id, l.txn_amount, l.line_kind)
                  for l in txn.journal_entry.lines.select_related("account"))


def draft_from(w, line, kind, user=None, **kwargs):
    return services.create_bank_transaction(line.bank_account, user=user or w.a, kind=kind, statement_line=line, **kwargs)


def post(w, txn, submitter=None, approver=None):
    services.submit_bank_transaction(txn, user=submitter or w.a)
    return services.approve_bank_transaction(txn, user=approver or w.p)


def test_at15_bank_charge_posts_on_a_second_persons_approval(w, accounts):
    imp = jan(w, accounts.riyad, [(date(2027, 1, 20), "D", "25.00", "NCHG", "", "BR9", "CHARGES")])
    line = imp.lines.get()
    both = make_user("a_and_p")
    member(both, w.alm, "accountant")
    member(both, w.alm, "approver")
    txn = draft_from(w, line, "bank_charge", user=both)
    assert (txn.status, txn.source, txn.amount, txn.direction, txn.date) == ("draft", "human", D("25.00"), "out",
                                                                           date(2027, 1, 20))
    services.submit_bank_transaction(txn, user=both)
    with pytest.raises(access.AccessDenied) as exc:
        services.approve_bank_transaction(txn, user=both)
    assert exc.value.code == "self_approval"
    with pytest.raises(access.AccessDenied) as exc:
        services.approve_bank_transaction(txn, user=w.a)
    assert exc.value.code == "no_membership"
    with pytest.raises(access.AccessDenied) as exc:
        services.approve_bank_transaction(txn, user=w.g)
    assert exc.value.code == "agent_forbidden"
    posted = services.approve_bank_transaction(txn, user=w.p)
    assert (posted.status, posted.number, posted.posted_by, posted.approved_by) == (
        "posted", "ALM-BTX-2027-000001", w.p, w.p)
    entry = posted.journal_entry
    assert (entry.status, entry.document_type, entry.document_id, entry.posted_by) == (
        "posted", "banking.banktransaction", posted.pk, w.p)
    assert lines_of(posted) == [("1101", D("0"), D("25.00"), "SAR", D("25.000"), "normal"),
                                ("5700", D("25.00"), D("0"), "SAR", D("25.000"), "normal")]
    match = Match.objects.get(bank_account=accounts.riyad, status="confirmed")  # R20: the line is matched (M1)
    assert match.rule == "M1" and match.auto and match.confirmed_by == both
    assert services.statement_line_state(line, user=w.a) == "matched"
    with pytest.raises(BankingError, match="submitted"):  # a document books once
        services.approve_bank_transaction(posted, user=w.p)
    grant = access.require(w.p, w.alm, "banking.approve_bank_transaction", document=posted)
    with pytest.raises(LedgerError) as exc:
        ledger.create_entry(w.alm, posted.date, "again", None, user=w.p, post=True, grant=grant, document=posted)
    assert exc.value.code == "document_booked"
    assert JournalEntry.objects.filter(document_type="banking.banktransaction", document_id=posted.pk).count() == 1


def test_r25_build_lines_only_runs_inside_the_approval(w, accounts):
    txn = services.create_bank_transaction(accounts.riyad, user=w.a, kind="bank_charge", amount=D("5.00"),
                                           date=date(2027, 1, 5))
    with pytest.raises(BankingError, match="approve"):
        services._bank_transaction_lines(txn)
    services.submit_bank_transaction(txn, user=w.a)
    grant = access.require(w.p, w.alm, "banking.approve_bank_transaction", document=txn)
    with pytest.raises(BankingError):  # posting outside banking.services is refused by the document's hook
        ledger.create_entry(w.alm, txn.date, "direct", None, user=w.p, post=True, grant=grant, document=txn)
    assert not JournalEntry.objects.filter(document_type="banking.banktransaction").exists()


def test_at16_islamic_profit_only_on_an_islamic_account(w, accounts):
    rajhi = jan(w, accounts.rajhi, [(date(2027, 1, 31), "C", "1200.00", "NTRF", "", "", "PROFIT")]).lines.get()
    riyad = jan(w, accounts.riyad, [(date(2027, 1, 31), "C", "1200.00", "NTRF", "", "", "PROFIT")]).lines.get()
    txn = post(w, draft_from(w, rajhi, "islamic_profit"))
    assert lines_of(txn) == [("1103", D("1200.00"), D("0"), "SAR", D("1200.000"), "normal"),
                             ("4600", D("0"), D("1200.00"), "SAR", D("1200.000"), "normal")]
    with pytest.raises(BankingError, match="conventional_interest_credit"):
        draft_from(w, riyad, "islamic_profit")


def nsh_account(w):
    return open_account(w, w.nsh, "RIYAD-SAR", "riyad", w.gl.nsh_riyad, "SAR", "conventional", opening="0.00")


def test_at17_conventional_interest_is_never_income(w, accounts):
    line = jan(w, accounts.riyad, [(date(2027, 1, 31), "C", "300.00", "NINT", "", "", "INTEREST")]).lines.get()
    with pytest.raises(BankingError, match="interest"):
        draft_from(w, line, "islamic_profit")
    assert services.available_kinds(line, user=w.a)[0] == "conventional_interest_credit"
    txn = post(w, draft_from(w, line, "conventional_interest_credit"))
    assert txn.requires_purification
    assert lines_of(txn) == [("1101", D("300.00"), D("0"), "SAR", D("300.000"), "normal"),
                             ("2600", D("0"), D("300.00"), "SAR", D("300.000"), "normal")]
    assert not any(l.account.type == "income" for l in txn.journal_entry.lines.all())
    report = services.purification_report(w.alm, user=w.a)
    assert [r["number"] for r in report["lines"]] == [txn.number] and report["total_by_currency"] == {"SAR": D("300.00")}
    # NSH does not enforce Sharia: the same treatment (OQ-6 default).
    nsh = nsh_account(w)
    nline = statement(w, nsh, [(date(2027, 1, 31), "C", "300.00", "NINT", "", "", "INTEREST")], start=JAN1,
                      end=JAN31).lines.get()
    ntxn = post(w, draft_from(w, nline, "conventional_interest_credit", user=w.n), w.n, w.pn)
    assert lines_of(ntxn) == [("1101", D("300.00"), D("0"), "SAR", D("300.000"), "normal"),
                              ("2600", D("0"), D("300.00"), "SAR", D("300.000"), "normal")]


def test_at17_validate_lines_refuses_an_income_line(w, accounts):
    txn = services.create_bank_transaction(accounts.riyad, user=w.a, kind="conventional_interest_credit",
                                           amount=D("300.00"), date=date(2027, 1, 31))
    services.submit_bank_transaction(txn, user=w.a)

    def income_lines(document):
        return [{"account": "1101", "debit": D("300.00")}, {"account": "4900", "credit": D("300.00")}]

    with documents._registered_for_tests(BankTransaction, post_permissions={"banking.approve_bank_transaction"},
                                         reverse_permissions={"banking.reverse_bank_transaction"},
                                         build_lines=income_lines,
                                         validate_lines=services._check_bank_transaction_lines):
        with pytest.raises(BankingError) as exc:
            services.approve_bank_transaction(txn, user=w.p)
    assert exc.value.code == "document_lines_rejected" and "never income" in " ".join(exc.value.messages)
    txn.refresh_from_db()
    assert txn.status == "submitted" and txn.number == "" and txn.journal_entry_id is None


def test_at18_foreign_currency_charges_transfers_and_decimals(w, accounts):
    rate(w, "USD", date(2027, 1, 15), "3.7500")
    rate(w, "USD", date(2027, 1, 20), "3.7600")
    usd = jan(w, accounts.snb, [(date(2027, 1, 15), "D", "15.00", "NCHG", "", "", "CHARGES"),
                                (date(2027, 1, 20), "C", "10000.00", "NTRF", "", "", "From Riyad")])
    charge = post(w, draft_from(w, usd.lines.get(line_no=1), "bank_charge"))  # B2
    assert lines_of(charge) == [("1102", D("0"), D("56.25"), "USD", D("15.000"), "normal"),
                                ("5700", D("56.25"), D("0"), "USD", D("15.000"), "normal")]
    assert charge.fx_exchange_rate.rate == D("3.7500")
    with pytest.raises(BankingError, match="same date"):  # R31: spot only
        services.create_bank_transaction(accounts.riyad, user=w.a, kind="own_transfer", amount=D("37510.00"),
                                         date=date(2027, 1, 20), direction="out", to_bank_account=accounts.snb,
                                         to_amount=D("10000.00"), to_date=date(2027, 1, 21))
    with pytest.raises(access.AccessDenied):  # the actual bank rate is a manual rate: agents never draft it
        services.create_bank_transaction(accounts.riyad, user=w.g, kind="own_transfer", amount=D("37510.00"),
                                         date=date(2027, 1, 20), direction="out", to_bank_account=accounts.snb,
                                         to_amount=D("10000.00"))
    transfer = post(w, services.create_bank_transaction(
        accounts.riyad, user=w.a, kind="own_transfer", amount=D("37510.00"), date=date(2027, 1, 20), direction="out",
        to_bank_account=accounts.snb, to_amount=D("10000.00")))  # B6
    assert lines_of(transfer) == [("1101", D("0"), D("37510.00"), "SAR", D("37510.000"), "normal"),
                                  ("1102", D("37510.00"), D("0"), "USD", D("10000.000"), "normal")]
    usd_leg = transfer.journal_entry.lines.get(account__code="1102")
    assert (usd_leg.rate_source, usd_leg.fx_rate, usd_leg.manual_rate_reason) == (
        "manual", D("3.7510000000"), ledger.ACTUAL_BANK_RATE)
    proposal = Match.objects.get(bank_account=accounts.snb, status="proposed")  # the USD credit (M2)
    services.confirm_match(proposal, user=w.a)
    # No USD rate within 5 days of 03-10: approval fails, nothing posts, the grant stays unused (R34).
    late = services.create_bank_transaction(accounts.snb, user=w.a, kind="bank_charge", amount=D("15.00"),
                                            date=date(2027, 3, 10))
    services.submit_bank_transaction(late, user=w.a)
    uses = GrantUse.objects.count()
    with pytest.raises(BankingError, match="exchange rate"):
        services.approve_bank_transaction(late, user=w.p)
    late.refresh_from_db()
    assert (late.status, late.number, late.journal_entry_id, GrantUse.objects.count()) == ("submitted", "", None, uses)
    # KWD has 3 decimals.
    rate(w, "KWD", date(2027, 1, 18), "12.2000")
    kwd = post(w, services.create_bank_transaction(accounts.kwd, user=w.a, kind="bank_charge", amount=D("1.234"),
                                                   date=date(2027, 1, 18)))
    assert lines_of(kwd)[0][:5] == ("1104", D("0"), D("15.05"), "KWD", D("1.234"))
    with pytest.raises(BankingError):
        services.create_bank_transaction(accounts.kwd, user=w.a, kind="bank_charge", amount=D("1.2345"),
                                         date=date(2027, 1, 18))
    with pytest.raises(BankingError, match="decimals"):
        jan(w, accounts.kwd, [(date(2027, 1, 18), "D", "1.2345", "NCHG", "", "", "")])
    # The USD reconciliation shows USD figures and the SAR book balance (R42).
    rec = services.prepare_reconciliation(accounts.snb, FiscalPeriod.objects.get(entity=w.alm, name="2027-01"), user=w.a)
    assert (rec.bank_balance, rec.book_balance, rec.book_balance_functional, rec.difference) == (
        D("9985.000"), D("9985.000"), D("37453.75"), D("0"))


def test_at19_intercompany_settlement_books_realised_fx(w, accounts):
    rate(w, "USD", date(2027, 1, 15), "3.7500")
    rate(w, "USD", date(2027, 1, 20), "3.7600")
    owed = journal(w, w.alm, date(2027, 1, 15), [
        {"account": "5300", "currency": "USD", "txn_debit": D("10000.00")},
        {"account": "2110", "currency": "USD", "txn_credit": D("10000.00"), "counterparty": w.sub},
    ])
    item = owed.lines.get(account__code="2110")
    assert item.credit == D("37500.00")
    txn = post(w, services.create_bank_transaction(
        accounts.snb, user=w.a, kind="intercompany_transfer", amount=D("10000.00"), date=date(2027, 1, 20),
        direction="out", counterparty_entity=w.sub, intercompany_account="2110", settles_line=item))
    assert lines_of(txn) == [("1102", D("0"), D("37600.00"), "USD", D("10000.000"), "normal"),
                             ("2110", D("37500.00"), D("0"), "USD", D("10000.000"), "settlement"),
                             ("5750", D("100.00"), D("0"), "SAR", D("100.000"), "realised_fx")]
    assert txn.journal_entry.lines.get(account__code="2110").counterparty_entity == w.sub
    assert ledger._open_item(JournalLine.objects.get(pk=item.pk)) == (D("0"), D("0"))


def test_at20_intercompany_transfer_needs_a_group_counterparty_and_books_in_one_entity(w, accounts):
    line = jan(w, accounts.riyad, [(date(2027, 1, 10), "D", "100000.00", "NTRF", "", "", "Loan to SUB")]).lines.get()
    for counterparty, text in ((None, "counterparty"), (w.alm, "not another active entity"),
                               (w.other, "not another active entity")):
        with pytest.raises(BankingError, match=text):
            draft_from(w, line, "intercompany_transfer", counterparty_entity=counterparty, intercompany_account="1220")
    sub_entries = JournalEntry.objects.filter(entity=w.sub).count()
    txn = post(w, draft_from(w, line, "intercompany_transfer", counterparty_entity=w.sub, intercompany_account="1220"))
    assert lines_of(txn) == [("1101", D("0"), D("100000.00"), "SAR", D("100000.000"), "normal"),
                             ("1220", D("100000.00"), D("0"), "SAR", D("100000.000"), "normal")]
    assert JournalEntry.objects.filter(entity=w.sub).count() == sub_entries  # no automatic mirror (OQ-25)
    sub_line = statement(w, accounts.sub, [(date(2027, 1, 10), "C", "100000.00", "NTRF", "", "", "Loan from ALM")],
                         start=JAN1, end=JAN31).lines.get()
    mirror = post(w, draft_from(w, sub_line, "intercompany_transfer", user=w.s, counterparty_entity=w.alm,
                                intercompany_account="2110"), w.s, w.ps)
    assert lines_of(mirror) == [("1101", D("100000.00"), D("0"), "SAR", D("100000.000"), "normal"),
                                ("2110", D("0"), D("100000.00"), "SAR", D("100000.000"), "normal")]
    balances = ledger.intercompany_balances(JAN31, user=w.mp, group=w.alm, by_currency=True)
    assert balances[("ALM", "SUB", "SAR")] == D("100000.000") and balances[("SUB", "ALM", "SAR")] == D("-100000.000")


def test_at21_own_transfer_books_once_and_both_lines_match(w, accounts):
    out = jan(w, accounts.riyad, [(date(2027, 1, 10), "D", "50000.00", "NTRF", "", "", "To Rajhi")]).lines.get()
    txn = post(w, draft_from(w, out, "own_transfer", to_bank_account=accounts.rajhi))
    assert lines_of(txn) == [("1101", D("0"), D("50000.00"), "SAR", D("50000.000"), "normal"),
                             ("1103", D("50000.00"), D("0"), "SAR", D("50000.000"), "normal")]
    assert services.statement_line_state(out, user=w.a) == "matched"
    incoming = jan(w, accounts.rajhi, [(date(2027, 1, 12), "C", "50000.00", "NTRF", "", "", f"From Riyad {txn.number}")])
    match = Match.objects.get(bank_account=accounts.rajhi)
    assert match.status == "confirmed" and match.rule == "M1"  # by document number
    assert {p.journal_line_id for p in match.parts.all()} - {None} == {doc_bank_line(txn, "1103").pk}
    assert services.statement_line_state(incoming.lines.get(), user=w.a) == "matched"
    assert BankTransaction.objects.filter(kind="own_transfer").count() == 1
    with pytest.raises(BankingError, match="intercompany transfer"):
        services.create_bank_transaction(accounts.riyad, user=w.a, kind="own_transfer", amount=D("10.00"),
                                         date=date(2027, 1, 11), direction="out", to_bank_account=accounts.sub)


def test_at26_closed_period_and_pending_documents(w, accounts):
    period_jan = FiscalPeriod.objects.get(entity=w.alm, name="2027-01")
    feb = services.create_bank_transaction(accounts.riyad, user=w.a, kind="bank_charge", amount=D("10.00"),
                                           date=date(2027, 2, 10))
    services.submit_bank_transaction(feb, user=w.a)
    ledger.close_period(period_jan, user=w.p)
    jan_doc = services.create_bank_transaction(accounts.riyad, user=w.a, kind="bank_charge", amount=D("10.00"),
                                               date=date(2027, 1, 25))
    services.submit_bank_transaction(jan_doc, user=w.a)
    uses = GrantUse.objects.count()
    with pytest.raises(BankingError, match="closed"):
        services.approve_bank_transaction(jan_doc, user=w.p)
    jan_doc.refresh_from_db()
    assert jan_doc.status == "submitted" and GrantUse.objects.count() == uses
    with pytest.raises(LedgerError) as exc:
        ledger.close_period(FiscalPeriod.objects.get(entity=w.alm, name="2027-02"), user=w.p)
    assert exc.value.code == "documents_pending"


def test_at27_reversal_needs_the_match_undone_then_m0_pairs_reversals(w, accounts):
    imp = jan(w, accounts.riyad, [(date(2027, 1, 20), "D", "25.00", "NCHG", "", "", "CHARGES")])
    charge = post(w, draft_from(w, imp.lines.get(), "bank_charge"))
    rec = receipt(w, "800.00", date(2027, 1, 22))
    with pytest.raises(BankingError, match="undo the match"):
        services.reverse_bank_transaction(charge, user=w.p, reason="Charge refunded")
    services.undo_match(Match.objects.get(bank_account=accounts.riyad, status="confirmed"), user=w.a,
                        reason="Charge refunded by the bank")
    with pytest.raises(access.AccessDenied):  # accountants do not reverse bank transactions (OQ-19)
        services.reverse_bank_transaction(charge, user=w.a, reason="x")
    reversed_ = services.reverse_bank_transaction(charge, user=w.p, reason="Charge refunded")
    assert reversed_.status == "reversed" and reversed_.reversal_entry.reversal_of_id == charge.journal_entry_id
    from invoicing import services as inv

    inv.reverse_document(rec, user=w.a, reason="Bounced")
    services.run_matching(accounts.riyad, user=w.a)
    m0 = list(Match.objects.filter(bank_account=accounts.riyad, rule="M0"))
    assert len(m0) == 2 and all(m.status == "confirmed" and m.confidence == 100 for m in m0)
    paired = [{p.journal_line.entry_id for p in m.parts.all()} for m in m0]
    assert {charge.journal_entry_id, reversed_.reversal_entry_id} in paired
    assert BankingEvent.objects.filter(event="bank_transaction_reversed", object_id=charge.pk).exists()


def test_r35_draft_is_discarded_and_kept(w, accounts):
    txn = services.create_bank_transaction(accounts.riyad, user=w.a, kind="bank_charge", amount=D("5.00"),
                                           date=date(2027, 1, 5))
    with pytest.raises(BankingError, match="reason"):
        services.discard_bank_transaction(txn, user=w.a, reason="")
    services.discard_bank_transaction(txn, user=w.a, reason="Entered twice")
    txn.refresh_from_db()
    assert txn.status == "discarded" and BankTransaction.objects.filter(pk=txn.pk).exists()
    with pytest.raises(BankingError):
        services.submit_bank_transaction(txn, user=w.a)


def test_r25_editing_after_submission_is_refused_and_editor_cannot_approve(w, accounts):
    txn = services.create_bank_transaction(accounts.riyad, user=w.a, kind="bank_charge", amount=D("5.00"),
                                           date=date(2027, 1, 5))
    services.update_bank_transaction(txn, user=w.a2, amount=D("6.00"))
    txn.refresh_from_db()
    assert txn.amount == D("6.00") and txn.modified_by == w.a2
    services.submit_bank_transaction(txn, user=w.a2)
    with pytest.raises(BankingError, match="draft"):
        services.update_bank_transaction(txn, user=w.a, amount=D("7.00"))


def test_at30_no_kind_for_a_conventional_interest_debit(w, accounts):
    line = jan(w, accounts.riyad, [(date(2027, 1, 25), "D", "80.00", "NINT", "", "", "INTEREST CHARGED")]).lines.get()
    assert services.available_kinds(line, user=w.a) == []
    with pytest.raises(BankingError, match="Sharia exception"):
        draft_from(w, line, "bank_charge")
    entry = ledger.create_entry(w.alm, date(2027, 1, 25), "Interest", [{"account": "5610", "debit": D("80.00")},
                                                                       {"account": "1101", "credit": D("80.00")}],
                                user=w.a)
    with pytest.raises(LedgerError, match="Sharia-prohibited"):
        ledger.post_entry(entry, access.require(w.p, w.alm, "ledger.post_entry", document=entry))
    entry.delete()
