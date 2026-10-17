"""The purification account (invoicing R54 amendment D31b; banking R3, R5 to R7). Synthetic IBANs only.

One active SAR bank account per entity, Saudi IBAN (SA + 22 digits, valid mod-97), on GL 1140; GL 1140 is kept for
it; designating and changing it go through the R7 approval by another person.
"""
import re
from datetime import date
from decimal import Decimal as D

import pytest
from django.db import IntegrityError, connection, transaction

from banking import iban as iban_rules
from banking import services
from banking.conftest import CUTOVER, journal, postgres_only
from banking.models import Bank, BankAccount, BankAccountChange, BankingError, BankingEvent
from banking.testing import make_iban
from core import access
from core.access.testing import make_agent
from ledger.models import Account

pytestmark = pytest.mark.django_db
ARABIC = re.compile(r"[؀-ۿ]")
PURIFY_IBAN = make_iban("20", "000000000000401111")


def gl1140(entity):
    return Account.objects.get(entity=entity, code="1140")


def purification_draft(w, entity=None, **overrides):
    entity = entity or w.alm
    fields = dict(code="RIYAD-PUR", name_en="Purification account", name_ar="حساب التطهير", bank="riyad",
                  currency="SAR", gl_account=gl1140(entity), sharia_basis="islamic", statement_format="mt940",
                  opening_statement_date=date(2027, 1, 1), opening_statement_balance=D("0.00"),
                  active_from=date(2027, 1, 1), iban=PURIFY_IBAN, is_purification_account=True)
    fields.update(overrides)
    user = fields.pop("user", w.a if entity == w.alm else w.users[entity.code]["acc"])
    return services.create_bank_account(entity, user=user, **fields)


def refused(w, code="purification_account_invalid", code_=None, **overrides):
    with pytest.raises(BankingError) as exc:
        purification_draft(w, **overrides, **({"code": code_} if code_ else {}))
    text = " ".join(exc.value.messages)
    assert exc.value.code == code, (exc.value.code, text)
    assert ARABIC.search(text), text
    return text


def test_saudi_iban_rule_is_sa_plus_22_digits_with_mod97():
    assert iban_rules.saudi_problems(PURIFY_IBAN) == []
    assert iban_rules.saudi_problems(" " + PURIFY_IBAN.lower() + " ") == []  # normalised
    wrong = PURIFY_IBAN[:-1] + str((int(PURIFY_IBAN[-1]) + 1) % 10)
    assert "check digits" in iban_rules.saudi_problems(wrong)[0]
    assert "check digits" in iban_rules.saudi_problems("SA00" + PURIFY_IBAN[4:])[0]  # 00 is never a check value
    assert "check digits" in iban_rules.saudi_problems("SA99" + PURIFY_IBAN[4:])[0]
    assert "22 digits" in iban_rules.saudi_problems(make_iban("20", "00000000000040111A"))[0]  # letters: R3-valid only
    assert iban_rules.problems(make_iban("20", "00000000000040111A"), country="SA") == []
    assert iban_rules.saudi_problems(make_iban("20", "000000000000401111", country="AE")[:23])
    assert iban_rules.saudi_problems("")


def test_d31b_purification_account_is_drafted_then_designated_on_approval_by_another_person(w):
    account = purification_draft(w)
    assert (account.status, account.is_purification_account) == ("draft", True)
    assert services.purification_account(w.alm, user=w.a) is None  # not active yet
    with pytest.raises(access.AccessDenied) as exc:
        services.approve_bank_account(account, user=w.a)
    assert exc.value.code in {"self_approval", "no_membership"}
    active = services.approve_bank_account(account, user=w.p)
    assert services.purification_account(w.alm, user=w.v) == active
    assert gl1140(w.alm).currency_id == "SAR"  # R6: set at activation
    row = services.account_row(active, user=w.v)
    assert row["purification_account"] is True and PURIFY_IBAN not in str(row) and row["number"].endswith("1111")
    assert BankingEvent.objects.get(event="bank_account_activated", object_id=active.pk).details[
        "purification_account"] is True


def test_d31b_wrong_iban_currency_gl_bank_and_second_account_are_refused(w):
    wrong = PURIFY_IBAN[:-1] + str((int(PURIFY_IBAN[-1]) + 1) % 10)
    assert "check digits" in refused(w, code="bank_account_invalid", iban=wrong)  # R3 already refuses it  # R3 already refuses it
    assert "22 digits" in refused(w, iban=make_iban("20", "00000000000040111A"))
    assert "SAR" in refused(w, currency="USD", gl_account=gl1140(w.alm))
    assert "1140" in refused(w, gl_account=w.gl.rajhi)
    Bank.objects.create(code="foreign", name_en="Foreign Bank", name_ar="بنك أجنبي", country="AE")
    assert "Saudi bank" in refused(w, bank="foreign", iban=make_iban("033", "0000000000004011", country="AE"))
    # GL 1140 is kept for the purification account: an ordinary account cannot use it
    assert "kept for the purification account" in refused(w, is_purification_account=False)
    first = purification_draft(w)
    text = refused(w, code="purification_account_invalid", bank="alrajhi", code_="RAJHI-PUR", gl_account=w.gl.rajhi,
                   iban=make_iban("80", "000000000000402222"))  # a second purification account in ALM
    assert "already has purification account RIYAD-PUR" in text
    services.approve_bank_account(first, user=w.p)
    assert BankAccount.objects.filter(entity=w.alm, is_purification_account=True).count() == 1


