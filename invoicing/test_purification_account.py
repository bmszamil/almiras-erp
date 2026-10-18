"""D31b (R54 amendment): purification money kept in the designated purification account and paid only to approved
charity recipients. Acceptance tests AT-103 (recipients), AT-104 (funding and payout limits), AT-105 (bank
instruction, masking and reversal). Made-up names, IBANs and amounts only.
"""
from datetime import date

import pytest
from django.db import DatabaseError, connection, transaction

from banking import services as banking
from banking.testing import make_iban
from core import access
from core.access.models import AccessLog
from core.access.testing import make_agent, make_user, member
from invoicing import services
from invoicing.conftest import D
from invoicing.models import CharityRecipient, InvoicingError, PurificationInstruction, PurificationTransfer
from invoicing.test_purification import (
    CHARITY_IBAN,
    PURIFY_IBAN,
    _posted,
    board_approve,
    cash,
    flagged_invoice,
    managing_partner,
    purification_account,
    ready,
    recipient,
)
from ledger.models import Account

pytestmark = pytest.mark.django_db
JAN31, FEB5 = date(2027, 1, 31), date(2027, 2, 5)
postgres_only = pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL triggers")


def refused(code, fn, *args, **kwargs):
    with pytest.raises(InvoicingError) as exc:
        fn(*args, **kwargs)
    text = " ".join(exc.value.messages)
    assert exc.value.code == code, (exc.value.code, text)
    assert " / " in text  # bilingual
    return text


def payout(w, amount, charity, on=FEB5, **extra):
    return services.create_purification_transfer(w.alm, user=w.accountant, kind="payout", date=on, amount=amount,
                                                 recipient=charity, **extra)


def bank_txn(w, account, kind, amount, on):
    """A posted banking transaction on the purification account (D31b: bank charges, Islamic profit and conventional
    interest are the only bank transactions allowed on it), drafted by the accountant and approved by the CFO."""
    txn = banking.create_bank_transaction(account, user=w.accountant, kind=kind, amount=D(amount), date=on)
    banking.submit_bank_transaction(txn, user=w.accountant)
    return banking.approve_bank_transaction(txn, user=w.cfo)


def sub_people(w):
    acc, cfo = make_user("sub_accountant"), make_user("sub_approver")
    member(acc, w.sub, "accountant")
    member(cfo, w.sub, "approver")
    return acc, cfo


# --- AT-103: charity recipients --------------------------------------------------------------------------------

def test_at103_recipient_added_by_the_accountant_and_approved_by_a_managing_partner_who_did_not_add_it(w):
    rec = recipient(w, approve=False)
    assert (rec.is_approved, rec.is_active, rec.iban_last4, rec.created_by) == (False, True, CHARITY_IBAN[-4:],
                                                                                 w.accountant)
    assert CHARITY_IBAN not in str(rec) and rec.masked_iban.endswith(CHARITY_IBAN[-4:])
    with pytest.raises(access.AccessDenied) as exc:  # the CFO approves payouts, not recipients
        services.approve_charity_recipient(rec, user=w.cfo)
    assert exc.value.code == "no_membership"
    both = make_user("accountant_and_partner")
    member(both, w.alm, "accountant")
    member(both, w.alm, "managing_partner")
    own = recipient(w, approve=False, adder=both, iban=make_iban("80", "000000000000503333"))
    with pytest.raises(access.AccessDenied) as exc:  # S!: never the person who added it, whatever roles they hold
        services.approve_charity_recipient(own, user=both)
    assert exc.value.code == "self_approval"
    with pytest.raises(access.AccessDenied):
        services.approve_charity_recipient(rec, user=make_agent("purifier"))
    approved = board_approve(w, rec)
    assert approved.is_approved and approved.approved_by == w.mp
    assert list(services.charity_recipients(w.alm, user=w.viewer, usable_only=True)) == [approved]


