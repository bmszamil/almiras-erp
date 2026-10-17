"""D31b review fixes (invoicing R54 amendment; E14a, E15; OQ-30, OQ-64). Ported from the reviewer's probes
(``test_zz_review_probe.py``) plus one test per fixed rule. Made-up names, IBANs and amounts only.

- 2: every entry on 1140 takes the purification lock; nothing but a payout takes 1140 below zero.
- 3: funding room = 2600 balance less 1140 balance (on its date and later), so bank charges are topped up.
- 4: conventional interest credited to 2600 by banking B4 is payable.
- 5: bank instructions carry the beneficiary IBAN hash and the purification account's IBAN last 4 and hash.
- 6: no bulk or base-manager writes; no pre-approved or inactive creates; cancelling needs the payout reversed.
- 8: a recipient's bank is the bank its IBAN names.
- 9: an exported instruction's payout is never reversed.
"""
from datetime import date

import pytest
from django.db import DatabaseError, connection, models, transaction

from banking import services as banking
from banking.crypto import keyed_hash
from banking.models import Bank
from banking.testing import make_iban
from core.access.testing import make_user
from invoicing import services
from invoicing.conftest import D
from invoicing.models import CharityRecipient, InvoicingError, PurificationInstruction, PurificationTransfer
from invoicing.test_purification import (
    CHARITY_IBAN,
    PURIFY_IBAN,
    _posted,
    balance,
    cash,
    flagged_invoice,
    managing_partner,
    ready,
    recipient,
)
from ledger.models import Account
from ledger.services_setup import install_bank_sub_account

pytestmark = pytest.mark.django_db
JAN31, FEB1, FEB5 = date(2027, 1, 31), date(2027, 2, 1), date(2027, 2, 5)
postgres_only = pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL triggers")


def refused(code, fn, *args, **kwargs):
    with pytest.raises(InvoicingError) as exc:
        fn(*args, **kwargs)
    text = " ".join(exc.value.messages)
    assert exc.value.code == code, (exc.value.code, text)
    assert " / " in text  # bilingual
    return text


def bank_txn(w, account, kind, amount, on):
    txn = banking.create_bank_transaction(account, user=w.accountant, kind=kind, amount=D(amount), date=on)
    banking.submit_bank_transaction(txn, user=w.accountant)
    return banking.approve_bank_transaction(txn, user=w.cfo)


def payout(w, amount, charity, on=FEB5):
    return services.create_purification_transfer(w.alm, user=w.accountant, kind="payout", date=on, amount=amount,
                                                 recipient=charity)


def operating_account(w):
    """An operating SAR bank account on 1101 (conventional), for banking B4."""
    gl = install_bank_sub_account(w.alm, "1101", "Riyad SAR", "الرياض ريال", "SAR")
    account = banking.create_bank_account(
        w.alm, user=w.accountant, code="RIYAD-SAR", name_en="Operating", name_ar="تشغيلي", bank="riyad",
        currency="SAR", gl_account=gl, sharia_basis="conventional", statement_format="mt940",
        opening_statement_date=date(2027, 1, 1), opening_statement_balance=D("0.00"), active_from=date(2027, 1, 1),
        iban=make_iban("20", "000000000000509876"))
    return banking.approve_bank_account(account, user=w.cfo)


# --- 2 and 3: bank charges, the purification lock and topping up -------------------------------------------------

def test_probe_bank_charge_is_topped_up_and_the_full_amount_reaches_charity(w):
    """Reviewer probe: a bank charge on the purification account used to strand charity money (no top-up, no
    payout of the rest). Now: charge 100.00, payout 900.00, top up 100.00, pay out the last 100.00."""
    account, charity = ready(w, "1000.00")
    bank_txn(w, account, "bank_charge", "100.00", FEB1)
    _posted(w, "payout", "900.00", FEB5, recipient=charity)
    pos = services.purification_position(w.alm, user=w.accountant)
    assert (pos["payable"], pos["purification_account_balance"], pos["to_fund"]) == (D("100.00"), D("0.00"),
                                                                                     D("100.00"))
    refused("purification_invalid", payout, w, "100.00", charity)  # the account is empty: fund it first
    _posted(w, "funding", "100.00", FEB5, bank_account="1100")
    _posted(w, "payout", "100.00", FEB5, recipient=charity)
    assert balance(w, "2600") == cash(w) == D("0.00")
    assert sum(PurificationInstruction.objects.values_list("amount", flat=True), D("0")) == D("1000.00")


