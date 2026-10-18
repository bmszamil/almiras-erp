"""The purification account in banking (invoicing R54 amendment D31b, review fixes 1 and 2; banking R25 to R33).

Besides invoicing's purification fundings and payouts, only bank charges, Islamic profit and conventional interest
credited touch the purification account's GL 1140: no transfer between own accounts or intercompany transfer out of
it or into it, no manual journal, never an agent. A bank charge never takes the account below zero. Ported from the
reviewer's probes (``test_zz_review_probe.py``). Synthetic IBANs, made-up amounts.
"""
import re
from datetime import date
from decimal import Decimal as D

import pytest
from django.db import DatabaseError, connection, transaction

from banking import services
from banking.conftest import journal, postgres_only, statement
from banking.models import BankingError, BankTransaction
from banking.test_purification_account import posted_txn, purification_draft
from core import access
from ledger import services as ledger
from ledger.models import JournalLine, LedgerError

pytestmark = pytest.mark.django_db
ARABIC = re.compile(r"[؀-ۿ]")
FEB1 = date(2027, 2, 1)


def bal(w, code):
    lines = JournalLine.objects.filter(entry__entity=w.alm, account__code=code, entry__status__in=["posted", "reversed"])
    return sum((l.debit - l.credit for l in lines), D("0"))


def funded(w, amount="500.00"):
    """The purification account holding ``amount`` (conventional interest credited on it: Dr 1140 / Cr 2600)."""
    pur = services.approve_bank_account(purification_draft(w), user=w.p)
    posted_txn(w, pur, "conventional_interest_credit", amount, date(2027, 1, 15))
    return pur


def refused(fn, *args, code="purification_account_restricted", **kwargs):
    with pytest.raises(BankingError) as exc:
        fn(*args, **kwargs)
    text = " ".join(exc.value.messages)
    assert exc.value.code == code, (exc.value.code, text)
    assert ARABIC.search(text), text
    return text


def test_own_transfer_out_of_the_purification_account_is_refused(w, accounts):
    pur = funded(w)
    text = refused(services.create_bank_transaction, pur, user=w.a, kind="own_transfer", amount=D("500.00"),
                   date=FEB1, direction="out", to_bank_account=accounts.riyad)
    assert "only through invoicing" in text
    assert bal(w, "1140") == D("500.00")


def test_own_transfer_into_the_purification_account_is_refused_fundings_go_through_invoicing(w, accounts):
    pur = funded(w)
    for direction in ("out", "in"):
        refused(services.create_bank_transaction, accounts.riyad, user=w.a, kind="own_transfer", amount=D("100.00"),
                date=FEB1, direction=direction, to_bank_account=pur)
    assert bal(w, "1140") == D("500.00")


def test_intercompany_transfer_out_of_the_purification_account_is_refused(w, accounts):
    pur = funded(w)
    for direction in ("out", "in"):
        refused(services.create_bank_transaction, pur, user=w.a, kind="intercompany_transfer", amount=D("200.00"),
                date=FEB1, direction=direction, counterparty_entity="SUB", intercompany_account="2110")
    assert bal(w, "1140") == D("500.00")


def test_a_draft_changed_into_a_transfer_is_refused(w, accounts):
    pur = funded(w)
    txn = services.create_bank_transaction(pur, user=w.a, kind="bank_charge", amount=D("10.00"), date=FEB1)
    refused(services.update_bank_transaction, txn, user=w.a, kind="own_transfer", direction="out",
            to_bank_account=accounts.riyad)
    assert BankTransaction.objects.get(pk=txn.pk).kind == "bank_charge"


def test_agents_never_draft_on_the_purification_account(w, accounts):
    pur = funded(w)
    for kind, extra in (("own_transfer", {"direction": "out", "to_bank_account": accounts.riyad}),
                        ("bank_charge", {})):
        with pytest.raises(access.AccessDenied) as exc:
            services.create_bank_transaction(pur, user=w.g, kind=kind, amount=D("10.00"), date=FEB1, **extra)
        assert exc.value.code == "agent_forbidden"
    with pytest.raises(access.AccessDenied):  # nor transfers into it
        services.create_bank_transaction(accounts.riyad, user=w.g, kind="own_transfer", amount=D("10.00"), date=FEB1,
                                         direction="out", to_bank_account=pur)
    assert not BankTransaction.objects.filter(source__startswith="agent").exists()
    # on other accounts the agent still drafts (R43)
    assert services.create_bank_transaction(accounts.riyad, user=w.g, kind="bank_charge", amount=D("10.00"),
                                            date=FEB1).source.startswith("agent")


def test_bank_charges_profit_and_interest_are_allowed_on_the_purification_account(w, accounts):
    pur = funded(w, "100.00")
    posted_txn(w, pur, "islamic_profit", "20.00", date(2027, 1, 20))  # C5: a return on it goes to charity (2600)
    charge = posted_txn(w, pur, "bank_charge", "30.00", date(2027, 1, 25))
    assert bal(w, "1140") == D("90.00") and charge.status == "posted"
    assert bal(w, "4600") == D("0") and bal(w, "2600") == D("-120.00")


def test_bank_charge_on_an_empty_purification_account_is_refused_fund_first(w, accounts):
    pur = services.approve_bank_account(purification_draft(w), user=w.p)
    text = refused(services.create_bank_transaction, pur, user=w.a, kind="bank_charge", amount=D("25.00"), date=FEB1,
                   code="purification_account_balance")
    assert "holds only 0.00" in text and "Fund the purification account first" in text and "موّل حساب التطهير" in text