def test_at103_wrong_iban_bank_names_and_duplicates_are_refused(w):
    def add(**over):
        fields = dict(name_en="Charity", name_ar="جمعية", bank="alrajhi", iban=make_iban("80", "000000000000504444"))
        fields.update(over)
        return services.create_charity_recipient(w.alm, user=w.accountant, **fields)

    good = make_iban("80", "000000000000504444")
    wrong = good[:-1] + str((int(good[-1]) + 1) % 10)  # one digit off: the mod-97 check fails
    assert "check digits" in refused("charity_recipient_invalid", add, iban=wrong)
    if good[2:4] == "97":  # 97 and 00 are the same mod 97, but 00 is not an ISO 13616 check value
        assert "check digits" in refused("charity_recipient_invalid", add, iban="SA00" + good[4:])
    assert "22 digits" in refused("charity_recipient_invalid", add, iban=make_iban("80", "00000000000050444A"))
    assert "22 digits" in refused("charity_recipient_invalid", add, iban=good[:-1])
    assert "Arabic name" in refused("charity_recipient_invalid", add, name_ar=" ")
    from banking.models import Bank
    Bank.objects.create(code="abroad", name_en="Abroad Bank", name_ar="بنك خارجي", country="AE")
    # review fix 8: the bank is the one the IBAN names (positions 5-6), verified or not; another bank is refused
    assert "belongs to Al Rajhi Bank" in refused("charity_recipient_invalid", add, bank="abroad")
    assert "belongs to Riyad Bank" in refused("charity_recipient_invalid", add,
                                              iban=make_iban("20", "000000000000504444"))
    add()
    assert "already an active charity recipient" in refused("charity_recipient_invalid", add, name_en="Again")


def test_at103_agents_system_users_and_other_roles_cannot_add_recipients(w):
    for user in (make_agent("purifier"), w.queue, w.clerk, w.cfo, w.viewer):
        with pytest.raises(access.AccessDenied):
            services.create_charity_recipient(w.alm, user=user, name_en="Charity", name_ar="جمعية", bank="alrajhi",
                                              iban=CHARITY_IBAN)
    assert not CharityRecipient.objects.exists()


def test_at103_recipients_are_entity_separated(w):
    sub_acc, sub_cfo = sub_people(w)
    with pytest.raises(access.AccessDenied):  # ALM's accountant has no rights in SUB
        recipient(w, w.sub, approve=False)
    sub_rec = recipient(w, w.sub, adder=sub_acc)  # same IBAN in another entity is another recipient
    _, alm_rec = ready(w)
    refused("purification_recipient_not_approved", payout, w, "100.00", sub_rec)  # SUB's recipient, ALM's payout
    assert payout(w, "100.00", alm_rec).recipient == alm_rec
    assert not services.charity_recipients(w.alm, user=w.accountant).filter(pk=sub_rec.pk).exists()
    with pytest.raises(access.AccessDenied):
        services.charity_recipients(w.sub, user=w.accountant)
    with pytest.raises(access.AccessDenied):
        services.reveal_charity_recipient_iban(sub_rec, user=w.accountant)


def test_at103_approved_recipients_never_change_only_deactivate(w):
    rec = recipient(w)
    stored = CharityRecipient.objects.get(pk=rec.pk)
    stored.name_en = "Another charity"
    stored._service_write = True
    with pytest.raises(InvoicingError):
        stored.save()
    with pytest.raises(InvoicingError):
        CharityRecipient.objects.filter(pk=rec.pk).update(name_en="x")
    with pytest.raises(InvoicingError):
        CharityRecipient.objects.filter(pk=rec.pk).delete()
    with pytest.raises(InvoicingError):
        stored.delete()
    refused("reason_required", services.deactivate_charity_recipient, rec, user=w.accountant, reason=" ")
    off = services.deactivate_charity_recipient(rec, user=w.accountant, reason="Closed its account")
    assert (off.is_active, off.deactivated_by) == (False, w.accountant)
    refused("recipient_inactive", services.deactivate_charity_recipient, rec, user=w.accountant, reason="again")
    unapproved = recipient(w, approve=False, iban=make_iban("80", "000000000000505555"))
    services.deactivate_charity_recipient(unapproved, user=w.accountant, reason="duplicate")
    refused("recipient_inactive", board_approve, w, unapproved)


def test_at103_recipient_lists_are_masked_and_unmasked_views_are_logged(w):
    rec = recipient(w)
    row = services.charity_recipient_row(rec, user=w.viewer)
    assert CHARITY_IBAN not in str(row) and row["iban"].endswith(CHARITY_IBAN[-4:]) and row["approved"]
    with pytest.raises(access.AccessDenied):  # viewers have no banking.view_iban
        services.reveal_charity_recipient_iban(rec, user=w.viewer)
    before = AccessLog.objects.filter(event="personal_data_viewed").count()
    assert services.reveal_charity_recipient_iban(rec, user=w.accountant) == {"iban": CHARITY_IBAN}
    log = AccessLog.objects.filter(event="personal_data_viewed").order_by("-pk").first()
    assert AccessLog.objects.filter(event="personal_data_viewed").count() == before + 1
    assert (log.actor, log.permission, log.details["object"]) == (w.accountant, "banking.view_iban",
                                                                  ["invoicing.charityrecipient", rec.pk])
    assert CHARITY_IBAN not in str(log.details)


