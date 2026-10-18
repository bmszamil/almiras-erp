"""Owner decisions of 2026-10-18, section C (invoicing R54 purification, as built).

C1 every entity; C2 the CFO approves the funding; C3 recipients expire 12 months after the last board approval
(alerts 30 and 7 days before; re-approval resets the clock; reactivation by a second person, no re-entry); C4 an
accounting user marks bank instructions exported / sent; C5 the board approves the list (recorded by the Managing
Partner) and any return on the purification account goes to charity (2600); C6 1140 is restricted cash and money
never flows back except by an error-correcting reversal; C7 1140 approved. Made-up names, IBANs and amounts.
"""
from datetime import date
from pathlib import Path

import pytest
from django.db import DatabaseError, connection, transaction

from banking import services as banking
from banking.models import BankingError
from banking.testing import make_iban
from core import access
from core.access.seed import ROLE_SEED
from core.access.testing import make_agent, make_user, member
from core.models import Entity
from invoicing import services
from invoicing.conftest import D
from invoicing.models import (
    CharityBoardApproval,
    CharityRecipient,
    CharityRecipientAlert,
    InvoicingError,
    PurificationInstruction,
    PurificationTransfer,
)
from invoicing.test_purification import (
    _posted,
    balance,
    board_approve,
    cash,
    flagged_invoice,
    managing_partner,
    purification_account,
    ready,
    recipient,
)
from invoicing.test_purification_account import bank_txn, payout, refused
from ledger import restricted
from ledger import services as ledger
from ledger.models import Account, JournalEntry, LedgerError

pytestmark = pytest.mark.django_db
JAN31, FEB1, FEB5 = date(2027, 1, 31), date(2027, 2, 1), date(2027, 2, 5)
OTHER_IBAN = make_iban("80", "000000000000507777")
postgres_only = pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL triggers")


def clock(monkeypatch, day):
    monkeypatch.setattr(services, "_today", lambda: day)


def charity(w, monkeypatch, board_on, *, iban=OTHER_IBAN, name="Clock Charity"):
    """A recipient whose board approval is dated ``board_on`` (recorded that day)."""
    clock(monkeypatch, board_on)
    rec = recipient(w, iban=iban, approve=False, name_en=name, name_ar="جمعية الساعة")
    return board_approve(w, rec, on=board_on)


# --- C1: every entity --------------------------------------------------------------------------------------------

def test_c1_purification_rules_apply_to_an_entity_that_does_not_enforce_sharia(w):
    Entity.objects.filter(pk=w.alm.pk).update(sharia_enforced=False)
    w.alm.refresh_from_db()
    assert w.alm.sharia_enforced is False
    flagged_invoice(w, "500.00")
    _posted(w, "transfer", "500.00", JAN31)
    rec = recipient(w, approve=False)
    refused("purification_account_required", payout, w, "100.00", rec)  # no purification account: refused
    purification_account(w)
    funding = services.create_purification_transfer(w.alm, user=w.accountant, kind="funding", date=JAN31,
                                                    amount="500.00", bank_account="1100")
    other = make_user("accountant2")
    member(other, w.alm, "accountant")
    with pytest.raises(access.AccessDenied):  # C2 holds here too: the CFO approves the funding
        services.post_purification_transfer(funding, user=other)
    services.post_purification_transfer(funding, user=w.cfo)
    refused("purification_recipient_not_approved", payout, w, "100.00", rec)  # C5: no board approval yet
    board_approve(w, rec)
    paid = services.post_purification_transfer(payout(w, "100.00", rec), user=w.cfo)
    assert paid.status == "posted" and PurificationInstruction.objects.filter(payout=paid).exists()


# --- C2: CFO approval of the funding -----------------------------------------------------------------------------

