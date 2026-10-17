"""Bank accounts (banking R3 to R8; AT-1, AT-2). Synthetic IBANs only."""
import re
from datetime import date
from decimal import Decimal as D

import pytest

from banking import iban as iban_rules
from banking import services
from banking.conftest import IBANS, open_account
from banking.models import Bank, BankAccount, BankingError, BankingEvent
from banking.testing import make_iban, mt940
from core import access
from core.access.models import AccessLog, Role, RolePermission
from core.access.testing import make_user, member
from ledger.models import Account

ARABIC = re.compile(r"[؀-ۿ]")


def draft(w, **overrides):
    fields = dict(code="RIYAD-SAR", name_en="Riyad SAR", name_ar="الرياض ريال", bank="riyad", currency="SAR",
                  gl_account=w.gl.riyad, sharia_basis="conventional", statement_format="mt940",
                  opening_statement_date=date(2027, 1, 1), opening_statement_balance=D("100000.00"),
                  active_from=date(2027, 1, 1), iban=IBANS["RIYAD-SAR"])
    fields.update(overrides)
    return services.create_bank_account(w.alm, user=fields.pop("user", w.a), **fields)


def refused(w, **overrides):
    with pytest.raises(BankingError) as exc:
        draft(w, **overrides)
    text = " ".join(exc.value.messages)
    assert ARABIC.search(text), text  # every refusal is bilingual (R3, R46)
    return text


# --- R3: IBAN validation ----------------------------------------------------------------------------------------

def test_r3_iban_checks_are_iso13616_with_the_saudi_structure():
    good = make_iban("20", "000000000000101234")
    assert len(good) == 24 and iban_rules.problems(good, country="SA", bank_code="20") == []
    wrong_check = good[:2] + ("00" if good[2:4] != "00" else "01") + good[4:]
    assert iban_rules.problems(wrong_check, country="SA")
    assert iban_rules.problems(good[:-1], country="SA")  # 23 characters
    assert iban_rules.problems(good.lower().replace("sa", "SA"), country="SA") == []  # stored upper case
    assert iban_rules.problems("SA12 34*", country="SA")
    assert iban_rules.problems(good, country="SA", bank_code="80")  # another bank's code
    assert iban_rules.masked("SA", "1234") == "SA•• •••• … 1234"


def test_at1_valid_draft_and_each_invalid_case_refused(w):
    good = IBANS["RIYAD-SAR"]
    wrong_check = good[:2] + ("00" if good[2:4] != "00" else "01") + good[4:]
    assert "check digits" in refused(w, iban=wrong_check)
    assert "24 characters" in refused(w, iban=good[:-1])
    assert "Positions 5-6" in refused(w, iban=IBANS["RAJHI-SAR"])  # an Al Rajhi IBAN at Riyad Bank
    assert "another entity" in refused(w, gl_account=w.gl.sub_alinma)
    assert "Sharia-prohibited" in refused(w, gl_account=Account.objects.get(entity=w.alm, code="2410"))
    assert "sub-account of 1100" in refused(w, gl_account=Account.objects.get(entity=w.alm, code="1200"))
    assert "USD" in refused(w, gl_account=w.gl.snb)  # R6: a USD sub-account for a SAR account
    both = make_user("a_and_approver")  # holds accountant and approver: still never approves own draft (S!)
    member(both, w.alm, "accountant")
    member(both, w.alm, "approver")
    account = draft(w, user=both)
    assert account.status == "draft" and account.iban == good and account.iban_last4 == good[-4:]
    assert BankingEvent.objects.filter(event="bank_account_created", object_id=account.pk).exists()
    with pytest.raises(access.AccessDenied) as exc:  # R7: the creator does not approve
        services.approve_bank_account(account, user=both)
    assert exc.value.code == "self_approval"
    with pytest.raises(access.AccessDenied) as exc:  # an accountant has no approval right at all
        services.approve_bank_account(account, user=w.a)
    assert exc.value.code == "no_membership"
    with pytest.raises(BankingError, match="not active"):  # R8: no import before activation
        services.import_statement(account, b":20:X", user=w.a)
    assert BankingEvent.objects.filter(event="import_refused", object_id=account.pk).exists()
    active = services.approve_bank_account(account, user=w.p)
    assert active.status == "active" and active.approved_by == w.p
    assert BankingEvent.objects.filter(event="bank_account_activated", object_id=account.pk, actor=w.p).exists()
    assert "already used" in refused(w, code="RIYAD-SAR-2", iban=make_iban("20", "000000000000199999"))  # GL 1101 used


def test_r3_unverified_bank_code_is_a_warning_until_verified(w):
    Bank.objects.filter(code="riyad").update(verified=False)
    account = draft(w, iban=IBANS["RAJHI-SAR"])  # wrong bank code, but the code is not verified yet (OQ-4)
    assert account.status == "draft" and any("Positions 5-6" in x for x in account.warnings)
    Bank.objects.filter(code="riyad").update(verified=True)


def test_r3_one_iban_belongs_to_one_entity(w):
    draft(w)
    with pytest.raises(BankingError, match="already registered"):
        services.create_bank_account(
            w.sub, user=w.s, code="X", name_en="x", name_ar="س", bank="riyad", currency="SAR",
            gl_account=w.gl.sub_alinma, sharia_basis="islamic", statement_format="mt940",
            opening_statement_date=date(2027, 1, 1), opening_statement_balance=D("0"), active_from=date(2027, 1, 1),
            iban=IBANS["RIYAD-SAR"])