def test_charge_then_top_up_then_full_payout(w):
    account, charity = ready(w, "1000.00")
    bank_txn(w, account, "bank_charge", "40.00", FEB1)
    assert services.purification_position(w.alm, user=w.accountant)["to_fund"] == D("40.00")
    assert "40.00" in refused("purification_invalid", services.create_purification_transfer, w.alm, user=w.accountant,
                              kind="funding", date=FEB1, amount="40.01", bank_account="1100")
    _posted(w, "funding", "40.00", FEB1, bank_account="1100")
    p = _posted(w, "payout", "1000.00", FEB5, recipient=charity)
    assert PurificationInstruction.objects.get(payout=p).amount == D("1000.00")
    assert balance(w, "2600") == cash(w) == D("0.00")


def test_funding_room_counts_later_dates(w):
    account, _ = ready(w, "1000.00")
    bank_txn(w, account, "bank_charge", "30.00", date(2027, 3, 1))
    # nothing is missing before the charge: a top-up dated earlier would leave more in 1140 than is owed then
    assert "2027-02-01" in refused("purification_invalid", services.create_purification_transfer, w.alm,
                                   user=w.accountant, kind="funding", date=FEB1, amount="30.00", bank_account="1100")
    assert _posted(w, "funding", "30.00", date(2027, 3, 1), bank_account="1100").status == "posted"
    refused("purification_invalid", services.create_purification_transfer, w.alm, user=w.accountant, kind="funding",
            date=date(2027, 3, 2), amount="0.01", bank_account="1100")


def test_bank_charge_on_an_empty_purification_account_is_refused_with_fund_first(w):
    account, _ = ready(w, "1000.00", fund=False)
    from banking.models import BankingError

    with pytest.raises(BankingError) as exc:
        banking.create_bank_transaction(account, user=w.accountant, kind="bank_charge", amount=D("10.00"), date=FEB1)
    assert exc.value.code == "purification_account_balance"
    assert "Fund the purification account first" in str(exc.value) and "موّل حساب التطهير" in str(exc.value)


def test_entries_on_1140_take_the_purification_lock_and_never_take_it_below_zero(w):
    """Review fix 2: the ledger takes the purification lock for any entry with a 1140 line and refuses a non-payout
    credit that would take 1140 below zero on its date or later (here a funding reversal dated before a charge)."""
    from django.test.utils import CaptureQueriesContext

    account, charity = ready(w, "1000.00")
    bank_txn(w, account, "bank_charge", "10.00", date(2027, 3, 1))
    funding = PurificationTransfer.objects.get(kind="funding")
    text = refused("purification_invalid", services.reverse_purification_transfer, funding, user=w.accountant,
                   reason="wrong")
    assert "990.00" in text and "2027-03-01" in text
    with CaptureQueriesContext(connection) as queries:
        bank_txn(w, account, "islamic_profit", "5.00", date(2027, 3, 2))
    entity_reads = [q["sql"] for q in queries.captured_queries if 'FROM "core_entity"' in q["sql"]]
    assert entity_reads and (connection.vendor != "postgresql"
                             or any("FOR NO KEY UPDATE" in sql for sql in entity_reads))


# --- 4: conventional interest credited to 2600 is payable --------------------------------------------------------