def test_c2_funding_is_approved_by_the_cfo_never_by_its_recorder(w):
    flagged_invoice(w, "1000.00")
    other = make_user("accountant2")
    member(other, w.alm, "accountant")
    t = services.create_purification_transfer(w.alm, user=w.accountant, kind="transfer", date=JAN31,
                                              amount="1000.00")
    assert services.post_purification_transfer(t, user=other).status == "posted"  # E14 unchanged: a second accountant
    purification_account(w)
    f = services.create_purification_transfer(w.alm, user=w.accountant, kind="funding", date=JAN31, amount="400.00",
                                              bank_account="1100")
    for user in (other, w.viewer, make_agent("purifier")):
        with pytest.raises(access.AccessDenied):
            services.post_purification_transfer(f, user=user)
    own = services.create_purification_transfer(w.alm, user=w.cfo, kind="funding", date=JAN31, amount="100.00",
                                                bank_account="1100")
    with pytest.raises(access.AccessDenied) as exc:  # the CFO who recorded it does not approve it (A)
        services.post_purification_transfer(own, user=w.cfo)
    assert exc.value.code == "self_approval"
    posted = services.post_purification_transfer(f, user=w.cfo)
    assert (posted.status, posted.posted_by) == ("posted", w.cfo)
    assert services.post_purification_transfer(own, user=managing_partner(w)).posted_by == w.mp  # fallback (OQ-1)
    assert cash(w) == D("500.00")


def test_c2_the_seed_gives_the_funding_approval_to_the_cfo_and_the_managing_partner_only():
    holders = {code for code, row in ROLE_SEED.items() if "invoicing.approve_purification_funding" in row[5]}
    assert holders == {"approver", "managing_partner"}
    from core.access import catalogue
    perm = catalogue.get("invoicing.approve_purification_funding")
    assert perm.is_approval and perm.posts


# --- C3, C5: board approval, 12 months, alerts, reactivation -----------------------------------------------------

def test_c5_the_board_approval_is_recorded_with_the_approval(w):
    rec = recipient(w, approve=False)
    refused("board_approval_required", services.approve_charity_recipient, rec, user=managing_partner(w))
    refused("board_approval_required", services.approve_charity_recipient, rec, user=w.mp,
            board_minute_reference=" ", board_approved_on=date(2026, 9, 1))
    today = services._today()
    for minute, on in (("M-1", today.replace(year=today.year + 1)),  # in the future
                       ("M-1", today.replace(year=today.year - 2)),  # older than 12 months
                       ("M" * 101, today), ("M-1", "2026-09-01")):
        refused("board_approval_invalid", services.approve_charity_recipient, rec, user=w.mp,
                board_minute_reference=minute, board_approved_on=on)
    assert not CharityBoardApproval.objects.exists()
    with pytest.raises(access.AccessDenied):  # the CFO does not record the board's approval
        services.approve_charity_recipient(rec, user=w.cfo, board_minute_reference="M-1", board_approved_on=today)
    approved = board_approve(w, rec, minute="BOARD-2026-07", on=today)
    board = approved.board_approval
    assert (board.kind, board.minute_reference, board.board_approved_on, board.recorded_by) == (
        "approval", "BOARD-2026-07", today, w.mp)
    assert approved.approval_expires_on == services._plus_years(today, 1) == board.expires_on
    row = services.charity_recipient_row(approved, user=w.mp)
    assert (row["board_minute_reference"], row["approval_expires_on"], row["expired"]) == (
        "BOARD-2026-07", board.expires_on, False)
    assert list(services.charity_board_approvals(approved, user=w.viewer)) == [board]