def test_r3_non_iban_country_uses_the_account_number(w):
    foreign = Bank.objects.create(code="testus", name_en="Test US Bank", name_ar="بنك أمريكي تجريبي", country="US",
                                  verified=True)
    gl = Account.objects.create(entity=w.alm, code="1109", name_en="US", name_ar="أمريكي", type="asset",
                                parent=Account.objects.get(entity=w.alm, code="1100"), currency_id="USD", is_monetary=True)
    with pytest.raises(BankingError, match="account number is required"):
        draft(w, code="US-1", bank=foreign, currency="USD", gl_account=gl, iban="")
    account = draft(w, code="US-1", bank=foreign, currency="USD", gl_account=gl, iban="", account_number="12345678")
    assert account.masked_number == "•••• 5678" and account.account_number == "12345678"


def test_r5_r6_gl_without_currency_is_set_at_activation(w):
    gl = Account.objects.create(entity=w.alm, code="1105", name_en="New bank", name_ar="بنك جديد", type="asset",
                                parent=Account.objects.get(entity=w.alm, code="1100"), is_monetary=True)
    account = draft(w, code="NEW", gl_account=gl)
    services.approve_bank_account(account, user=w.p)
    gl.refresh_from_db()
    assert gl.currency_id == "SAR"


def test_floats_are_refused_for_money(w):
    with pytest.raises(BankingError):
        draft(w, opening_statement_balance=100000.0)
    with pytest.raises(BankingError):
        draft(w, opening_statement_balance=D("1.234"))  # SAR has 2 decimals


def test_r7_changes_of_an_active_account_need_an_approved_change_request(w, accounts):
    account = accounts.riyad
    with pytest.raises(BankingError, match="change request"):
        services.update_bank_account(account, user=w.a, iban=make_iban("20", "000000000000188888"))
    services.update_bank_account(account, user=w.a, name_en="Riyad main")  # names change directly, logged
    assert BankingEvent.objects.filter(event="bank_account_changed", object_id=account.pk).exists()
    change = services.propose_bank_account_change(account, user=w.a, reason="New IBAN from the bank",
                                                  iban=make_iban("20", "000000000000188888"))
    with pytest.raises(access.AccessDenied):
        services.approve_bank_account_change(change, user=w.a)  # the requester does not approve
    services.approve_bank_account_change(change, user=w.p)
    account.refresh_from_db()
    assert account.iban.endswith("8888")
    services.import_statement(account, mt940(statement_id="S1", sequence=1, currency="SAR", opening="100000.00",
                                             opening_date=date(2027, 1, 1), closing_date=date(2027, 1, 2), lines=[]),
                              user=w.a)
    with pytest.raises(BankingError, match="cannot change"):  # R7: currency and GL fixed once a statement exists
        services.propose_bank_account_change(account, user=w.a, reason="x", gl_account=w.gl.rajhi)


def test_r8_documents_refused_outside_the_active_dates(w, accounts):
    with pytest.raises(BankingError, match="outside the active dates"):
        services.create_bank_transaction(accounts.riyad, user=w.a, kind="bank_charge", amount=D("5.00"),
                                         date=date(2026, 12, 31))


# --- R4 / AT-2: masking --------------------------------------------------------------------------------------------

def test_at2_iban_masked_everywhere_and_unmasked_views_logged(w, accounts):
    account = accounts.riyad
    full = IBANS["RIYAD-SAR"]
    last4 = full[-4:]
    no_iban = Role.objects.create(code="accountant_no_iban", name_en="Accountant without IBAN view",
                                  name_ar="محاسب دون عرض الآيبان", scope="entity", kind="human")
    for code in ("banking.view", "banking.import_statement"):
        RolePermission.objects.create(role=no_iban, codename=code)
    a2 = make_user("a2_variant")
    member(a2, w.alm, "accountant_no_iban")
    for user in (w.v, a2):
        row = services.bank_account_detail(account, user=user)
        assert row["number"] == f"SA•• •••• … {last4}" and "iban" not in row
        with pytest.raises(access.AccessDenied):
            services.reveal_iban(account, user=user)
    rows = [services.account_row(a, user=w.v) for a in services.bank_accounts(w.alm, user=w.v)]
    assert all(full not in str(r) for r in rows)
    before = AccessLog.objects.filter(event="personal_data_viewed").count()
    detail = services.bank_account_detail(account, user=w.a)
    assert detail["iban"] == full
    assert BankingEvent.objects.filter(event="iban_viewed", actor=w.a, object_id=account.pk).exists()
    assert AccessLog.objects.filter(event="personal_data_viewed").count() == before + 1
    assert full not in str(account) and last4 in str(account)  # admin labels use the masked form
    with pytest.raises(BankingError) as exc:  # a refused import's message never shows the IBAN
        services.import_statement(account, mt940(statement_id="X", sequence=1, currency="SAR", opening="1.00",
                                                 opening_date=date(2027, 1, 1), closing_date=date(2027, 1, 2),
                                                 lines=[], account=make_iban("80", "000000000000177777")), user=w.a)
    assert full not in str(exc.value.messages)
    for event in BankingEvent.objects.all():
        assert full not in str(event.details) and full[4:] not in str(event.details)
    for log in AccessLog.objects.all():
        assert full not in str(log.details)