# --- AT-104: purification account, funding and payout limits ---------------------------------------------------

def test_at104_payout_needs_the_purification_account_and_an_approved_active_recipient(w):
    flagged_invoice(w, "1000.00")
    _posted(w, "transfer", "1000.00", JAN31)
    charity = recipient(w)
    assert "no active purification account" in refused("purification_account_required", payout, w, "100.00", charity)
    refused("purification_account_required", services.create_purification_transfer, w.alm, user=w.accountant,
            kind="funding", date=JAN31, amount="100.00", bank_account="1100")
    purification_account(w)
    _posted(w, "funding", "1000.00", JAN31, bank_account="1100")
    assert "only from the purification account" in refused("purification_account_required", payout, w, "100.00",
                                                           charity, bank_account="1100")
    pending = recipient(w, approve=False, iban=make_iban("80", "000000000000506666"))
    refused("purification_recipient_not_approved", payout, w, "100.00", pending)
    refused("purification_recipient_not_approved", payout, w, "100.00", None)
    refused("purification_recipient_not_approved", payout, w, "100.00", charity, recipient_ar="جهة أخرى")
    draft = payout(w, "100.00", charity)
    services.deactivate_charity_recipient(charity, user=w.accountant, reason="stopped")
    refused("purification_recipient_not_approved", services.post_purification_transfer, draft, user=w.cfo)
    assert PurificationTransfer.objects.get(pk=draft.pk).status == "draft"
    assert not PurificationInstruction.objects.exists()


def test_at104_designation_changed_after_recording_is_refused_at_posting(w):
    account, charity = ready(w)
    draft = payout(w, "100.00", charity)
    # the account holds 1,000.00: reverse the funding so the account can be closed (banking: no closing with money)
    services.reverse_purification_transfer(PurificationTransfer.objects.get(kind="funding"), user=w.accountant,
                                           reason="moving bank")
    banking.close_bank_account(account, user=w.cfo, active_to=date(2027, 2, 28), reason="moving bank")
    refused("purification_account_required", services.post_purification_transfer, draft, user=w.cfo)


def test_at104_funding_is_limited_to_what_was_transferred_and_not_yet_funded(w):
    flagged_invoice(w, "1000.00")
    purification_account(w)
    refused("purification_invalid", services.create_purification_transfer, w.alm, user=w.accountant,
            kind="funding", date=JAN31, amount="1.00", bank_account="1100")  # nothing transferred yet
    _posted(w, "transfer", "600.00", date(2027, 2, 10))
    text = refused("purification_invalid", services.create_purification_transfer, w.alm, user=w.accountant,
                   kind="funding", date=date(2027, 2, 1), amount="1.00", bank_account="1100")  # before the transfer
    assert "2027-02-01" in text
    _posted(w, "funding", "400.00", date(2027, 2, 10), bank_account="1100")
    assert "200.00" in refused("purification_invalid", services.create_purification_transfer, w.alm,
                               user=w.accountant, kind="funding", date=date(2027, 2, 11), amount="200.01",
                               bank_account="1100")
    for gl in ("1140", "2600", "1200"):  # never from the purification account itself or a non-bank account
        refused("bank_account", services.create_purification_transfer, w.alm, user=w.accountant, kind="funding",
                date=date(2027, 2, 11), amount="1.00", bank_account=gl)
    with pytest.raises(access.AccessDenied):  # the recorder never posts it
        services.post_purification_transfer(services.create_purification_transfer(
            w.alm, user=w.accountant, kind="funding", date=date(2027, 2, 11), amount="200.00", bank_account="1100"),
            user=w.accountant)
    pos = services.purification_position(w.alm, user=w.accountant)
    assert (pos["transferred"], pos["funded"], pos["to_fund"]) == (D("600.00"), D("400.00"), D("200.00"))
    assert cash(w) == D("400.00")