def test_c3_no_payout_once_the_approval_has_expired_and_re_approval_resets_the_clock(w, monkeypatch):
    ready(w, "1000.00")
    rec = charity(w, monkeypatch, date(2026, 2, 1))  # expires 2027-02-01
    assert rec.approval_expires_on == FEB1
    draft = payout(w, "100.00", rec, on=JAN31)  # recorded while valid
    text = refused("charity_recipient_expired", payout, w, "100.00", rec, on=FEB1)  # dated on the expiry day
    assert "2027-02-01" in text
    clock(monkeypatch, date(2027, 2, 2))  # the approval has run out: the draft is not posted any more
    refused("charity_recipient_expired", services.post_purification_transfer, draft, user=w.cfo)
    assert rec not in services.charity_recipients(w.alm, user=w.viewer, usable_only=True)
    assert services.charity_recipient_row(rec, user=w.viewer)["expired"] is True
    refused("board_approval_invalid", board_approve, w, rec, on=date(2026, 2, 1))  # not a newer board approval
    with pytest.raises(access.AccessDenied):  # only the approver role re-approves
        services.approve_charity_recipient(rec, user=w.accountant, board_minute_reference="M-2",
                                           board_approved_on=date(2027, 2, 2))
    again = board_approve(w, rec, minute="BOARD-2027-02", on=date(2027, 2, 2))
    assert again.approval_expires_on == date(2028, 2, 2) and again.board_approval.kind == "reapproval"
    assert again.board_approval.previous_expires_on == FEB1
    assert services.post_purification_transfer(draft, user=w.cfo).status == "posted"
    assert [b.kind for b in services.charity_board_approvals(rec, user=w.mp)] == ["reapproval", "approval"]


def test_c3_a_twelve_month_approval_from_29_february_ends_on_28_february():
    assert services._plus_years(date(2028, 2, 29), services.CHARITY_APPROVAL_YEARS) == date(2029, 2, 28)


def test_c3_expiry_alerts_go_out_30_and_7_days_before_once_each(w, monkeypatch):
    rec = charity(w, monkeypatch, date(2026, 3, 1))  # expires 2027-03-01
    mp = managing_partner(w)
    assert services.send_charity_recipient_alerts(today=date(2027, 1, 29)) == 0  # 31 days left
    assert services.send_charity_recipient_alerts(today=date(2027, 1, 30)) == 1  # 30 days left
    alerts = CharityRecipientAlert.objects.filter(charity_recipient=rec)
    assert {(a.addressee, a.days_before) for a in alerts} == {(w.accountant, 30), (mp, 30)}
    alert = alerts.get(addressee=w.accountant)
    assert "Clock Charity" in alert.text_en and "2027-03-01" in alert.text_en and "30 days" in alert.text_en
    assert "جمعية الساعة" in alert.text_ar and rec.iban_last4 not in alert.text_en + alert.text_ar
    assert services.send_charity_recipient_alerts(today=date(2027, 1, 30)) == 0  # idempotent
    assert services.send_charity_recipient_alerts(today=date(2027, 2, 10)) == 0
    assert services.send_charity_recipient_alerts(today=date(2027, 2, 22)) == 1  # 7 days left
    assert services.send_charity_recipient_alerts(today=date(2027, 2, 25)) == 0
    assert services.send_charity_recipient_alerts(today=date(2027, 3, 1)) == 0  # expired: no "before" alert
    assert sorted(a.days_before for a in alerts.filter(addressee=mp)) == [7, 30]
    assert [a.days_before for a in services.charity_recipient_alerts(w.alm, user=mp)] == [7, 30]
    assert not CharityRecipientAlert.objects.filter(addressee__in=[w.cfo, w.viewer, w.clerk]).exists()


def test_c3_a_late_first_run_sends_only_the_most_urgent_alert(w, monkeypatch):
    rec = charity(w, monkeypatch, date(2026, 3, 1))
    assert services.send_charity_recipient_alerts(today=date(2027, 2, 26)) == 1
    assert set(CharityRecipientAlert.objects.filter(charity_recipient=rec).values_list("days_before", flat=True)) == {7}
    assert services.send_charity_recipient_alerts(today=date(2027, 2, 27)) == 0
    from django.core.management import call_command
    call_command("invoicing_charity_recipient_alerts")  # the scheduled job runs as a command


