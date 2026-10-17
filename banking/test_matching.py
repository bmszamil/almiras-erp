"""Matching (banking R16 to R24; AT-11 to AT-14, AT-29). Matching never creates or posts anything."""
import ast
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from banking import services
from banking.conftest import doc_bank_line, journal, receipt, statement, vendor_payment
from banking.models import BankingError, BankTransaction, Match, StatementLine
from ledger.models import JournalEntry

JAN1, JAN31 = date(2027, 1, 1), date(2027, 1, 31)


def jan(w, account, lines, **kwargs):
    return statement(w, account, lines, start=JAN1, end=JAN31, **kwargs)


def line_of(imp, no):
    return imp.lines.get(line_no=no)


def test_at11_exact_amount_and_reference_auto_confirms_m1(w, accounts):
    rec = receipt(w, "11475.00", date(2027, 1, 14), bank_reference="TRX88")
    entries = JournalEntry.objects.count()
    imp = jan(w, accounts.riyad, [(date(2027, 1, 15), "C", "11475.00", "NTRF", "", "TRX88", "Incoming transfer")])
    match = Match.objects.get(bank_account=accounts.riyad)
    assert (match.rule, match.confidence, match.status, match.auto, match.confirmed_by) == ("M1", 100, "confirmed",
                                                                                            True, w.a)
    assert {p.journal_line_id for p in match.parts.all()} - {None} == {doc_bank_line(rec).pk}
    assert services.statement_line_state(line_of(imp, 1), user=w.a) == "matched"
    assert JournalEntry.objects.count() == entries and not BankTransaction.objects.exists()  # R16, R24


def test_m1_document_number_in_the_narrative(w, accounts):
    rec = receipt(w, "700.00", date(2027, 1, 14))
    jan(w, accounts.riyad, [(date(2027, 1, 18), "C", "700.00", "NTRF", "", "", f"Payment {rec.number} thanks")])
    match = Match.objects.get(bank_account=accounts.riyad)
    assert (match.rule, match.status) == ("M1", "confirmed")


def test_m1_reference_must_be_a_whole_token(w, accounts):
    receipt(w, "700.00", date(2027, 1, 14), bank_reference="TRX88")
    jan(w, accounts.riyad, [(date(2027, 1, 30), "C", "700.00", "NTRF", "", "TRX888", "")])  # 16 days, no token
    assert not Match.objects.filter(bank_account=accounts.riyad).exists()


def test_at12_two_candidates_are_proposed_and_confirming_one_rejects_the_other(w, accounts):
    one = receipt(w, "5000.00", date(2027, 1, 10), bank_reference="INV-ALM-2027-000040")
    two = receipt(w, "5000.00", date(2027, 1, 11), bank_reference="INV-ALM-2027-000040")
    jan(w, accounts.riyad, [(date(2027, 1, 12), "C", "5000.00", "NTRF", "", "", "Settles INV-ALM-2027-000040")])
    proposals = list(Match.objects.filter(bank_account=accounts.riyad))
    assert len(proposals) == 2 and {(m.rule, m.status, m.confidence) for m in proposals} == {("M1", "proposed", 100)}
    chosen = next(m for m in proposals if doc_bank_line(one).pk in {p.journal_line_id for p in m.parts.all()})
    services.confirm_match(chosen, user=w.a)
    other = next(m for m in proposals if m.pk != chosen.pk)
    other.refresh_from_db()
    assert other.status == "rejected" and other.rejected_by == w.a  # amount consumed
    assert doc_bank_line(two).pk in {p.journal_line_id for p in other.parts.all()}


def test_at13_amount_and_date_window_is_only_proposed(w, accounts):
    pay = vendor_payment(w, "8000.00", date(2027, 1, 10))
    imp = jan(w, accounts.riyad, [(date(2027, 1, 12), "D", "8000.00", "NTRF", "", "", "Transfer"),
                                  (date(2027, 1, 20), "D", "8000.00", "NTRF", "", "", "Transfer")])
    match = Match.objects.get(bank_account=accounts.riyad)
    assert (match.rule, match.confidence, match.status) == ("M2", 70, "proposed")
    assert {p.statement_line_id for p in match.parts.all()} - {None} == {line_of(imp, 1).pk}
    assert services.statement_line_state(line_of(imp, 2), user=w.a) == "unmatched"
    services.confirm_match(match, user=w.a)
    assert services.statement_line_state(line_of(imp, 1), user=w.a) == "matched"
    assert doc_bank_line(pay).pk in {p.journal_line_id for p in match.parts.all()}