def test_probe_interest_credited_to_2600_by_banking_b4_is_payable(w):
    _, charity = ready(w, "1000.00")
    bank_txn(w, operating_account(w), "conventional_interest_credit", "300.00", FEB1)  # B4: Dr 1101 / Cr 2600
    pos = services.purification_position(w.alm, user=w.accountant)
    assert (pos["payable"], pos["to_fund"], pos["transferred"]) == (D("1300.00"), D("300.00"), D("1000.00"))
    refused("purification_invalid", payout, w, "1300.00", charity)  # 1140 holds only 1,000.00
    _posted(w, "funding", "300.00", FEB1, bank_account="1101")
    _posted(w, "payout", "1300.00", FEB5, recipient=charity)
    assert balance(w, "2600") == cash(w) == D("0.00")
    refused("purification_invalid", payout, w, "0.01", charity)


def test_interest_credited_on_the_purification_account_is_payable_without_funding(w):
    account, charity = ready(w, "1000.00")
    bank_txn(w, account, "conventional_interest_credit", "50.00", FEB1)  # Dr 1140 / Cr 2600
    pos = services.purification_position(w.alm, user=w.accountant)
    assert (pos["payable"], pos["purification_account_balance"], pos["to_fund"]) == (D("1050.00"), D("1050.00"),
                                                                                     D("0.00"))
    refused("purification_invalid", services.create_purification_transfer, w.alm, user=w.accountant, kind="funding",
            date=FEB1, amount="0.01", bank_account="1100")
    _posted(w, "payout", "1050.00", FEB5, recipient=charity)
    assert balance(w, "2600") == cash(w) == D("0.00")


# --- 5: bank instruction snapshots -------------------------------------------------------------------------------

def test_probe_instruction_is_tied_to_the_recipient_and_the_purification_account_iban(w):
    account, charity = ready(w, "1000.00")
    p = _posted(w, "payout", "100.00", FEB5, recipient=charity)
    instruction = PurificationInstruction.objects.get(payout=p)
    assert instruction.beneficiary_iban_hash == charity.iban_hash == keyed_hash(CHARITY_IBAN)
    assert (instruction.from_iban_last4, instruction.from_iban_hash) == (PURIFY_IBAN[-4:], keyed_hash(PURIFY_IBAN))
    assert (instruction.beneficiary_bank, instruction.beneficiary_name_en) == (charity.bank, charity.name_en)
    row = services.purification_instruction_row(instruction, user=w.viewer)
    assert row["from_iban"].endswith(PURIFY_IBAN[-4:]) and PURIFY_IBAN not in str(row)


# --- 6: guards on SQLite ------------------------------------------------------------------------------------------

def test_probe_base_manager_update_is_refused(w):
    _, rec = ready(w, "1000.00")
    for manager in (CharityRecipient._base_manager, CharityRecipient.objects):
        with pytest.raises(InvoicingError):
            manager.filter(pk=rec.pk).update(iban_last4="9999", name_en="Swapped")
    p = _posted(w, "payout", "10.00", FEB5, recipient=rec)
    instruction = PurificationInstruction.objects.get(payout=p)
    with pytest.raises(InvoicingError):
        PurificationInstruction._base_manager.filter(pk=instruction.pk).update(status="cancelled")
    with pytest.raises(InvoicingError):
        PurificationTransfer._base_manager.filter(pk=p.pk).update(status="draft")
    assert CharityRecipient.objects.get(pk=rec.pk).name_en == "Test Charity"
    assert PurificationInstruction.objects.get(pk=instruction.pk).status == "pending"


def test_probe_bulk_create_of_a_preapproved_recipient_is_refused(w):
    other = make_user("x")
    iban = make_iban("80", "000000000000509999")
    with pytest.raises(InvoicingError):
        CharityRecipient.objects.bulk_create([CharityRecipient(
            entity=w.alm, name_en="Rogue", name_ar="مزيف", bank=Bank.objects.get(code="alrajhi"), iban=iban,
            iban_hash=keyed_hash(iban), iban_last4=iban[-4:], created_by=other, approved_by=w.accountant,
            approved_at=other.date_joined)])
    with pytest.raises(InvoicingError):
        CharityRecipient._base_manager.bulk_create([CharityRecipient(
            entity=w.alm, name_en="Rogue", name_ar="مزيف", bank=Bank.objects.get(code="alrajhi"), iban=iban,
            iban_hash=keyed_hash(iban), iban_last4=iban[-4:], created_by=other)])
    assert not CharityRecipient.objects.exists()