def test_at104_payout_is_capped_by_the_purification_account_balance_on_every_later_date(w):
    flagged_invoice(w, "1000.00")
    _posted(w, "transfer", "1000.00", JAN31)
    account = purification_account(w)
    charity = recipient(w)
    _posted(w, "funding", "400.00", JAN31, bank_account="1100")
    text = refused("purification_invalid", payout, w, "500.00", charity)  # 2600 holds 1,000.00 but 1140 only 400.00
    assert "400.00" in text and "1140" in text
    bank_txn(w, account, "bank_charge", "100.00", date(2027, 3, 1))
    text = refused("purification_invalid", payout, w, "400.00", charity, on=date(2027, 2, 10))
    assert "300.00" in text and "2027-03-01" in text  # a later bank charge leaves only 300.00
    assert _posted(w, "payout", "300.00", date(2027, 2, 10), recipient=charity).status == "posted"
    assert cash(w) == D("0.00")
    refused("purification_invalid", payout, w, "0.01", charity, on=date(2027, 3, 5))


def test_at104_payout_limits_are_rechecked_at_posting_under_the_entity_lock(w):
    _, charity = ready(w, "1000.00")
    first, second = payout(w, "700.00", charity), payout(w, "700.00", charity)
    services.post_purification_transfer(first, user=w.cfo)
    text = refused("purification_invalid", services.post_purification_transfer, second, user=w.cfo)
    assert "300.00" in text
    assert PurificationTransfer.objects.get(pk=second.pk).status == "draft"


def test_at104_receipts_and_payments_cannot_use_the_purification_account(w):
    with pytest.raises(InvoicingError) as exc:
        services.create_receipt(w.alm, user=w.clerk, customer=w.customer, date=JAN31, amount=D("100.00"),
                                bank_account="1140")
    assert exc.value.code == "bank_account"
    with pytest.raises(InvoicingError) as exc:
        services.create_vendor_payment(w.alm, user=w.ap, vendor=w.vendor, date=JAN31, amount=D("100.00"),
                                       bank_account="1140")
    assert exc.value.code == "bank_account"


def test_at104_purification_accounts_are_entity_separated(w):
    sub_acc, sub_cfo = sub_people(w)
    purification_account(w, w.sub, preparer=sub_acc, approver=sub_cfo, iban=make_iban("20", "000000000000507777"))
    flagged_invoice(w, "1000.00")
    _posted(w, "transfer", "1000.00", JAN31)
    # SUB's purification account is not ALM's: ALM has none
    refused("purification_account_required", services.create_purification_transfer, w.alm, user=w.accountant,
            kind="funding", date=JAN31, amount="100.00", bank_account="1100")
    with pytest.raises(InvoicingError):  # nor can ALM fund from SUB's bank GL
        services.create_purification_transfer(w.alm, user=w.accountant, kind="funding", date=JAN31, amount="1.00",
                                              bank_account=Account.objects.get(entity=w.sub, code="1100"))


# --- AT-105: bank instruction, masking and reversal ------------------------------------------------------------

def test_at105_payout_produces_a_masked_bank_instruction_and_unmasked_views_are_logged(w):
    account, charity = ready(w, "1000.00")
    p = _posted(w, "payout", "250.00", FEB5, recipient=charity, reference_en="Purification Q1")
    instruction = PurificationInstruction.objects.get(payout=p)
    assert (instruction.from_account, instruction.amount, instruction.execution_date, instruction.status) == (
        account, D("250.00"), FEB5, "pending")
    assert instruction.reference == f"{p.number} Purification Q1"
    assert (instruction.beneficiary_name_en, instruction.beneficiary_bank.code) == ("Test Charity", "alrajhi")
    rows = [services.purification_instruction_row(i, user=w.viewer)
            for i in services.purification_instructions(w.alm, user=w.viewer)]
    assert len(rows) == 1 and CHARITY_IBAN not in str(rows) and PURIFY_IBAN not in str(rows)
    assert rows[0]["iban"].endswith(CHARITY_IBAN[-4:]) and rows[0]["from_iban"].endswith(PURIFY_IBAN[-4:])
    assert CHARITY_IBAN not in str(instruction)
    for user in (w.viewer, make_agent("purifier")):
        with pytest.raises(access.AccessDenied):
            services.reveal_purification_instruction(instruction, user=user)
    assert services.reveal_purification_instruction(instruction, user=w.cfo) == {"beneficiary_iban": CHARITY_IBAN}
    log = AccessLog.objects.filter(event="personal_data_viewed").order_by("-pk").first()
    assert (log.actor, log.details["object"]) == (w.cfo, ["invoicing.purificationinstruction", instruction.pk])
    with pytest.raises(access.AccessDenied):  # another entity's user sees nothing
        services.purification_instructions(w.alm, user=w.sub_cfo)