def test_d31b_one_purification_account_per_entity_in_the_database(w):
    account = services.approve_bank_account(purification_draft(w), user=w.p)
    clone = BankAccount.objects.get(pk=account.pk)
    clone.pk, clone.code, clone.iban_hash, clone.status = None, "PUR-2", "x" * 64, "draft"
    clone.gl_account = w.gl.rajhi
    clone._service_write = True
    with pytest.raises(IntegrityError), transaction.atomic():
        clone.save()


def test_d31b_entity_separation_and_agents(w):
    with pytest.raises(BankingError) as exc:  # ALM cannot book its purification account on SUB's 1140
        purification_draft(w, gl_account=gl1140(w.sub))
    assert "another entity" in " ".join(exc.value.messages)
    sub_account = services.approve_bank_account(purification_draft(w, w.sub, code="SUB-PUR"), user=w.ps)
    alm_account = services.approve_bank_account(purification_draft(w, iban=make_iban("20", "000000000000403333")),
                                                user=w.p)
    assert services.purification_account(w.sub, user=w.s) == sub_account
    assert services.purification_account(w.alm, user=w.a) == alm_account
    with pytest.raises(access.AccessDenied):  # ALM's accountant has no rights in SUB
        services.purification_account(w.sub, user=w.a)
    with pytest.raises(access.AccessDenied):  # agents never draft bank accounts
        purification_draft(w, w.alm, user=w.g, code="AGENT-PUR")
    with pytest.raises(access.AccessDenied):
        purification_draft(w, w.alm, user=make_agent("purifier"), code="AGENT-PUR")


def test_d31b_iban_change_of_the_purification_account_is_approved_by_another_person_and_kept_saudi(w):
    account = services.approve_bank_account(purification_draft(w), user=w.p)
    with pytest.raises(BankingError) as exc:  # letters in the account part: valid R3, not a D31b IBAN
        services.propose_bank_account_change(account, user=w.a, reason="new account",
                                             iban=make_iban("20", "00000000000040999A"))
    assert exc.value.code == "purification_account_invalid"
    new_iban = make_iban("20", "000000000000409999")
    change = services.propose_bank_account_change(account, user=w.a, reason="new account", iban=new_iban)
    with pytest.raises(access.AccessDenied):
        services.approve_bank_account_change(change, user=w.a)
    services.approve_bank_account_change(change, user=w.p)
    account.refresh_from_db()
    assert account.iban == new_iban and account.is_purification_account
    with pytest.raises(BankingError) as exc:  # removing the designation leaves GL 1140 on an ordinary account
        services.propose_bank_account_change(account, user=w.a, reason="stop", purification_account=False)
    assert exc.value.code == "purification_account_invalid"


def test_d31b_designation_through_a_change_request_moves_the_account_to_1140(w):
    ordinary = services.create_bank_account(
        w.alm, user=w.a, code="RIYAD-NEW", name_en="New", name_ar="جديد", bank="riyad", currency="SAR",
        gl_account=w.gl.riyad, sharia_basis="islamic", statement_format="mt940",
        opening_statement_date=date(2027, 1, 1), opening_statement_balance=D("0.00"), active_from=date(2027, 1, 1),
        iban=PURIFY_IBAN)
    ordinary = services.approve_bank_account(ordinary, user=w.p)
    with pytest.raises(BankingError) as exc:  # designated but still on 1101
        services.propose_bank_account_change(ordinary, user=w.a, reason="designate", purification_account=True)
    assert "1140" in " ".join(exc.value.messages)
    change = services.propose_bank_account_change(ordinary, user=w.a, reason="designate", purification_account=True,
                                                  gl_account=gl1140(w.alm))
    assert BankAccountChange.objects.get(pk=change.pk).purification_account is True
    designated = services.approve_bank_account_change(change, user=w.p)
    assert designated.is_purification_account and designated.gl_account.code == "1140"
    assert services.purification_account(w.alm, user=w.a).pk == ordinary.pk
    with pytest.raises(BankingError) as exc:
        services.propose_bank_account_change(designated, user=w.a, reason="nothing")
    assert exc.value.code == "nothing_to_change"


def test_d31b_purification_account_with_money_is_not_closed(w):
    account = services.approve_bank_account(purification_draft(w), user=w.p)
    journal(w, w.alm, CUTOVER, [{"account": "1140", "debit": D("50.00")}, {"account": "3900", "credit": D("50.00")}])
    with pytest.raises(BankingError) as exc:
        services.close_bank_account(account, user=w.p, active_to=date(2027, 6, 30), reason="moving bank")
    assert exc.value.code == "purification_balance" and "50.00" in str(exc.value)
    journal(w, w.alm, CUTOVER, [{"account": "3900", "debit": D("50.00")}, {"account": "1140", "credit": D("50.00")}])
    closed = services.close_bank_account(account, user=w.p, active_to=date(2027, 6, 30), reason="moving bank")
    assert closed.status == "closed" and services.purification_account(w.alm, user=w.a) is None
    replacement = purification_draft(w, code="RIYAD-PUR-2", iban=make_iban("20", "000000000000404444"))
    assert services.approve_bank_account(replacement, user=w.p).is_purification_account  # 1140 is free again


@postgres_only
def test_d31b_database_refuses_raw_changes_of_the_designation(w):
    account = services.approve_bank_account(purification_draft(w), user=w.p)
    for sql in ("UPDATE banking_bankaccount SET is_purification_account = false WHERE id = %s",
                "UPDATE banking_bankaccount SET currency_id = 'USD' WHERE id = %s",
                "UPDATE banking_bankaccount SET gl_account_id = (SELECT id FROM ledger_account WHERE entity_id = "
                "banking_bankaccount.entity_id AND code = '1103') WHERE id = %s"):
        with pytest.raises(Exception, match="D31b"), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(sql, [account.pk])
    assert BankAccount.objects.get(pk=account.pk).is_purification_account