def test_m2_several_candidates_get_confidence_40(w, accounts):
    journal(w, w.alm, date(2027, 1, 10), [{"account": "5300", "debit": D("300.00")}, {"account": "1101", "credit": D("300.00")}])
    journal(w, w.alm, date(2027, 1, 11), [{"account": "5300", "debit": D("300.00")}, {"account": "1101", "credit": D("300.00")}])
    jan(w, accounts.riyad, [(date(2027, 1, 12), "D", "300.00", "NTRF", "", "", "")])
    assert {(m.rule, m.confidence) for m in Match.objects.filter(bank_account=accounts.riyad)} == {("M2", 40)}


def test_at14_grouped_one_to_many_and_many_to_one_are_proposed_then_confirmed(w, accounts):
    receipts = [receipt(w, "10000.00", date(2027, 1, d)) for d in (5, 6, 7)]
    pay = vendor_payment(w, "50000.00", date(2027, 1, 10))
    imp = jan(w, accounts.riyad, [(date(2027, 1, 7), "C", "30000.00", "NTRF", "", "", "Batch"),
                                  (date(2027, 1, 10), "D", "30000.00", "NTRF", "", "", "Part 1"),
                                  (date(2027, 1, 11), "D", "20000.00", "NTRF", "", "", "Part 2")])
    groups = list(Match.objects.filter(bank_account=accounts.riyad, rule="M3"))
    assert len(groups) == 2 and all(m.status == "proposed" and m.confidence == 50 for m in groups)
    one_to_many = next(m for m in groups if line_of(imp, 1).pk in {p.statement_line_id for p in m.parts.all()})
    assert {p.journal_line_id for p in one_to_many.parts.all()} - {None} == {doc_bank_line(r).pk for r in receipts}
    many_to_one = next(m for m in groups if m.pk != one_to_many.pk)
    assert {p.statement_line_id for p in many_to_one.parts.all()} - {None} == {line_of(imp, 2).pk, line_of(imp, 3).pk}
    assert doc_bank_line(pay).pk in {p.journal_line_id for p in many_to_one.parts.all()}
    for m in groups:
        services.confirm_match(m, user=w.a)
    assert all(services.statement_line_state(l, user=w.a) == "matched" for l in imp.lines.all())


def approved_charge(w, account, amount, on):
    txn = services.create_bank_transaction(account, user=w.a, kind="bank_charge", amount=D(amount), date=on)
    services.submit_bank_transaction(txn, user=w.a2)
    return services.approve_bank_transaction(txn, user=w.p)


def test_at14a_manual_group_with_mixed_directions(w, accounts):
    rec = receipt(w, "10000.00", date(2027, 1, 12))
    charge = approved_charge(w, accounts.riyad, "25.00", date(2027, 1, 12))
    imp = jan(w, accounts.riyad, [(date(2027, 1, 12), "C", "9975.00", "NTRF", "", "", "Net of fees")])
    group = services.manual_match(accounts.riyad, user=w.a, statement_lines=[(line_of(imp, 1), None)],
                                  journal_lines=[(doc_bank_line(rec), None), (doc_bank_line(charge), None)])
    assert (group.rule, group.status) == ("manual", "confirmed")
    assert services.statement_line_state(line_of(imp, 1), user=w.a) == "matched"


def test_at14b_partial_matches_then_the_remainder(w, accounts):
    ten = receipt(w, "10000.00", date(2027, 1, 18))
    five = receipt(w, "5000.00", date(2027, 1, 22))
    imp = jan(w, accounts.riyad, [(date(2027, 1, 20), "C", "15000.00", "NTRF", "", "", "Two receipts")])
    line = line_of(imp, 1)
    services.manual_match(accounts.riyad, user=w.a, statement_lines=[(line, D("10000.00"))],
                          journal_lines=[(doc_bank_line(ten), None)])
    assert services.statement_line_state(line, user=w.a) == "partly_matched"
    assert not Match.objects.filter(bank_account=accounts.riyad, status="proposed").exists()  # M3 no longer possible
    services.manual_match(accounts.riyad, user=w.a, statement_lines=[(line, None)], journal_lines=[(doc_bank_line(five), None)])
    assert services.statement_line_state(line, user=w.a) == "matched"


def test_at14c_d_unbalanced_groups_and_parts_above_the_open_amount_are_refused(w, accounts):
    rec = receipt(w, "10000.00", date(2027, 1, 12))
    imp = jan(w, accounts.riyad, [(date(2027, 1, 25), "C", "9975.00", "NTRF", "", "", "")])
    with pytest.raises(BankingError, match="does not balance"):
        services.manual_match(accounts.riyad, user=w.a, statement_lines=[(line_of(imp, 1), None)],
                              journal_lines=[(doc_bank_line(rec), None)])
    with pytest.raises(BankingError, match="open amount"):
        services.manual_match(accounts.riyad, user=w.a, statement_lines=[(line_of(imp, 1), D("10000.00"))],
                              journal_lines=[(doc_bank_line(rec), D("10000.00"))])
    assert not Match.objects.filter(bank_account=accounts.riyad, status="confirmed").exists()