def test_c3_reactivation_by_a_second_person_without_re_entry(w):
    _, rec = ready(w, "1000.00")
    services.deactivate_charity_recipient(rec, user=w.accountant, reason="Charity paused its programme")
    refused("purification_recipient_not_approved", payout, w, "100.00", rec)
    refused("recipient_inactive", board_approve, w, rec)  # nobody asked for it
    refused("reason_required", services.request_charity_recipient_reactivation, rec, user=w.accountant, reason=" ")
    with pytest.raises(access.AccessDenied):
        services.request_charity_recipient_reactivation(rec, user=make_agent("purifier"), reason="again")
    both = make_user("accountant_and_partner")
    member(both, w.alm, "accountant")
    member(both, w.alm, "managing_partner")
    services.request_charity_recipient_reactivation(rec, user=both, reason="Programme resumed")
    refused("already_requested", services.request_charity_recipient_reactivation, rec, user=w.accountant,
            reason="again")
    with pytest.raises(access.AccessDenied) as exc:  # the requester does not approve their own request
        board_approve(w, rec, user=both)
    assert exc.value.code == "self_approval"
    from datetime import timedelta
    refused("board_approval_invalid", board_approve, w, rec, on=services._today() - timedelta(days=1))  # pre-dates it
    back = board_approve(w, rec, minute="BOARD-2026-10")
    stored = CharityRecipient.objects.get(pk=rec.pk)
    assert (back.pk, stored.is_active, stored.iban_hash, stored.name_en) == (rec.pk, True, rec.iban_hash, rec.name_en)
    assert stored.deactivated_by_id is None and stored.reactivation_requested_by_id is None
    board = stored.board_approval
    assert (board.kind, board.requested_by, board.request_reason, board.ended_deactivation_by,
            board.ended_deactivation_reason) == ("reactivation", both, "Programme resumed", w.accountant,
                                                 "Charity paused its programme")
    refused("recipient_active", services.request_charity_recipient_reactivation, rec, user=w.accountant,
            reason="again")
    assert services.post_purification_transfer(payout(w, "100.00", rec), user=w.cfo).recipient == stored


def test_c3_reactivation_refused_while_the_same_iban_is_active_again(w):
    rec = recipient(w)
    services.deactivate_charity_recipient(rec, user=w.accountant, reason="stop")
    recipient(w, name_en="Same IBAN again")  # added again by hand
    refused("charity_recipient_invalid", services.request_charity_recipient_reactivation, rec, user=w.accountant,
            reason="again")


def test_c3_board_approvals_and_recipient_dates_never_change_outside_the_service(w):
    rec = recipient(w)
    board = rec.board_approval
    board.minute_reference = "FORGED"
    with pytest.raises(InvoicingError):
        board._service_write = True
        board.save()
    with pytest.raises(InvoicingError):
        board.delete()
    with pytest.raises(InvoicingError):
        CharityBoardApproval.objects.filter(pk=board.pk).update(minute_reference="FORGED")
    stored = CharityRecipient.objects.get(pk=rec.pk)
    stored.approval_expires_on = date(2099, 1, 1)  # a later expiry without a board approval
    stored._service_write = True
    with pytest.raises(InvoicingError) as exc:
        stored.save()
    assert exc.value.code == "recipient_immutable"


@postgres_only
def test_c3_db_refuses_dates_and_board_rows_changed_by_sql(w):
    rec = recipient(w)

    def db_refused(sql, params):
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(sql, params)

    db_refused("UPDATE invoicing_charityrecipient SET approval_expires_on = '2099-01-01' WHERE id = %s", [rec.pk])
    db_refused("UPDATE invoicing_charityrecipient SET approved_at = now() WHERE id = %s", [rec.pk])
    db_refused("UPDATE invoicing_charityboardapproval SET minute_reference = 'X' WHERE id = %s", [rec.board_approval_id])
    db_refused("DELETE FROM invoicing_charityboardapproval WHERE id = %s", [rec.board_approval_id])
    db_refused("UPDATE invoicing_charityboardapproval SET expires_on = '2099-01-01' WHERE id = %s",
               [rec.board_approval_id])


@postgres_only
def test_c3_db_refuses_posting_a_payout_to_an_expired_recipient(w, monkeypatch):
    ready(w, "1000.00")
    rec = charity(w, monkeypatch, date(2026, 2, 1))  # expires 2027-02-01
    draft = payout(w, "100.00", rec, on=JAN31)
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:  # dated after expiry
        cursor.execute("UPDATE invoicing_purificationtransfer SET date = '2027-02-02', status = 'posted', "
                       "number = 'X-1', posted_by_id = %s WHERE id = %s", [w.cfo.pk, draft.pk])