def test_at105_instruction_never_changes_and_is_cancelled_by_the_payout_reversal(w):
    _, charity = ready(w, "1000.00")
    p = _posted(w, "payout", "250.00", FEB5, recipient=charity)
    instruction = PurificationInstruction.objects.get(payout=p)
    instruction.amount = D("1.00")
    instruction._service_write = True
    with pytest.raises(InvoicingError):
        instruction.save()
    with pytest.raises(InvoicingError):
        PurificationInstruction.objects.filter(pk=instruction.pk).update(status="cancelled")
    with pytest.raises(InvoicingError):
        PurificationInstruction.objects.get(pk=instruction.pk).delete()
    reversed_ = services.reverse_purification_transfer(p, user=w.accountant, reason="wrong amount")
    instruction.refresh_from_db()
    assert (reversed_.status, instruction.status, instruction.cancelled_by) == ("reversed", "cancelled", w.accountant)
    assert cash(w) == D("1000.00")
    again = _posted(w, "payout", "1000.00", FEB5, recipient=charity)  # the room is back after the reversal
    assert PurificationInstruction.objects.get(payout=again).status == "pending"


def test_at105_funding_reversal_keeps_the_purification_account_balance_non_negative(w):
    _, charity = ready(w, "1000.00")
    funding = PurificationTransfer.objects.get(kind="funding")
    p = _posted(w, "payout", "600.00", FEB5, recipient=charity)
    text = refused("purification_invalid", services.reverse_purification_transfer, funding, user=w.accountant,
                   reason="wrong")
    assert "400.00" in text
    services.reverse_purification_transfer(p, user=w.accountant, reason="wrong")
    assert services.reverse_purification_transfer(funding, user=w.accountant, reason="wrong").status == "reversed"
    assert cash(w) == D("0") and services.purification_position(w.alm, user=w.accountant)["to_fund"] == D("1000.00")
    for user in (make_agent("purifier"), w.queue):
        with pytest.raises(access.AccessDenied):
            services.reverse_purification_transfer(PurificationTransfer.objects.get(kind="transfer"), user=user,
                                                   reason="x")


# --- PostgreSQL triggers (migration 0022) ----------------------------------------------------------------------

def _db_refused(sql, params=()):
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql, params)


@postgres_only
def test_db_locks_recipients_instructions_and_payout_postings(w):
    pending = recipient(w, approve=False, iban=make_iban("80", "000000000000508888"))
    _db_refused("UPDATE invoicing_charityrecipient SET approved_by_id = created_by_id, approved_at = now() "
                "WHERE id = %s", [pending.pk])  # never approved by its creator
    _, charity = ready(w, "1000.00")
    _db_refused("UPDATE invoicing_charityrecipient SET name_en = 'x' WHERE id = %s", [charity.pk])
    _db_refused("UPDATE invoicing_charityrecipient SET is_active = true, approved_by_id = NULL, approved_at = NULL "
                "WHERE id = %s", [charity.pk])
    _db_refused("DELETE FROM invoicing_charityrecipient WHERE id = %s", [charity.pk])
    draft = payout(w, "100.00", charity)
    with connection.cursor() as cursor:  # raw SQL may point a draft at an unapproved recipient; posting it is refused
        cursor.execute("UPDATE invoicing_purificationtransfer SET recipient_id = %s WHERE id = %s",
                       [pending.pk, draft.pk])
    _db_refused("UPDATE invoicing_purificationtransfer SET status = 'posted', number = 'ALM-PUR-2027-999999', "
                "posted_by_id = %s, posted_at = now() WHERE id = %s", [w.cfo.pk, draft.pk])
    p = _posted(w, "payout", "250.00", FEB5, recipient=charity)
    instruction = PurificationInstruction.objects.get(payout=p)
    _db_refused("UPDATE invoicing_purificationinstruction SET amount = 1 WHERE id = %s", [instruction.pk])
    _db_refused("UPDATE invoicing_purificationinstruction SET status = 'cancelled', cancelled_by_id = %s, "
                "cancelled_at = now() WHERE id = %s", [w.cfo.pk, instruction.pk])  # payout not reversed
    _db_refused("DELETE FROM invoicing_purificationinstruction WHERE id = %s", [instruction.pk])
    _db_refused("UPDATE invoicing_purificationtransfer SET recipient_id = %s WHERE id = %s", [pending.pk, p.pk])
