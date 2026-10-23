"""Banking R32 note B11(b) / invoicing R97 (policy A) and AT-115, banking side: until OQ-27 (= invoicing OQ-66) is
answered, ``BankTransaction(kind=intercompany_transfer)`` is on the registry allow-list for 1210 / 2110; no other
bank transaction kind, and no manual journal, posts there."""
from datetime import date
from decimal import Decimal as D

import pytest

from banking import services
from banking.conftest import statement
from banking.models import BankTransaction
from ledger import documents
from ledger import services as ledger
from ledger.models import LedgerError

JAN1, JAN31 = date(2027, 1, 1), date(2027, 1, 31)


def post(txn, submitter, approver):
    services.submit_bank_transaction(txn, user=submitter)
    return services.approve_bank_transaction(txn, user=approver)


def test_at115_banking_intercompany_transfer_on_2110_posts(w, accounts):
    line = statement(w, accounts.sub, [(date(2027, 1, 10), "C", "2500.00", "NTRF", "", "", "From ALM")],
                     start=JAN1, end=JAN31).lines.get()
    txn = services.create_bank_transaction(line.bank_account, user=w.s, kind="intercompany_transfer",
                                           statement_line=line, counterparty_entity=w.alm, intercompany_account="2110")
    txn = post(txn, w.s, w.ps)
    item = txn.journal_entry.lines.get(account__code="2110")
    assert (txn.status, item.credit, item.counterparty_entity) == ("posted", D("2500.00"), w.alm)


def test_r97_only_the_intercompany_transfer_kind_is_on_the_allow_list(w, accounts):
    bank_type = documents.get("banking.banktransaction")
    assert bank_type.control_codes() == {"1210", "2110"}  # never 1200 / 2100; 1220 is not a control account
    for kind in BankTransaction.Kind.values:
        txn = BankTransaction(kind=kind)
        assert bank_type.may_post_control("2110", txn) is (kind == "intercompany_transfer")
        assert bank_type.may_post_control("1200", txn) is False


def test_r97_manual_journal_on_2110_stays_refused(w, accounts):
    with pytest.raises(LedgerError) as exc:
        ledger.create_entry(w.alm, date(2027, 1, 10), "Manual IC", [
            {"account": "1100", "debit": D("10.00")}, {"account": "2110", "credit": D("10.00"), "counterparty": w.sub},
        ], user=w.a)
    assert exc.value.code == "control_account"