# --- C4: instructions marked exported / sent by an accounting user ------------------------------------------------

def test_c4_an_accountant_marks_the_instruction_exported_then_sent(w):
    _, rec = ready(w, "1000.00")
    p = _posted(w, "payout", "100.00", FEB5, recipient=rec)
    instruction = PurificationInstruction.objects.get(payout=p)
    for user in (w.viewer, w.cfo, make_agent("purifier")):
        with pytest.raises(access.AccessDenied):
            services.mark_purification_instruction_exported(instruction, user=user)
    done = services.mark_purification_instruction_exported(instruction, user=w.accountant)
    assert (done.status, done.exported_by) == ("exported", w.accountant) and done.exported_at is not None
    refused("instruction_status", services.mark_purification_instruction_exported, instruction, user=w.accountant)
    sent = services.mark_purification_instruction_sent(instruction, user=w.accountant, bank_reference="RJ-0001")
    assert (sent.status, sent.sent_by, sent.bank_reference) == ("sent", w.accountant, "RJ-0001")
    row = services.purification_instruction_row(sent, user=w.viewer)
    assert row["status"] == "sent" and row["bank_reference"] == "RJ-0001" and row["sent_at"] is not None
    refused("instruction_status", services.mark_purification_instruction_sent, instruction, user=w.accountant)
    refused("instruction_exported", services.reverse_purification_transfer, p, user=w.accountant, reason="wrong")


def test_c4_a_pending_instruction_may_be_marked_sent_directly_but_not_a_cancelled_one(w):
    _, rec = ready(w, "1000.00")
    first = _posted(w, "payout", "100.00", FEB5, recipient=rec)
    second = _posted(w, "payout", "50.00", FEB5, recipient=rec)
    sent = services.mark_purification_instruction_sent(PurificationInstruction.objects.get(payout=first),
                                                       user=w.accountant)
    assert sent.status == "sent" and sent.exported_at is None
    services.reverse_purification_transfer(second, user=w.accountant, reason="Wrong amount")
    cancelled = PurificationInstruction.objects.get(payout=second)
    assert cancelled.status == "cancelled"
    refused("instruction_status", services.mark_purification_instruction_sent, cancelled, user=w.accountant)
    stored = PurificationInstruction.objects.get(pk=sent.pk)
    stored.status = "exported"  # never backwards, and never without who and when
    stored._service_write = True
    with pytest.raises(InvoicingError):
        stored.save()


@postgres_only
def test_c4_db_records_who_marked_the_instruction(w):
    _, rec = ready(w, "1000.00")
    p = _posted(w, "payout", "100.00", FEB5, recipient=rec)
    pk = PurificationInstruction.objects.get(payout=p).pk

    def db_refused(sql, params):
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(sql, params)

    db_refused("UPDATE invoicing_purificationinstruction SET status = 'sent' WHERE id = %s", [pk])  # who? when?
    db_refused("UPDATE invoicing_purificationinstruction SET status = 'exported' WHERE id = %s", [pk])
    with connection.cursor() as cursor:
        cursor.execute("UPDATE invoicing_purificationinstruction SET status = 'sent', sent_by_id = %s, "
                       "sent_at = now() WHERE id = %s", [w.accountant.pk, pk])
    db_refused("UPDATE invoicing_purificationinstruction SET status = 'pending' WHERE id = %s", [pk])


# --- C5: any return on the purification account goes to charity --------------------------------------------------

