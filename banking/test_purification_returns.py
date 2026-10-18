"""Owner decision C5 (2026-10-18; banking R25, R27 note, B3a): the purification account should not earn a return,
and any return credited on it goes to charity. Islamic profit credited on the purification account is booked
Dr 1140 / Cr 2600 (payable to charity), never 4600; conventional interest stays B4 (Dr bank / Cr 2600). Profit on an
operating Islamic account is unchanged (B3, Cr 4600). Synthetic IBANs, made-up amounts.
"""
from datetime import date
from decimal import Decimal as D

import pytest

from banking import services
from banking.models import BankingError, BankTransaction
from banking.test_purification_account import posted_txn, purification_draft
from banking.test_purification_restrictions import bal
from ledger import documents

pytestmark = pytest.mark.django_db


def lines_of(txn):
    txn.refresh_from_db()
    return sorted((l.account.code, l.debit, l.credit) for l in txn.journal_entry.lines.select_related("account"))


def test_c5_profit_on_the_purification_account_is_payable_to_charity_not_income(w, accounts):
    pur = services.approve_bank_account(purification_draft(w), user=w.p)
    txn = posted_txn(w, pur, "islamic_profit", "40.00", date(2027, 1, 20))
    assert lines_of(txn) == [("1140", D("40.00"), D("0")), ("2600", D("0"), D("40.00"))]
    assert txn.requires_purification is True
    assert "to charity" in txn.description_en and "للجهات الخيرية" in txn.description_ar
    assert bal(w, "4600") == D("0") and bal(w, "2600") == D("-40.00") and bal(w, "1140") == D("40.00")
    report = services.purification_report(w.alm, user=w.p)  # listed for purification (R28, C5)
    assert [r["number"] for r in report["lines"]] == [txn.number]


def test_c5_conventional_interest_on_the_purification_account_stays_payable_to_charity(w, accounts):
    pur = services.approve_bank_account(purification_draft(w), user=w.p)
    txn = posted_txn(w, pur, "conventional_interest_credit", "15.00", date(2027, 1, 20))
    assert lines_of(txn) == [("1140", D("15.00"), D("0")), ("2600", D("0"), D("15.00"))]
    assert txn.requires_purification is True


def test_c5_profit_on_an_operating_islamic_account_is_still_income(w, accounts):
    txn = posted_txn(w, accounts.rajhi, "islamic_profit", "1200.00", date(2027, 1, 20))  # B3 unchanged
    codes = [code for code, _, _ in lines_of(txn)]
    assert "4600" in codes and "2600" not in codes and txn.requires_purification is False


def test_c5_validate_lines_refuses_income_for_a_return_on_the_purification_account(w, accounts):
    pur = services.approve_bank_account(purification_draft(w), user=w.p)
    txn = services.create_bank_transaction(pur, user=w.a, kind="islamic_profit", amount=D("40.00"),
                                           date=date(2027, 1, 20))
    services.submit_bank_transaction(txn, user=w.a)

    def old_b3_lines(document):  # the pre-C5 booking: to 4600 income
        return [{"account": "1140", "debit": D("40.00")}, {"account": "4600", "credit": D("40.00")}]

    with documents._registered_for_tests(BankTransaction, post_permissions={"banking.approve_bank_transaction"},
                                         reverse_permissions={"banking.reverse_bank_transaction"},
                                         build_lines=old_b3_lines,
                                         validate_lines=services._check_bank_transaction_lines):
        with pytest.raises(BankingError) as exc:
            services.approve_bank_transaction(txn, user=w.p)
    text = " ".join(exc.value.messages)
    assert exc.value.code == "document_lines_rejected"
    assert "never income" in text and "credited to 2600" in text and "للجهات الخيرية" in text
    txn.refresh_from_db()
    assert txn.status == "submitted" and txn.journal_entry_id is None