def test_bank_charge_cannot_take_a_later_date_below_zero(w, accounts):
    pur = funded(w, "100.00")
    posted_txn(w, pur, "bank_charge", "80.00", date(2027, 3, 1))
    text = refused(services.create_bank_transaction, pur, user=w.a, kind="bank_charge", amount=D("30.00"), date=FEB1,
                   code="purification_account_balance")
    assert "20.00" in text and "2027-03-01" in text


def test_bank_charge_is_checked_again_at_approval(w, accounts):
    pur = funded(w, "50.00")
    first = services.create_bank_transaction(pur, user=w.a, kind="bank_charge", amount=D("40.00"), date=FEB1)
    services.submit_bank_transaction(first, user=w.a)
    posted_txn(w, pur, "bank_charge", "40.00", FEB1)  # a second charge approved first
    refused(services.approve_bank_transaction, first, user=w.p, code="purification_account_balance")
    assert BankTransaction.objects.get(pk=first.pk).status == "submitted"
    assert bal(w, "1140") == D("10.00")


def test_reversing_interest_credited_cannot_take_the_account_below_zero(w, accounts):
    pur = services.approve_bank_account(purification_draft(w), user=w.p)
    interest = posted_txn(w, pur, "conventional_interest_credit", "100.00", date(2027, 1, 15))
    posted_txn(w, pur, "bank_charge", "60.00", date(2027, 1, 20))
    with pytest.raises(BankingError) as exc:  # the ledger's 1140 check, under the purification lock
        services.reverse_bank_transaction(interest, user=w.p, reason="booked twice")
    assert "Fund the purification account first" in " ".join(exc.value.messages)
    assert BankTransaction.objects.get(pk=interest.pk).status == "posted"


def test_manual_journals_never_touch_the_purification_account(w, accounts):
    """Reviewer probe: a manual journal moving money out of 1140 (or into it) is refused, as a draft and at posting."""
    funded(w)
    for lines in ([{"account": "5700", "debit": D("500.00")}, {"account": "1140", "credit": D("500.00")}],
                  [{"account": "1140", "debit": D("500.00")}, {"account": "3900", "credit": D("500.00")}]):
        with pytest.raises(LedgerError) as exc:
            journal(w, w.alm, FEB1, lines)
        assert exc.value.code == "restricted_account"
        assert "never by a manual journal" in str(exc.value) and ARABIC.search(str(exc.value))
    draft = ledger.create_entry(w.alm, FEB1, "Charge / رسوم", [{"account": "5700", "debit": D("1.00")},
                                                                 {"account": "1101", "credit": D("1.00")}], user=w.a)
    with pytest.raises(LedgerError) as exc:  # a draft is not changed into one either
        ledger.update_draft(draft, user=w.a, lines=[{"account": "5700", "debit": D("1.00")},
                                                    {"account": "1140", "credit": D("1.00")}])
    assert exc.value.code == "restricted_account"
    assert bal(w, "1140") == D("500.00")


def test_purification_account_statement_lines_offer_only_the_allowed_kinds(w, accounts):
    pur = funded(w)
    imp = statement(w, pur, [(date(2027, 1, 20), "D", "15.00", "NCHG", "", "BR1", "CHARGES"),
                             (date(2027, 1, 21), "C", "40.00", "NINT", "", "BR2", "PROFIT")],
                    start=date(2027, 1, 1), end=date(2027, 1, 31))
    debit, credit = imp.lines.order_by("booking_date")
    assert services.available_kinds(debit, user=w.a) == ["bank_charge"]
    assert services.available_kinds(credit, user=w.a) == ["islamic_profit", "conventional_interest_credit"]


def test_approving_a_bank_transaction_on_the_purification_account_takes_the_purification_lock(w, accounts):
    from django.test.utils import CaptureQueriesContext

    pur = funded(w)
    txn = services.create_bank_transaction(pur, user=w.a, kind="bank_charge", amount=D("5.00"), date=FEB1)
    services.submit_bank_transaction(txn, user=w.a)
    with CaptureQueriesContext(connection) as queries:
        services.approve_bank_transaction(txn, user=w.p)
    entity_reads = [q["sql"] for q in queries.captured_queries if 'FROM "core_entity"' in q["sql"]]
    if connection.vendor == "postgresql":
        assert any("FOR NO KEY UPDATE" in sql for sql in entity_reads)
    else:
        assert entity_reads


@postgres_only
def test_database_refuses_lines_on_1140_outside_its_documents(w, accounts):
    funded(w)
    entry = ledger.create_entry(w.alm, FEB1, "Charge / رسوم", [{"account": "5700", "debit": D("1.00")},
                                                                 {"account": "1101", "credit": D("1.00")}], user=w.a)
    with pytest.raises(DatabaseError, match="1140"), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("UPDATE ledger_journalline SET account_id = (SELECT id FROM ledger_account WHERE entity_id = %s "
                       "AND code = '1140') WHERE entry_id = %s AND debit = 0", [w.alm.pk, entry.pk])
    # an import draft may hold a 1140 line (a migration opening), but is posted only with its opening import record
    imported = ledger.create_entry(w.alm, FEB1, "Import / استيراد", [{"account": "1140", "debit": D("1.00")},
                                                                    {"account": "3900", "credit": D("1.00")}],
                                   user=w.a, source="import")
    with pytest.raises(DatabaseError, match="1140"), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("UPDATE ledger_journalentry SET status = 'posted', number = 'ALM-JE-2027-999999', "
                       "posted_by_id = %s, posted_at = now() WHERE id = %s", [w.p.pk, imported.pk])
    with pytest.raises(LedgerError) as exc:  # and the service refuses it too
        ledger.post_entry(imported, access.require(w.p, w.alm, "ledger.post_entry", document=imported))
    assert exc.value.code == "restricted_account"