def test_c5_profit_on_the_purification_account_is_owed_to_charity_and_payable_without_funding(w):
    account, rec = ready(w, "1000.00")
    before = services.purification_position(w.alm, user=w.accountant)
    profit = bank_txn(w, account, "islamic_profit", "50.00", FEB1)
    lines = sorted((l.account.code, l.debit, l.credit) for l in profit.journal_entry.lines.select_related("account"))
    assert lines == [("1140", D("50.00"), D("0")), ("2600", D("0"), D("50.00"))]
    assert balance(w, "4600") == D("0")
    after = services.purification_position(w.alm, user=w.accountant)
    assert after["payable"] == before["payable"] + D("50.00")
    assert after["purification_account_balance"] == before["purification_account_balance"] + D("50.00")
    assert after["to_fund"] == before["to_fund"] == D("0")  # the return does not lower the funding room any more
    refused("purification_invalid", services.create_purification_transfer, w.alm, user=w.accountant, kind="funding",
            date=FEB5, amount="1.00", bank_account="1100")
    paid = services.post_purification_transfer(payout(w, "1050.00", rec), user=w.cfo)
    assert paid.status == "posted" and balance(w, "2600") == cash(w) == D("0")
    with pytest.raises((BankingError, LedgerError, InvoicingError)):  # its reversal would empty 1140 below zero
        banking.reverse_bank_transaction(profit, user=w.cfo, reason="wrong")


# --- C6: restricted cash, no way back -----------------------------------------------------------------------------

def test_c6_1140_is_presented_as_restricted_cash_with_the_ias7_note():
    spec = restricted.get("1140")
    assert spec.restricted_cash is True and restricted.restricted_cash_codes() == frozenset({"1140"})
    assert "IAS 7" in spec.presentation_en and "cannot be returned to the company" in spec.presentation_en
    assert "excluded from cash and cash equivalents" in spec.presentation_en
    assert "النقد وما في حكمه" in spec.presentation_ar and "لا تعود إلى الشركة" in spec.presentation_ar


def test_c6_restricted_cash_needs_its_bilingual_note(monkeypatch):
    from django.apps import apps
    monkeypatch.setattr(apps, "ready", False)
    with pytest.raises(restricted.RestrictedAccountError):
        restricted.restrict("1999", name_en="X", name_ar="س", posted_by_en="x", posted_by_ar="س",
                            restricted_cash=True, presentation_en="Restricted", presentation_ar="")
    assert restricted.get("1999") is None


def test_c6_money_in_1140_flows_back_only_by_reversing_an_erroneous_funding(w):
    flagged_invoice(w, "1000.00")
    _posted(w, "transfer", "1000.00", JAN31)
    account = purification_account(w)
    funding = _posted(w, "funding", "1000.00", JAN31, bank_account="1100")
    assert account.gl_account.code == "1140"
    # no transfer out of the purification account: banking/test_purification_restrictions.py (own and intercompany)
    with pytest.raises(LedgerError) as exc:  # no manual journal back to the operating account
        ledger.create_entry(w.alm, FEB1, "Back / إعادة", [{"account": "1100", "debit": D("100.00")},
                                                            {"account": "1140", "credit": D("100.00")}],
                            user=w.accountant)
    assert "1140" in " ".join(exc.value.messages)
    refused("reason_required", services.reverse_purification_transfer, funding, user=w.accountant, reason=" ")
    reversed_ = services.reverse_purification_transfer(funding, user=w.accountant,
                                                       reason="Error: funded from the wrong account")
    entry = JournalEntry.objects.get(pk=reversed_.reversal_entry_id)
    assert entry.reversal_of_id == funding.journal_entry_id and reversed_.reversal_reason.startswith("Error")
    assert cash(w) == D("0") and reversed_.status == "reversed"


# --- C7: 1140 approved -------------------------------------------------------------------------------------------

def test_c7_1140_is_no_longer_marked_proposed_in_the_chart_template():
    from ledger.coa_template import TEMPLATE
    row = next(r for r in TEMPLATE if r[0] == "1140")
    assert row[4].get("parent") == "1100" and row[4].get("monetary") is True
    source = Path(__file__).resolve().parent.parent.joinpath("ledger", "coa_template.py").read_text().splitlines()
    at = next(i for i, line in enumerate(source) if '("1140"' in line)
    comment = []
    for line in reversed(source[:at]):
        if not line.strip().startswith("#"):
            break
        comment.append(line)
    assert comment and not any("PROPOSED" in line for line in comment) and any("Approved" in line for line in comment)
    assert "proposed" not in {f.name for f in Account._meta.get_fields()}  # nothing stored: no data migration


def test_purification_transfer_kinds_are_unchanged():
    assert set(PurificationTransfer.Kind.values) == {"transfer", "funding", "payout"}