def test_bulk_writes_of_instructions_and_posted_purification_rows_are_refused(w):
    _, charity = ready(w, "1000.00")
    p = _posted(w, "payout", "10.00", FEB5, recipient=charity)
    instruction = PurificationInstruction.objects.get(payout=p)
    with pytest.raises(InvoicingError):
        PurificationInstruction.objects.bulk_create([PurificationInstruction(
            entity=w.alm, payout=p, from_account=instruction.from_account, recipient=charity, amount=D("1.00"))])
    with pytest.raises(InvoicingError):
        PurificationInstruction.objects.bulk_update([instruction], ["amount"])
    with pytest.raises(InvoicingError):
        PurificationTransfer.objects.bulk_create([PurificationTransfer(
            entity=w.alm, kind="transfer", date=FEB5, amount=D("1.00"), status="posted", number="X-1",
            created_by=w.accountant)])
    assert PurificationTransfer.objects.filter(number="X-1").count() == 0


def test_a_recipient_is_never_created_approved_or_inactive(w):
    rec = CharityRecipient(entity=w.alm, name_en="Rogue", name_ar="مزيف", bank=Bank.objects.get(code="alrajhi"),
                           iban=CHARITY_IBAN, iban_hash=keyed_hash(CHARITY_IBAN), iban_last4=CHARITY_IBAN[-4:],
                           created_by=w.accountant)
    for field, value in (("approved_by", managing_partner(w)), ("is_active", False)):
        candidate = CharityRecipient(**{f.attname: getattr(rec, f.attname) for f in rec._meta.concrete_fields})
        setattr(candidate, field, value)
        if field == "approved_by":
            candidate.approved_at = w.mp.date_joined
        candidate._service_write = True
        with pytest.raises(InvoicingError) as exc:
            candidate.save()
        assert exc.value.code == "recipient_immutable"
    assert not CharityRecipient.objects.exists()


def test_an_instruction_is_created_pending_and_cancelled_only_after_its_payout_is_reversed(w):
    _, charity = ready(w, "1000.00")
    p = _posted(w, "payout", "10.00", FEB5, recipient=charity)
    instruction = PurificationInstruction.objects.get(payout=p)
    instruction.status, instruction.cancelled_by = "cancelled", w.cfo
    instruction._service_write = True
    with pytest.raises(InvoicingError) as exc:  # the payout is still posted
        instruction.save()
    assert exc.value.code == "instruction_immutable"
    clone = PurificationInstruction(**{f.attname: getattr(instruction, f.attname)
                                       for f in instruction._meta.concrete_fields if f.attname != "id"})
    clone.status = "cancelled"
    clone._service_write = True
    with pytest.raises(InvoicingError):
        clone.save()
    services.reverse_purification_transfer(p, user=w.accountant, reason="wrong")
    assert PurificationInstruction.objects.get(pk=instruction.pk).status == "cancelled"


# --- 8: the bank is the one the IBAN names ------------------------------------------------------------------------

def test_recipient_bank_is_derived_from_the_iban(w):
    rec = services.create_charity_recipient(w.alm, user=w.accountant, name_en="Charity", name_ar="جمعية",
                                            iban=make_iban("10", "000000000000501234"))
    assert rec.bank.code == "snb"  # positions 5-6 = 10
    row = services.charity_recipient_row(rec, user=managing_partner(w))
    assert (row["iban_bank_code"], row["iban_bank_en"], row["iban_bank_matches"]) == ("10", "Saudi National Bank",
                                                                                      True)
    assert row["iban_bank_ar"] and row["iban_bank_verified"] is False  # seeded codes still await OQ-4
    text = refused("charity_recipient_invalid", services.create_charity_recipient, w.alm, user=w.accountant,
                   name_en="Charity", name_ar="جمعية", iban=make_iban("99", "000000000000501235"))
    assert "'99'" in text and "positions 5-6" in text
    refused("charity_recipient_invalid", services.create_charity_recipient, w.alm, user=w.accountant,
            name_en="Charity", name_ar="جمعية", bank="riyad", iban=make_iban("10", "000000000000501236"))