def test_undo_needs_a_reason_and_frees_the_amounts(w, accounts):
    receipt(w, "11475.00", date(2027, 1, 14), bank_reference="TRX88")
    imp = jan(w, accounts.riyad, [(date(2027, 1, 15), "C", "11475.00", "NTRF", "", "TRX88", "")])
    match = Match.objects.get(bank_account=accounts.riyad)
    with pytest.raises(BankingError, match="reason"):
        services.undo_match(match, user=w.a, reason=" ")
    services.undo_match(match, user=w.a, reason="Wrong receipt")
    assert services.statement_line_state(line_of(imp, 1), user=w.a) == "unmatched"
    match.refresh_from_db()
    assert match.status == "undone" and match.undone_by == w.a


def test_rejected_pairs_are_not_proposed_again(w, accounts):
    vendor_payment(w, "8000.00", date(2027, 1, 10))
    jan(w, accounts.riyad, [(date(2027, 1, 12), "D", "8000.00", "NTRF", "", "", "")])
    match = Match.objects.get(bank_account=accounts.riyad)
    services.reject_match(match, user=w.a, reason="Different payment")
    services.run_matching(accounts.riyad, user=w.a)
    assert Match.objects.filter(bank_account=accounts.riyad).count() == 1


def test_at29_wps_reference_on_a_salary_payment_matches_m1(w, accounts):
    """Payroll is not built: the salary payment is a posted journal with bank reference WPS-0127 (OQ-26)."""
    entry = journal(w, w.alm, date(2027, 1, 27), [{"account": "5100", "debit": D("19781.25")},
                                                  {"account": "1101", "credit": D("19781.25")}], reference="WPS-0127")
    jan(w, accounts.riyad, [(date(2027, 1, 28), "D", "19781.25", "NTRF", "", "WPS-0127", "Salaries")])
    match = Match.objects.get(bank_account=accounts.riyad)
    assert (match.rule, match.status, match.auto) == ("M1", "confirmed", True)
    assert entry.lines.get(account__code="1101").pk in {p.journal_line_id for p in match.parts.all()}


def test_m1_auto_confirm_can_be_switched_off(w, accounts):
    services.update_settings(w.alm, user=w.p, auto_confirm_m1=False)
    receipt(w, "11475.00", date(2027, 1, 14), bank_reference="TRX88")
    jan(w, accounts.riyad, [(date(2027, 1, 15), "C", "11475.00", "NTRF", "", "TRX88", "")])
    assert Match.objects.get(bank_account=accounts.riyad).status == "proposed"
    with pytest.raises(Exception):
        services.update_settings(w.alm, user=w.a, m2_date_window_days=5)  # accountants do not change settings


def test_r2_matching_another_accounts_line_is_refused(w, accounts):
    rec = receipt(w, "500.00", date(2027, 1, 5))
    imp = statement(w, accounts.rajhi, [(date(2027, 1, 5), "C", "500.00", "NTRF", "", "", "")], start=JAN1, end=JAN31)
    with pytest.raises(BankingError, match="another bank account"):
        services.manual_match(accounts.riyad, user=w.a, statement_lines=[(line_of(imp, 1), None)],
                              journal_lines=[(doc_bank_line(rec), None)])


# --- R24: the matching engine never creates documents or entries ------------------------------------------------

FORBIDDEN = {"create_entry", "post_entry", "reverse_entry", "approve_bank_transaction", "create_bank_transaction",
             "submit_bank_transaction", "BankTransaction", "JournalEntry", "JournalLine", "_create", "_post"}
ENGINE = ("run_matching", "_run_matching", "_match_after", "manual_match", "propose_match", "confirm_match",
          "reject_match", "undo_match")


def test_r24_no_entry_or_document_creation_is_reachable_from_the_matching_engine():
    tree = ast.parse((Path(services.__file__)).read_text(encoding="utf-8"))
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}

    def calls(fn):
        out = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                f = node.func
                out.add(f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else "")
        return out

    reachable, frontier = set(), list(ENGINE)
    while frontier:
        name = frontier.pop()
        if name in reachable or name not in funcs:
            continue
        reachable.add(name)
        frontier += [c for c in calls(funcs[name]) if c in funcs]
    called = set().union(*(calls(funcs[n]) for n in reachable))
    assert not (called & FORBIDDEN), called & FORBIDDEN
    assert {"_m0", "_m1", "_m2", "_m3", "_confirm"} <= reachable