@pytest.mark.skipif(connection.vendor == "postgresql", reason="the PostgreSQL trigger refuses the raw change itself")
def test_approval_refused_when_the_stored_bank_differs_from_the_iban(w):
    rec = recipient(w, approve=False)
    models.QuerySet.update(CharityRecipient.objects.filter(pk=rec.pk), bank=Bank.objects.get(code="riyad"))
    refused("charity_recipient_invalid", services.approve_charity_recipient, rec, user=managing_partner(w))


# --- 9: exported instructions -------------------------------------------------------------------------------------

def test_payout_with_an_exported_instruction_is_not_reversed(w):
    _, charity = ready(w, "1000.00")
    p = _posted(w, "payout", "100.00", FEB5, recipient=charity)
    instruction = PurificationInstruction.objects.get(payout=p)
    # nothing sets ``exported`` yet (banking OQ-2, OQ-64(d)): the future bank-file export would
    models.QuerySet.update(PurificationInstruction.objects.filter(pk=instruction.pk), status="exported")
    text = refused("instruction_exported", services.reverse_purification_transfer, p, user=w.accountant,
                   reason="wrong")
    assert "OQ-64(d)" in text
    assert PurificationTransfer.objects.get(pk=p.pk).status == "posted"


# --- PostgreSQL ---------------------------------------------------------------------------------------------------

def _db_refused(sql, params=()):
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql, params)


@postgres_only
def test_db_instruction_must_match_the_recipient_and_the_purification_account(w):
    _, charity = ready(w, "1000.00")
    p = _posted(w, "payout", "100.00", FEB5, recipient=charity)
    instruction = PurificationInstruction.objects.get(payout=p)
    for column in ("beneficiary_iban_hash", "from_iban_hash", "from_iban_last4", "beneficiary_name_en"):
        _db_refused(f"UPDATE invoicing_purificationinstruction SET {column} = 'x' WHERE id = %s", [instruction.pk])
    rogue = PurificationInstruction(**{f.attname: getattr(instruction, f.attname)
                                       for f in instruction._meta.concrete_fields if f.attname != "id"})
    rogue.beneficiary_iban_hash = "f" * 64  # another IBAN than the recipient's
    rogue._service_write = True
    with pytest.raises(DatabaseError, match="recipient IBAN"), transaction.atomic():
        rogue.save()


@postgres_only
def test_db_exported_and_sent_are_status_only_steps_and_block_cancellation(w):
    _, charity = ready(w, "1000.00")
    p = _posted(w, "payout", "100.00", FEB5, recipient=charity)
    pk = PurificationInstruction.objects.get(payout=p).pk
    _db_refused("UPDATE invoicing_purificationinstruction SET status = 'sent' WHERE id = %s", [pk])  # not yet exported
    _db_refused("UPDATE invoicing_purificationinstruction SET status = 'exported', amount = 1 WHERE id = %s", [pk])
    with connection.cursor() as cursor:
        cursor.execute("UPDATE invoicing_purificationinstruction SET status = 'exported' WHERE id = %s", [pk])
        cursor.execute("UPDATE invoicing_purificationinstruction SET status = 'sent' WHERE id = %s", [pk])
    _db_refused("UPDATE invoicing_purificationinstruction SET status = 'pending' WHERE id = %s", [pk])
    refused("instruction_exported", services.reverse_purification_transfer, p, user=w.accountant, reason="wrong")


@postgres_only
def test_db_refuses_a_manual_journal_line_on_1140(w):
    from ledger import services as ledger

    ready(w, "1000.00")
    entry = ledger.create_entry(w.alm, FEB1, "Charge / رسوم", [{"account": "5700", "debit": D("1.00")},
                                                                 {"account": "1100", "credit": D("1.00")}],
                                user=w.accountant)
    gl = Account.objects.get(entity=w.alm, code="1140")
    _db_refused("UPDATE ledger_journalline SET account_id = %s WHERE entry_id = %s AND credit > 0", [gl.pk, entry.pk])
