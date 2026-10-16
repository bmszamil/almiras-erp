"""Multi-currency ledger (docs/specs/ledger-fx.md): one test per rule and acceptance test.

Fixtures: ALM (parent, SAR, Sharia enforced), LZM (child, SAR), EGY (child, EGP functional,
created only as data); template chart incl. the proposed FX accounts; 2027 monthly periods;
anonymised, illustrative rates (not real market data). All amounts Decimal.
"""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from core import access
from core.access.models import AccessLog
from core.access.testing import make_agent, make_user, member
from core.models import Currency, Entity
from ledger import services
from ledger.models import Account, ExchangeRate, JournalEntry, JournalLine, LedgerError, RevaluationRun
from ledger.services_setup import create_monthly_periods, install_chart_of_accounts

pytestmark = pytest.mark.django_db
D = Decimal


# --- fixtures and helpers ------------------------------------------------------

def setup_entity(code, consolidation="full", parent=None, currency="SAR"):
    e = Entity.objects.create(
        code=code, name_en=code, name_ar=f"كيان {code}", consolidation=consolidation, parent=parent,
        base_currency_id=currency,
    )
    install_chart_of_accounts(e)
    create_monthly_periods(e, 2027)
    return e


@pytest.fixture
def w():
    alm = setup_entity("ALM", "parent")
    lzm = setup_entity("LZM", "full", alm)
    egy = setup_entity("EGY", "full", alm, currency="EGP")
    clerk, cfo, treasury, mp = make_user("clerk"), make_user("cfo"), make_user("treasury"), make_user("mp")
    for e in (alm, lzm, egy):
        member(clerk, e, "accountant")
        member(cfo, e, "approver")
    member(treasury, alm, "group_treasury")
    member(mp, alm, "managing_partner")
    return SimpleNamespace(alm=alm, lzm=lzm, egy=egy, clerk=clerk, cfo=cfo, treasury=treasury, mp=mp)


def rate(f, t, d, r, entity=None, source="manual", rate_type="spot", **kw):
    """Fixture rate row written directly (the rate services are tested separately)."""
    return ExchangeRate.objects.create(
        entity=entity, from_currency_id=f, to_currency_id=t, date=d, rate=D(r), source=source,
        rate_type=rate_type, created_by=kw.pop("created_by", None) or make_user(), **kw,
    )


def drafting_agent(entity, name="ledger_drafter"):
    """An agent user whose role holds only ledger.create_entry and ledger.view (both Ag)."""
    from core.access.models import Role, RolePermission

    role, _ = Role.objects.get_or_create(code=f"agent_{name}", defaults=dict(
        name_en=f"Agent: {name}", name_ar=f"وكيل: {name}", scope="entity", kind="agent"))
    for codename in ("ledger.create_entry", "ledger.view"):
        RolePermission.objects.get_or_create(role=role, codename=codename)
    agent = make_agent(name)
    member(agent, entity, role.code)
    return agent


def draft(w, entity, d, lines, user=None, description="FX test"):
    return services.create_entry(entity, d, description, lines, user=user or w.clerk)


def post(w, entry, approver=None):
    approver = approver or w.cfo
    return services.post_entry(entry, access.require(approver, entry.entity, "ledger.post_entry", document=entry))


def book(w, entity, d, lines, **kw):
    return post(w, draft(w, entity, d, lines, **kw))


def reverse(w, entry, **kw):
    return services.reverse_entry(entry, access.require(w.clerk, entry.entity, "ledger.reverse_entry", document=entry), **kw)


def lines_of(entry):
    return list(JournalLine.objects.filter(entry=entry).select_related("account").order_by("pk"))


def line_on(entry, code):
    return JournalLine.objects.get(entry=entry, account__code=code)


def balance(entity, code, as_of=date(2027, 12, 31), currency=None):
    """(functional net, transaction net) of an account; debit positive."""
    qs = JournalLine.objects.filter(
        entry__entity=entity, account__code=code, entry__date__lte=as_of, entry__status__in=["posted", "reversed"]
    )
    if currency:
        qs = qs.filter(currency_id=currency)
    func = sum((l.debit - l.credit for l in qs), D("0"))
    txn = sum((l.txn_amount if l.debit > 0 else -l.txn_amount for l in qs), D("0"))
    return func, txn


def tb(w, entity, as_of):
    return {r["code"]: r["debit"] - r["credit"] for r in services.trial_balance(entity, as_of, user=w.clerk)}


def f1_invoice(w):
    """F1: EUR 10,000.00 sales invoice at 4.1000 on 2027-01-15."""
    rate("EUR", "SAR", date(2027, 1, 15), "4.1000")
    return book(w, w.alm, date(2027, 1, 15), [
        {"account": "1200", "currency": "EUR", "txn_debit": D("10000.00")},
        {"account": "4100", "currency": "EUR", "txn_credit": D("10000.00")},
    ])


def receipt(w, invoice, d, amount, entity=None, **kw):
    entity = entity or w.alm
    item = line_on(invoice, "1200")
    lines = services.fx_settlement_lines(entity, d, item, amount, "1100", user=w.clerk, **kw)
    return book(w, entity, d, lines)


def problems_text(exc):
    return " ".join(exc.value.messages)


# --- R1, R2, R39 / AT-1: the SAR-only flow is unchanged --------------------------

def test_r1_at1_create_entry_without_currency_fields_is_functional_at_rate_1(w):
    e = book(w, w.alm, date(2027, 1, 10), [{"account": "1100", "debit": 500}, {"account": "3100", "credit": "500"}])
    for line in lines_of(e):
        assert (line.currency_id, line.fx_rate, line.rate_source, line.line_kind) == ("SAR", D("1"), "functional", "normal")
        assert line.txn_amount == line.debit + line.credit == D("500")
        assert line.exchange_rate_id is None and line.settles_line_id is None


def test_r1_line_saved_without_currency_fields_defaults_to_functional(w):
    entry = JournalEntry.objects.create(entity=w.egy, date=date(2027, 1, 3), description="admin draft")
    line = JournalLine.objects.create(entry=entry, account=Account.objects.get(entity=w.egy, code="1100"), debit=D("7"))
    line.refresh_from_db()
    assert (line.currency_id, line.txn_amount, line.fx_rate) == ("EGP", D("7.000"), D("1"))


def test_r2_at13_entry_must_balance_in_functional_currency_even_if_each_currency_balances(w):
    entry = draft(w, w.alm, date(2027, 1, 10), [
        {"account": "5300", "currency": "EUR", "txn_debit": "100.00", "rate": "4.1000", "rate_reason": "bank"},
        {"account": "2100", "currency": "EUR", "txn_credit": "100.00", "rate": "4.2000", "rate_reason": "bank"},
    ])
    assert not entry.lines.filter(line_kind="rounding").exists()  # 10.00 difference is not rounding
    with pytest.raises(LedgerError, match="does not balance"):
        post(w, entry)
    assert JournalEntry.objects.get(pk=entry.pk).status == "draft"


def test_r3_at6_entry_need_not_balance_per_currency(w):
    """F6: GBP bill paid from the SAR account; balanced in SAR only."""
    rate("GBP", "SAR", date(2027, 1, 10), "4.7000")
    rate("GBP", "SAR", date(2027, 2, 10), "4.7600")
    bill = book(w, w.alm, date(2027, 1, 10), [
        {"account": "5300", "currency": "GBP", "txn_debit": "2000.00"},
        {"account": "2100", "currency": "GBP", "txn_credit": "2000.00"},
    ])
    lines = services.fx_settlement_lines(
        w.alm, date(2027, 2, 10), line_on(bill, "2100"), "2000.00", "1100", user=w.clerk,
        counter_currency="SAR", counter_amount="9520.00",
    )
    pay = book(w, w.alm, date(2027, 2, 10), lines)
    assert line_on(pay, "2100").debit == D("9400.00")
    cash = line_on(pay, "1100")
    assert (cash.currency_id, cash.txn_amount, cash.credit) == ("SAR", D("9520.000"), D("9520.00"))
    assert line_on(pay, "5750").debit == D("120.00")
    assert balance(w.alm, "2100") == (D("0"), D("0"))


# --- R4, R5 / AT-20, AT-23: functional currency -----------------------------------

def test_r4_at20_functional_currency_must_have_two_minor_units(w):
    with pytest.raises(ValidationError, match="2 minor units"):
        Entity.objects.create(code="KWT", name_en="K", name_ar="ك", parent=w.alm, base_currency_id="KWD")
    with pytest.raises(ValidationError, match="2 minor units"):
        Entity(code="JPN", name_en="J", name_ar="ي", parent=w.alm, base_currency_id="JPY").full_clean()
    assert not Entity.objects.filter(code__in=["KWT", "JPN"]).exists()


def test_r5_at23_functional_currency_locked_after_first_posting(w):
    book(w, w.alm, date(2027, 1, 10), [{"account": "1100", "debit": 1}, {"account": "3100", "credit": 1}])
    with pytest.raises(LedgerError, match="cannot change"):
        services.set_functional_currency(w.alm, "USD", user=w.mp)
    alm = Entity.objects.get(pk=w.alm.pk)
    alm.base_currency_id = "USD"
    with pytest.raises(ValidationError, match="cannot change"):
        alm.save()
    assert Entity.objects.get(pk=w.alm.pk).base_currency_id == "SAR"
    # an entity with no postings may change, by the Managing Partner only (3.6)
    with pytest.raises(access.AccessDenied):
        services.set_functional_currency(w.lzm, "USD", user=w.clerk)
    assert services.set_functional_currency(w.lzm, "USD", user=w.mp).base_currency_id == "USD"
    with pytest.raises(LedgerError, match="2 minor units"):
        services.set_functional_currency(w.lzm, "KWD", user=w.mp)


# --- R6 to R8 / AT-14, AT-15, AT-20, AT-30: line amounts ----------------------------

def test_r7_at14_foreign_line_functional_amount_must_equal_txn_times_rate(w):
    rate("EUR", "SAR", date(2027, 1, 15), "4.1000")
    entry = draft(w, w.alm, date(2027, 1, 15), [
        {"account": "1200", "currency": "EUR", "txn_debit": "100.00", "debit": "411.00"},
        {"account": "4100", "credit": "411.00"},
    ])
    with pytest.raises(LedgerError, match="should be 410.00"):
        post(w, entry)


def test_r6_at15_functional_line_needs_rate_1_and_txn_equal_to_amount(w):
    entry = draft(w, w.alm, date(2027, 1, 15), [
        {"account": "1100", "currency": "SAR", "debit": "100.00", "rate": "1.0100", "rate_reason": "typo"},
        {"account": "3100", "credit": "100.00"},
    ])
    with pytest.raises(LedgerError, match="rate 1"):
        post(w, entry)
    entry = draft(w, w.alm, date(2027, 1, 15), [
        {"account": "1100", "currency": "SAR", "debit": "100.00", "txn_debit": "101.00"},
        {"account": "3100", "credit": "100.00"},
    ])
    with pytest.raises(LedgerError, match="rate 1"):
        post(w, entry)


def test_r8_at20_decimals_per_currency_and_float_rejected(w):
    rate("KWD", "SAR", date(2027, 1, 15), "12.2000")
    rate("JPY", "SAR", date(2027, 1, 15), "0.0250")
    kwd = book(w, w.alm, date(2027, 1, 15), [
        {"account": "1200", "currency": "KWD", "txn_debit": "1.234"},
        {"account": "4100", "currency": "KWD", "txn_credit": "1.234"},
    ])
    assert line_on(kwd, "1200").txn_amount == D("1.234")
    for bad in ({"currency": "JPY", "txn_debit": "1.5"}, {"currency": "USD", "txn_debit": "1.005"}):
        with pytest.raises(LedgerError, match="decimals"):
            draft(w, w.alm, date(2027, 1, 15), [{"account": "1200", **bad}, {"account": "4100", "credit": 1}])
    with pytest.raises(LedgerError, match="float"):
        draft(w, w.alm, date(2027, 1, 15), [{"account": "1100", "debit": 0.1}, {"account": "3100", "credit": "0.10"}])
    with pytest.raises(LedgerError, match="float"):
        draft(w, w.alm, date(2027, 1, 15), [{"account": "1200", "currency": "KWD", "txn_debit": 0.1}, {"account": "4100", "credit": 1}])
    with pytest.raises(LedgerError, match="2 decimals"):
        draft(w, w.alm, date(2027, 1, 15), [{"account": "1100", "debit": "1.005"}, {"account": "3100", "credit": "1.005"}])
    with pytest.raises(LedgerError, match="not negative"):
        draft(w, w.alm, date(2027, 1, 15), [{"account": "1200", "currency": "KWD", "txn_debit": "-1"}, {"account": "4100", "credit": 1}])
    assert not JournalEntry.objects.filter(entity=w.alm).exclude(pk=kwd.pk).exists()


def test_at30_three_decimal_kwd_and_rejections_name_currency_in_both_languages(w):
    rate("KWD", "SAR", date(2027, 1, 15), "12.2000")
    e = book(w, w.alm, date(2027, 1, 15), [
        {"account": "1200", "currency": "KWD", "txn_debit": "1234.567"},
        {"account": "4100", "currency": "KWD", "txn_credit": "1234.567"},
    ])
    ar = line_on(e, "1200")
    ar.refresh_from_db()
    assert ar.txn_amount == D("1234.567") and str(ar.txn_amount) == "1234.567"
    assert ar.debit == line_on(e, "4100").credit == D("15061.72")
    for cur, amount, places in (("KWD", "1.2345", 3), ("USD", "1.005", 2), ("SAR", "10.005", 2)):
        with pytest.raises(LedgerError) as exc:
            draft(w, w.alm, date(2027, 1, 15), [
                {"account": "1200", "currency": cur, "txn_debit": amount}, {"account": "4100", "credit": 1},
            ])
        text = problems_text(exc)
        assert cur in text and f"{places} decimals" in text and "خانات عشرية" in text


def test_r40_at30_currency_with_four_minor_units_cannot_be_active_or_used(w):
    with pytest.raises(ValidationError, match="cannot be active"):
        Currency.objects.create(code="XTF", numeric_code="999", name_en="Test 4dp", name_ar="اختبار", minor_units=4)
    cur = Currency.objects.create(
        code="XTF", numeric_code="999", name_en="Test 4dp", name_ar="اختبار", minor_units=4, is_active=False
    )
    cur.is_active = True
    with pytest.raises(ValidationError, match="cannot be active"):
        cur.save()
    with pytest.raises(LedgerError):
        draft(w, w.alm, date(2027, 1, 15), [{"account": "1200", "currency": "XTF", "txn_debit": "1"}, {"account": "4100", "credit": 1}])
    with pytest.raises(LedgerError):
        services.create_rate(user=w.treasury, from_currency="XTF", to_currency="SAR", on=date(2027, 1, 15), rate="1")
    # forced in through the ORM, it is still refused at posting
    Currency.objects.filter(code="XTF").update(is_active=True)
    entry = draft(w, w.alm, date(2027, 1, 15), [{"account": "1100", "debit": 1}, {"account": "3100", "credit": 1}])
    JournalLine.objects.filter(entry=entry, account__code="1100").update(currency_id="XTF", rate_source="manual", manual_rate_reason="x")
    with pytest.raises(LedgerError, match="cannot be used"):
        post(w, entry)


def test_r9_at21_account_with_currency_takes_only_that_currency(w):
    usd_bank = Account.objects.create(entity=w.alm, code="1101", name_en="Bank USD", name_ar="بنك بالدولار", type="asset", currency_id="USD")
    rate("EUR", "SAR", date(2027, 1, 15), "4.1000")
    rate("USD", "SAR", date(2027, 1, 15), "3.7500")
    entry = draft(w, w.alm, date(2027, 1, 15), [
        {"account": usd_bank, "currency": "EUR", "txn_debit": "10.00"}, {"account": "3100", "credit": "41.00"},
    ])
    with pytest.raises(LedgerError, match="only takes USD"):
        post(w, entry)
    ok = book(w, w.alm, date(2027, 1, 15), [
        {"account": usd_bank, "currency": "USD", "txn_debit": "10.00"}, {"account": "3100", "credit": "37.50"},
    ])
    assert ok.status == "posted"


def test_r9_at22_inactive_currency_rejected_on_lines_and_rates(w):
    Currency.objects.create(code="XXX", numeric_code="999", name_en="No currency", name_ar="بلا عملة", minor_units=0, is_active=False)
    with pytest.raises(LedgerError, match="inactive"):
        draft(w, w.alm, date(2027, 1, 15), [{"account": "1200", "currency": "XXX", "txn_debit": "1"}, {"account": "4100", "credit": 1}])
    with pytest.raises(LedgerError, match="inactive"):
        services.create_rate(user=w.treasury, from_currency="XXX", to_currency="SAR", on=date(2027, 1, 15), rate="1")
    assert not JournalEntry.objects.exists() and not ExchangeRate.objects.exists()


def test_r10_at8_manual_entry_with_revaluation_line_rejected(w):
    entry = draft(w, w.alm, date(2027, 1, 31), [{"account": "5760", "debit": "200.00"}, {"account": "3100", "credit": "200.00"}])
    JournalLine.objects.create(
        entry=entry, account=Account.objects.get(entity=w.alm, code="1200"), currency_id="EUR", txn_amount=0,
        fx_rate=None, rate_source="table", line_kind="revaluation", credit=D("200.00"),
    )
    JournalLine.objects.filter(entry=entry, account__code="3100").delete()
    with pytest.raises(LedgerError, match="revalue_period"):
        post(w, entry)
    with pytest.raises(LedgerError, match="set by the ledger"):
        draft(w, w.alm, date(2027, 1, 31), [{"account": "1200", "currency": "EUR", "credit": "1.00", "line_kind": "revaluation"}])


def test_revaluation_line_db_check(w):
    entry = JournalEntry.objects.create(entity=w.alm, date=date(2027, 1, 3), description="x")
    acc = Account.objects.get(entity=w.alm, code="1200")
    with pytest.raises(IntegrityError), transaction.atomic():
        JournalLine.objects.create(entry=entry, account=acc, currency_id="EUR", txn_amount=D("5"), fx_rate=None,
                                   rate_source="table", line_kind="revaluation", debit=D("1"))
    with pytest.raises(IntegrityError), transaction.atomic():
        JournalLine.objects.create(entry=entry, account=acc, currency_id="EUR", txn_amount=D("-1"), fx_rate=D("4"),
                                   rate_source="table", debit=D("1"))


# --- R11 to R16: rates ------------------------------------------------------------------

def test_at3_f1_posts_with_rate_row_which_is_then_locked(w):
    inv = f1_invoice(w)
    ar, income = line_on(inv, "1200"), line_on(inv, "4100")
    assert (ar.currency_id, ar.txn_amount, ar.fx_rate, ar.debit) == ("EUR", D("10000.000"), D("4.1"), D("41000.00"))
    assert income.credit == D("41000.00") and ar.rate_source == "table" and ar.exchange_rate_id
    row = ar.exchange_rate
    assert row.is_locked()
    row.rate = D("4.2")
    with pytest.raises(LedgerError, match="cannot be changed"):
        row.save()
    with pytest.raises(LedgerError, match="cannot be deleted"):
        row.delete()


def test_r11_at17_corrections_are_new_rows_and_posted_lines_keep_their_rate(w):
    inv = f1_invoice(w)
    services.create_rate(user=w.treasury, from_currency="EUR", to_currency="SAR", on=date(2027, 1, 15), rate="4.1500",
                         source="file", source_reference="corrected")
    assert line_on(inv, "1200").fx_rate == D("4.1") and line_on(inv, "1200").debit == D("41000.00")
    services.close_period(w.alm.periods.get(name="2027-01"), user=w.cfo)
    from io import StringIO

    result = services.import_rates(StringIO("date,from_currency,to_currency,rate\n2027-01-20,EUR,SAR,4.1100\n"), user=w.treasury)
    assert result["created"] == 1
    assert line_on(inv, "1200").debit == D("41000.00")
    with pytest.raises(LedgerError, match="closed"):
        book(w, w.alm, date(2027, 1, 20), [
            {"account": "1200", "currency": "EUR", "txn_debit": "1.00"}, {"account": "4100", "currency": "EUR", "txn_credit": "1.00"},
        ])


def test_r12_at11_rate_age_limit_and_inverse(w):
    usd = [{"account": "1200", "currency": "USD", "txn_debit": "100.00"}, {"account": "4100", "currency": "USD", "txn_credit": "100.00"}]
    rate("USD", "SAR", date(2027, 3, 4), "3.7400")  # 6 days before: too old
    with pytest.raises(LedgerError) as exc:
        draft(w, w.alm, date(2027, 3, 10), usd)
    text = problems_text(exc)
    assert "USD->SAR" in text and "2027-03-10" in text and "لا يوجد سعر صرف" in text
    assert not JournalEntry.objects.exists()
    row = rate("USD", "SAR", date(2027, 3, 6), "3.7500")  # 4 days before: used
    e = draft(w, w.alm, date(2027, 3, 10), usd)
    assert line_on(e, "1200").exchange_rate == row and line_on(e, "1200").debit == D("375.00")
    # only SAR->USD on the date: inverse, rounded to 10 dp
    rate("SAR", "USD", date(2027, 4, 1), "0.2666666667")
    e = draft(w, w.alm, date(2027, 4, 1), usd)
    assert line_on(e, "1200").fx_rate == D("3.7499999995")


def test_r12_source_precedence_and_latest_date(w):
    rate("EUR", "SAR", date(2027, 5, 3), "4.0000", source="file")
    rate("EUR", "SAR", date(2027, 5, 4), "4.1000", source="file")
    manual = rate("EUR", "SAR", date(2027, 5, 4), "4.2000", source="manual")
    found = services.get_rate(w.alm, "EUR", "SAR", date(2027, 5, 5), user=w.clerk)
    assert found.row == manual and found.rate == D("4.2")
    assert services.get_rate(w.alm, "SAR", "SAR", date(2027, 5, 5), user=w.clerk).rate == D("1")
    with pytest.raises(access.AccessDenied):
        services.get_rate(w.alm, "EUR", "SAR", date(2027, 5, 5), user=make_user("nobody"))


def test_r14_no_rate_no_entry(w):
    with pytest.raises(LedgerError, match="No spot exchange rate GBP->SAR"):
        draft(w, w.alm, date(2027, 6, 1), [
            {"account": "1100", "debit": "10.00"}, {"account": "4100", "currency": "GBP", "txn_credit": "2.00"},
        ])
    assert not JournalEntry.objects.exists() and not JournalLine.objects.exists()


def test_r15_at12_rate_must_match_entry_date_and_rerate_fixes_it(w):
    rate("USD", "SAR", date(2027, 3, 10), "3.7500")
    rate("USD", "SAR", date(2027, 3, 20), "3.7600")
    entry = draft(w, w.alm, date(2027, 3, 10), [
        {"account": "1200", "currency": "USD", "txn_debit": "100.00"}, {"account": "4100", "currency": "USD", "txn_credit": "100.00"},
    ])
    entry.date = date(2027, 3, 20)
    entry.save()
    with pytest.raises(LedgerError, match="rate does not match entry date"):
        post(w, entry)
    services.rerate_entry(entry, user=w.clerk)
    posted = post(w, entry)
    assert posted.status == "posted" and line_on(posted, "1200").debit == D("376.00")


def test_r15_foreign_line_without_rate_rejected_at_posting(w):
    entry = draft(w, w.alm, date(2027, 1, 3), [{"account": "1100", "debit": 1}, {"account": "3100", "credit": 1}])
    JournalLine.objects.filter(entry=entry, account__code="1100").update(currency_id="USD", fx_rate=None, rate_source="table")
    with pytest.raises(LedgerError, match="no exchange rate"):
        post(w, entry)


def test_r16_at12_manual_rate_needs_reason_and_permission_and_is_flagged(w):
    rate("USD", "SAR", date(2027, 3, 10), "3.7500")
    lines = [
        {"account": "1200", "currency": "USD", "txn_debit": "100.00", "rate": "3.7700"},
        {"account": "4100", "credit": "377.00"},
    ]
    with pytest.raises(LedgerError, match="needs a reason"):
        draft(w, w.alm, date(2027, 3, 10), lines)
    lines[0]["rate_reason"] = "Rate agreed with the bank on the day"
    agent = drafting_agent(w.alm)  # holds ledger.create_entry but never ledger.use_manual_rate
    with pytest.raises(access.AccessDenied) as exc:
        draft(w, w.alm, date(2027, 3, 10), lines, user=agent)
    assert exc.value.code == "agent_forbidden" and not JournalEntry.objects.exists()
    posted = book(w, w.alm, date(2027, 3, 10), lines)
    assert line_on(posted, "1200").rate_source == "manual"
    flagged = services.fx_exceptions(w.alm, user=w.clerk)
    assert len(flagged) == 1
    assert flagged[0]["rate"] == D("3.77") and flagged[0]["table_rate"] == D("3.75")
    assert flagged[0]["deviation_pct"] == D("0.5333") and flagged[0]["reason"].startswith("Rate agreed")
    with pytest.raises(access.AccessDenied):  # the approver does not draft, let alone with a manual rate
        draft(w, w.alm, date(2027, 3, 10), lines, user=w.cfo)


# --- R17 / AT-13: rounding --------------------------------------------------------

def test_r17_at13_f9_rounding_line_added_within_tolerance(w):
    rate("EUR", "SAR", date(2027, 1, 20), "4.1235")
    e = book(w, w.alm, date(2027, 1, 20), [
        {"account": "5300", "currency": "EUR", "txn_debit": "33.33"},
        {"account": "5300", "currency": "EUR", "txn_debit": "33.33"},
        {"account": "5300", "currency": "EUR", "txn_debit": "33.34"},
        {"account": "2100", "currency": "EUR", "txn_credit": "100.00"},
    ])
    assert sorted(l.debit for l in e.lines.filter(account__code="5300")) == [D("137.44"), D("137.44"), D("137.48")]
    assert line_on(e, "2100").credit == D("412.35")
    rounding = line_on(e, "5770")
    assert (rounding.credit, rounding.currency_id, rounding.line_kind) == (D("0.01"), "SAR", "rounding")


def test_r17_at13_difference_above_tolerance_is_not_fixed(w):
    rate("EUR", "SAR", date(2027, 1, 20), "4.1000")
    entry = draft(w, w.alm, date(2027, 1, 20), [
        {"account": "5300", "currency": "EUR", "txn_debit": "50.00"},
        {"account": "5300", "currency": "EUR", "txn_debit": "50.00", "rate": "4.1010", "rate_reason": "bank"},
        {"account": "2100", "currency": "EUR", "txn_credit": "100.00"},
    ])  # 205.00 + 205.05 vs 410.00: 0.05 on 3 foreign lines (tolerance 0.03)
    assert not entry.lines.filter(line_kind="rounding").exists()
    with pytest.raises(LedgerError, match="does not balance"):
        post(w, entry)


# --- R18 / AT-4, AT-5, AT-7: settlement and realised FX ------------------------------------

def test_r18_at4_f4_receipt_realised_gain(w):
    inv = f1_invoice(w)
    rate("EUR", "SAR", date(2027, 2, 20), "4.1250")
    r = receipt(w, inv, date(2027, 2, 20), "10000.00")
    cash, ar, gain = line_on(r, "1100"), line_on(r, "1200"), line_on(r, "4700")
    assert (cash.currency_id, cash.debit, cash.fx_rate, cash.rate_source) == ("EUR", D("41250.00"), D("4.125"), "table")
    assert (ar.credit, ar.fx_rate, ar.rate_source, ar.line_kind, ar.settles_line_id) == (
        D("41000.00"), D("4.1"), "carrying", "settlement", line_on(inv, "1200").pk)
    assert (gain.credit, gain.currency_id, gain.line_kind) == (D("250.00"), "SAR", "realised_fx")
    assert balance(w.alm, "1200", currency="EUR") == (D("0"), D("0"))


def test_r18_at5_f5_payment_realised_loss(w):
    rate("GBP", "SAR", date(2027, 1, 10), "4.7000")
    rate("GBP", "SAR", date(2027, 2, 10), "4.7600")
    bill = book(w, w.alm, date(2027, 1, 10), [
        {"account": "5300", "currency": "GBP", "txn_debit": "2000.00"},
        {"account": "2100", "currency": "GBP", "txn_credit": "2000.00"},
    ])
    pay = book(w, w.alm, date(2027, 2, 10), services.fx_settlement_lines(
        w.alm, date(2027, 2, 10), line_on(bill, "2100"), "2000.00", "1100", user=w.clerk))
    assert line_on(pay, "2100").debit == D("9400.00")
    assert line_on(pay, "1100").credit == D("9520.00")
    assert line_on(pay, "5750").debit == D("120.00")
    assert not pay.lines.filter(account__code="4700").exists()


def test_r18_at7_f10_partial_settlements_leave_no_residue(w):
    inv = f1_invoice(w)
    rate("EUR", "SAR", date(2027, 2, 5), "4.1100")
    rate("EUR", "SAR", date(2027, 2, 25), "4.0900")
    first = receipt(w, inv, date(2027, 2, 5), "3333.33")
    assert line_on(first, "1200").credit == D("13666.65")  # round(3,333.33 x 4.1000)
    second = receipt(w, inv, date(2027, 2, 25), "6666.67")
    assert line_on(second, "1200").credit == D("27333.35")  # exactly what is left
    assert balance(w.alm, "1200") == (D("0"), D("0"))
    # realised FX per receipt against its own settlement rate
    assert line_on(first, "4700").credit == D("33.34")  # 13,699.99 - 13,666.65
    assert line_on(second, "5750").debit == D("66.67")  # 27,333.35 - 27,266.68 (6,666.67 x 4.0900)


def test_r18_over_settlement_and_wrong_settlements_rejected(w):
    inv = f1_invoice(w)
    rate("EUR", "SAR", date(2027, 2, 20), "4.1250")
    item = line_on(inv, "1200")
    with pytest.raises(LedgerError, match="open"):
        services.fx_settlement_lines(w.alm, date(2027, 2, 20), item, "10000.01", "1100", user=w.clerk)
    # a hand-built settlement for more than is open is refused at posting
    entry = draft(w, w.alm, date(2027, 2, 20), [
        {"account": "1100", "currency": "EUR", "txn_debit": "10001.00"},
        {"account": "1200", "currency": "EUR", "txn_credit": "10001.00", "settles_line": item, "credit": "41004.10"},
        {"account": "4700", "credit": "250.03", "line_kind": "realised_fx"},
    ])
    with pytest.raises(LedgerError, match="only 10000.000 is open"):
        post(w, entry)
    # a settlement amount that is neither carrying rate nor remaining balance
    entry = draft(w, w.alm, date(2027, 2, 20), [
        {"account": "1100", "currency": "EUR", "txn_debit": "100.00"},
        {"account": "1200", "currency": "EUR", "txn_credit": "100.00", "settles_line": item, "credit": "412.50"},
    ])
    with pytest.raises(LedgerError, match="neither the carrying rate"):
        post(w, entry)
    # settling another entity's line
    with pytest.raises(LedgerError, match="another entity"):
        services.fx_settlement_lines(w.lzm, date(2027, 2, 20), item, "1.00", "1100", user=w.clerk)


def test_f11_at18_reversal_copies_currency_fields_and_nets_to_zero(w):
    inv = f1_invoice(w)
    rate("EUR", "SAR", date(2027, 2, 20), "4.1250")
    rate("EUR", "SAR", date(2027, 2, 25), "4.2000")  # a later rate does not affect the reversal (R15 skipped)
    r = receipt(w, inv, date(2027, 2, 20), "10000.00")
    rev = reverse(w, r, date=date(2027, 2, 25))
    assert rev.status == "posted" and JournalEntry.objects.get(pk=r.pk).status == "reversed"
    for orig in lines_of(r):
        copy = rev.lines.get(account=orig.account)
        assert (copy.debit, copy.credit) == (orig.credit, orig.debit)
        for f in ("currency_id", "txn_amount", "fx_rate", "rate_source", "exchange_rate_id", "settles_line_id",
                  "line_kind", "counterparty_entity_id", "manual_rate_reason"):
            assert getattr(copy, f) == getattr(orig, f), f
    for code in ("1100", "4700"):
        assert balance(w.alm, code, as_of=date(2027, 2, 28)) == (D("0"), D("0"))
    assert balance(w.alm, "1200", as_of=date(2027, 2, 28)) == (D("41000.00"), D("10000.000"))  # invoice open again
    again = receipt(w, inv, date(2027, 2, 25), "10000.00")  # and can be settled again
    assert line_on(again, "4700").credit == D("1000.00")


# --- R19, R21 to R26 / AT-8 to AT-11: revaluation -------------------------------------

def revalue_and_post(w, entity, period_name, runner=None, approver=None):
    period = entity.periods.get(name=period_name)
    run = services.revalue_period(entity, period, user=runner or w.clerk)
    if run.entry is not None:
        grant = access.require(approver or w.cfo, entity, "ledger.post_entry", document=run.entry)
        run = services.post_revaluation(run, grant)
    return run


def test_r21_r23_at8_f2_f3_revaluation_posts_and_reverses_next_day(w):
    inv = f1_invoice(w)
    rate("EUR", "SAR", date(2027, 1, 31), "4.0800")
    run = revalue_and_post(w, w.alm, "2027-01")
    entry, rev = JournalEntry.objects.get(pk=run.entry_id), JournalEntry.objects.get(pk=run.reversal_id)
    assert (entry.date, entry.status, entry.created_by, entry.posted_by) == (date(2027, 1, 31), "reversed", w.clerk, w.cfo)
    assert (rev.date, rev.status, rev.reversal_of_id) == (date(2027, 2, 1), "posted", entry.pk)
    assert "Automatic reversal of FX revaluation" in rev.description
    loss, ar = line_on(entry, "5760"), line_on(entry, "1200")
    assert (loss.debit, loss.currency_id, loss.line_kind) == (D("200.00"), "SAR", "normal")
    assert (ar.credit, ar.currency_id, ar.txn_amount, ar.fx_rate, ar.line_kind) == (D("200.00"), "EUR", D("0"), None, "revaluation")
    assert line_on(rev, "1200").debit == D("200.00") and line_on(rev, "5760").credit == D("200.00")
    assert tb(w, w.alm, date(2027, 1, 31))["5760"] == D("200.00")
    assert tb(w, w.alm, date(2027, 1, 31))["1200"] == D("40800.00")
    # R19: after the reversal the carrying rate is the original rate again
    rate("EUR", "SAR", date(2027, 2, 20), "4.1250")
    receipt(w, inv, date(2027, 2, 20), "10000.00")
    feb = tb(w, w.alm, date(2027, 2, 28))
    assert "5760" not in feb and feb["4700"] == D("-250.00")
    assert balance(w.alm, "1200", as_of=date(2027, 2, 28)) == (D("0"), D("0"))


def test_f7_revaluation_of_a_liability_and_gains(w):
    rate("GBP", "SAR", date(2027, 1, 10), "4.7000")
    rate("USD", "SAR", date(2027, 1, 10), "3.7500")
    book(w, w.alm, date(2027, 1, 10), [
        {"account": "5300", "currency": "GBP", "txn_debit": "2000.00"},
        {"account": "2100", "currency": "GBP", "txn_credit": "2000.00"},
    ])
    book(w, w.alm, date(2027, 1, 10), [
        {"account": "1100", "currency": "USD", "txn_debit": "1000.00"},
        {"account": "3100", "credit": "3750.00"},
    ])
    rate("GBP", "SAR", date(2027, 1, 31), "4.7500")
    rate("USD", "SAR", date(2027, 1, 31), "3.7600")
    run = revalue_and_post(w, w.alm, "2027-01")
    entry = JournalEntry.objects.get(pk=run.entry_id)
    assert line_on(entry, "2100").credit == D("100.00") and line_on(entry, "2100").currency_id == "GBP"
    assert line_on(entry, "1100").debit == D("10.00")
    assert line_on(entry, "5760").debit == D("100.00") and line_on(entry, "4710").credit == D("10.00")
    assert not entry.lines.filter(account__code="5300").exists()  # expenses are not monetary


def test_r24_at9_second_run_rejected(w):
    f1_invoice(w)
    rate("EUR", "SAR", date(2027, 1, 31), "4.0800")
    revalue_and_post(w, w.alm, "2027-01")
    with pytest.raises(LedgerError, match="already revalued"):
        services.revalue_period(w.alm, w.alm.periods.get(name="2027-01"), user=w.clerk)


def test_r22_r23_r25_at10_closed_or_missing_periods_fail_atomically(w):
    f1_invoice(w)
    rate("EUR", "SAR", date(2027, 1, 31), "4.0800")
    rate("EUR", "SAR", date(2027, 12, 31), "4.0800")
    jan, feb = w.alm.periods.get(name="2027-01"), w.alm.periods.get(name="2027-02")
    feb.status = "closed"
    feb.save()
    with pytest.raises(LedgerError, match="2027-02 is closed"):
        services.revalue_period(w.alm, jan, user=w.clerk)
    with pytest.raises(LedgerError, match="No fiscal period covers 2028-01-01"):
        services.revalue_period(w.alm, w.alm.periods.get(name="2027-12"), user=w.clerk)
    feb.status = "open"
    feb.save()
    services.close_period(jan, user=w.cfo)
    with pytest.raises(LedgerError, match="2027-01 is closed"):
        services.revalue_period(w.alm, jan, user=w.clerk)
    assert not RevaluationRun.objects.exists()
    assert JournalEntry.objects.filter(entity=w.alm).count() == 1  # only F1


def test_r23_next_period_closed_after_run_fails_posting_atomically(w):
    f1_invoice(w)
    rate("EUR", "SAR", date(2027, 1, 31), "4.0800")
    run = services.revalue_period(w.alm, w.alm.periods.get(name="2027-01"), user=w.clerk)
    feb = w.alm.periods.get(name="2027-02")
    feb.status = "closed"
    feb.save()
    with pytest.raises(LedgerError, match="closed"):
        services.post_revaluation(run, access.require(w.cfo, w.alm, "ledger.post_entry", document=run.entry))
    assert JournalEntry.objects.get(pk=run.entry_id).status == "draft"
    assert RevaluationRun.objects.get(pk=run.pk).reversal_id is None


def test_r22_at11_missing_closing_rate_fails_whole_run(w):
    f1_invoice(w)
    rate("USD", "SAR", date(2027, 1, 10), "3.7500")
    book(w, w.alm, date(2027, 1, 10), [{"account": "1100", "currency": "USD", "txn_debit": "10.00"}, {"account": "3100", "credit": "37.50"}])
    rate("EUR", "SAR", date(2027, 1, 31), "4.0800")  # EUR closing exists, USD does not (Jan 10 is too old)
    with pytest.raises(LedgerError) as exc:
        services.revalue_period(w.alm, w.alm.periods.get(name="2027-01"), user=w.clerk)
    assert "USD->SAR" in problems_text(exc) and "2027-01-31" in problems_text(exc)
    assert not RevaluationRun.objects.exists() and JournalEntry.objects.count() == 2


def test_r24_run_without_differences_records_run_and_posts_nothing(w):
    book(w, w.alm, date(2027, 1, 10), [{"account": "1100", "debit": 5}, {"account": "3100", "credit": 5}])
    run = services.revalue_period(w.alm, w.alm.periods.get(name="2027-01"), user=w.clerk)
    assert run.entry_id is None and JournalEntry.objects.count() == 1
    with pytest.raises(LedgerError, match="nothing to post"):
        services.post_revaluation(run, access.require(w.cfo, w.alm, "ledger.post_entry", document=JournalEntry.objects.first()))


def test_r26_at29_only_humans_with_revalue_right_run_it_and_runner_cannot_approve(w):
    f1_invoice(w)
    rate("EUR", "SAR", date(2027, 1, 31), "4.0800")
    jan = w.alm.periods.get(name="2027-01")
    agent = drafting_agent(w.alm)
    for user in (w.cfo, agent, access.system_user("fx_import"), make_user("outsider")):
        with pytest.raises(access.AccessDenied):
            services.revalue_period(w.alm, jan, user=user)
    run = services.revalue_period(w.alm, jan, user=w.clerk)
    member(w.clerk, w.alm, "approver")
    with pytest.raises(access.AccessDenied):  # the runner created the entry (access R14)
        access.require(w.clerk, w.alm, "ledger.post_entry", document=run.entry)
    other_grant = access.require(w.cfo, w.alm, "invoicing.approve_bill", document=run.entry)
    with pytest.raises(access.AccessDenied) as exc:  # only a manual-journal posting right posts a revaluation
        services.post_revaluation(run, other_grant)
    assert exc.value.code == "grant_wrong_permission"
    assert JournalEntry.objects.get(pk=run.entry_id).status == "draft"


def test_oq9_discard_draft_run_then_run_again(w):
    f1_invoice(w)
    rate("EUR", "SAR", date(2027, 1, 31), "4.0800")
    jan = w.alm.periods.get(name="2027-01")
    run = services.revalue_period(w.alm, jan, user=w.clerk)
    with pytest.raises(access.AccessDenied):
        services.discard_revaluation(run, user=w.cfo)
    services.discard_revaluation(run, user=w.clerk)
    assert not RevaluationRun.objects.exists() and JournalEntry.objects.count() == 1
    posted = revalue_and_post(w, w.alm, "2027-01")
    with pytest.raises(LedgerError, match="cannot be discarded"):
        services.discard_revaluation(posted, user=w.clerk)


def test_revaluation_entry_lines_cannot_be_changed_after_posting(w):
    f1_invoice(w)
    rate("EUR", "SAR", date(2027, 1, 31), "4.0800")
    run = revalue_and_post(w, w.alm, "2027-01")
    line = line_on(JournalEntry.objects.get(pk=run.entry_id), "1200")
    line.credit = D("1.00")
    with pytest.raises(LedgerError):
        line.save()


# --- R27, R28 / AT-16: entity separation of rates ------------------------------------

def test_r27_at16_entity_override_used_only_by_that_entity(w):
    services.create_rate(user=w.treasury, from_currency="EUR", to_currency="SAR", on=date(2027, 3, 1), rate="4.1000")
    services.create_rate(user=w.clerk, entity=w.lzm, from_currency="EUR", to_currency="SAR", on=date(2027, 3, 1), rate="4.1100")
    eur = [{"account": "1200", "currency": "EUR", "txn_debit": "100.00"}, {"account": "4100", "currency": "EUR", "txn_credit": "100.00"}]
    alm_e, lzm_e = book(w, w.alm, date(2027, 3, 1), eur), book(w, w.lzm, date(2027, 3, 1), eur)
    assert line_on(alm_e, "1200").debit == D("410.00") and line_on(alm_e, "1200").exchange_rate.entity_id is None
    assert line_on(lzm_e, "1200").debit == D("411.00") and line_on(lzm_e, "1200").exchange_rate.entity_id == w.lzm.pk


def test_r27_line_using_another_entitys_rate_row_rejected(w):
    lzm_rate = rate("EUR", "SAR", date(2027, 3, 1), "4.1100", entity=w.lzm)
    rate("EUR", "SAR", date(2027, 3, 1), "4.1000")
    entry = draft(w, w.alm, date(2027, 3, 1), [
        {"account": "1200", "currency": "EUR", "txn_debit": "100.00"}, {"account": "4100", "currency": "EUR", "txn_credit": "100.00"},
    ])
    JournalLine.objects.filter(entry=entry, account__code="1200").update(exchange_rate=lzm_rate, fx_rate=D("4.11"), debit=D("411.00"))
    JournalLine.objects.filter(entry=entry, account__code="4100").update(exchange_rate=lzm_rate, fx_rate=D("4.11"), credit=D("411.00"))
    with pytest.raises(LedgerError, match="another entity"):
        post(w, entry)


def test_r28_at16_who_may_maintain_rates(w):
    alm_only = make_user("alm.accountant")
    member(alm_only, w.alm, "accountant")
    kw = dict(from_currency="EUR", to_currency="SAR", on=date(2027, 3, 1), rate="4.1")
    with pytest.raises(access.AccessDenied):
        services.create_rate(user=alm_only, entity=w.lzm, **kw)
    with pytest.raises(access.AccessDenied):  # an accountant is not group treasury
        services.create_rate(user=alm_only, **kw)
    with pytest.raises(access.AccessDenied):
        services.create_rate(user=drafting_agent(w.alm), entity=w.alm, **kw)
    assert not ExchangeRate.objects.exists()
    assert services.create_rate(user=alm_only, entity=w.alm, **kw).entity == w.alm
    assert services.create_rate(user=w.treasury, entity=w.lzm, **kw).entity == w.lzm  # treasury: entity rates too
    fx_import = access.system_user("fx_import")
    member(fx_import, w.alm, "system_fx_import")
    assert services.create_rate(user=fx_import, source="file", **kw).entity is None
    with pytest.raises(LedgerError, match="not a group parent"):
        services.create_rate(user=w.treasury, group=w.lzm, **kw)


def test_r28_rate_lists_are_filtered_by_entity(w):
    rate("EUR", "SAR", date(2027, 3, 1), "4.1000")
    rate("EUR", "SAR", date(2027, 3, 1), "4.1100", entity=w.lzm)
    rate("EUR", "SAR", date(2027, 3, 1), "4.1200", entity=w.alm)
    lzm_viewer, nobody = make_user("lzm.viewer"), make_user("nobody")
    member(lzm_viewer, w.lzm, "viewer")
    rows = services.rates_visible_to(lzm_viewer)
    assert sorted((r.entity_id or 0) for r in rows) == [0, w.lzm.pk]
    assert not services.rates_visible_to(nobody).exists()


# --- R32 to R36 / AT-19: Sharia and spot exchange ------------------------------------------

def test_r32_at19_sharia_block_applies_to_foreign_lines(w):
    rate("EUR", "SAR", date(2027, 1, 15), "4.1000")
    entry = draft(w, w.alm, date(2027, 1, 15), [
        {"account": "5610", "currency": "EUR", "txn_debit": "10.00"}, {"account": "1100", "currency": "EUR", "txn_credit": "10.00"},
    ])
    with pytest.raises(LedgerError, match="Sharia-prohibited"):
        post(w, entry)


def test_r36_at19_no_forward_rate_type(w):
    with pytest.raises(LedgerError, match="only spot and average"):
        services.create_rate(user=w.treasury, from_currency="USD", to_currency="SAR", on=date(2027, 1, 15), rate="3.75",
                             rate_type="forward")
    row = ExchangeRate(from_currency_id="USD", to_currency_id="SAR", date=date(2027, 1, 15), rate=D("3.75"),
                       rate_type="forward", source="manual", created_by=w.treasury)
    with pytest.raises(ValidationError):
        row.full_clean()
    with pytest.raises(IntegrityError), transaction.atomic():
        row.save()
    assert "forward" not in ExchangeRate.RateType.values


def test_r20_r35_at19_f8_spot_currency_exchange(w):
    with pytest.raises(LedgerError, match="same date"):
        services.record_currency_exchange(
            w.alm, date(2027, 1, 20), "1100", "37510.00", "1110", "10000.00", user=w.clerk, to_currency="USD",
            to_date=date(2027, 1, 22),
        )
    assert not JournalEntry.objects.exists()
    entry = services.record_currency_exchange(
        w.alm, date(2027, 1, 20), "1100", "37510.00", "1110", "10000.00", user=w.clerk, to_currency="USD",
    )
    posted = post(w, entry)
    usd, sar = line_on(posted, "1110"), line_on(posted, "1100")
    assert (usd.currency_id, usd.txn_amount, usd.fx_rate, usd.rate_source, usd.debit) == ("USD", D("10000.000"), D("3.751"), "manual", D("37510.00"))
    assert "Actual bank rate" in usd.manual_rate_reason
    assert (sar.currency_id, sar.credit, sar.fx_rate) == ("SAR", D("37510.00"), D("1"))
    assert posted.lines.count() == 2  # no FX gain or loss at the exchange itself


def test_r20_exchange_between_two_foreign_currencies_uses_table_rate_for_sold_leg(w):
    rate("USD", "SAR", date(2027, 1, 20), "3.7500")
    entry = services.record_currency_exchange(
        w.alm, date(2027, 1, 20), "1100", "1000.00", "1110", "920.00", user=w.clerk, from_currency="USD", to_currency="EUR",
    )
    posted = post(w, entry)
    assert line_on(posted, "1100").credit == line_on(posted, "1110").debit == D("3750.00")
    assert line_on(posted, "1110").rate_source == "manual" and line_on(posted, "1100").rate_source == "table"


def test_r33_fx_accounts_are_ordinary_and_template_accounts_are_bilingual(w):
    for code in ("3400", "4700", "4710", "5750", "5760", "5770"):
        acc = Account.objects.get(entity=w.alm, code=code)
        assert acc.name_en and acc.name_ar and not acc.sharia_prohibited and not acc.requires_purification
    assert not Account.objects.get(entity=w.alm, code="3400").is_postable  # report / consolidation use only
    monetary = set(Account.objects.filter(entity=w.alm, is_monetary=True).values_list("code", flat=True))
    assert monetary == {"1100", "1110", "1200", "1210", "1220", "1300", "1310", "2100", "2110", "2200", "2210",
                        "2300", "2400", "2500", "2600"}


# --- AT-24, AT-25, AT-26: other functional currencies, intercompany, translation ------------

def test_at24_entity_with_egp_functional_works_as_data(w):
    rate("SAR", "EGP", date(2027, 1, 10), "13.0000")
    rate("SAR", "EGP", date(2027, 2, 10), "13.2000")
    bill = book(w, w.egy, date(2027, 1, 10), [
        {"account": "5300", "currency": "SAR", "txn_debit": "1000.00"},
        {"account": "2100", "currency": "SAR", "txn_credit": "1000.00"},
    ])
    assert bill.number.startswith("EGY-JE-2027-")
    assert line_on(bill, "2100").credit == D("13000.00")
    pay = book(w, w.egy, date(2027, 2, 10), services.fx_settlement_lines(
        w.egy, date(2027, 2, 10), line_on(bill, "2100"), "1000.00", "1100", user=w.clerk, counter_currency="EGP"))
    cash, loss = line_on(pay, "1100"), line_on(pay, "5750")
    assert (cash.currency_id, cash.credit) == ("EGP", D("13200.00"))
    assert (loss.currency_id, loss.debit) == ("EGP", D("200.00"))
    assert pay.number.startswith("EGY-JE-2027-")


def test_r29_at25_intercompany_positions_mirror_in_transaction_currency(w):
    rate("USD", "SAR", date(2027, 1, 10), "3.7500")
    rate("USD", "SAR", date(2027, 1, 12), "3.7600")
    book(w, w.alm, date(2027, 1, 10), [
        {"account": "1220", "currency": "USD", "txn_debit": "10000.00", "counterparty": w.lzm},
        {"account": "1100", "currency": "USD", "txn_credit": "10000.00"},
    ])
    book(w, w.lzm, date(2027, 1, 12), [
        {"account": "1100", "currency": "USD", "txn_debit": "10000.00"},
        {"account": "2110", "currency": "USD", "txn_credit": "10000.00", "counterparty": w.alm},
    ])
    viewer = make_user("group.viewer")
    member(viewer, w.alm, "group_viewer")
    ic = services.intercompany_balances(date(2027, 1, 31), user=viewer, group=w.alm, by_currency=True)
    assert ic[("ALM", "LZM", "USD")] == -ic[("LZM", "ALM", "USD")] == D("10000.000")
    functional = services.intercompany_balances(date(2027, 1, 31), user=viewer, group=w.alm)
    assert functional[("ALM", "LZM")] == D("37500.00") and functional[("LZM", "ALM")] == D("-37600.00")


def test_r30_r31_at26_translation_is_report_only(w):
    book(w, w.alm, date(2027, 1, 10), [{"account": "1100", "debit": 5}, {"account": "3100", "credit": 5}])
    before = JournalEntry.objects.count()
    viewer = make_user("auditor")
    member(viewer, w.alm, "group_viewer")
    same = services.translate_trial_balance(w.alm, date(2027, 1, 31), date(2027, 1, 1), "SAR", user=viewer)
    assert same == services.trial_balance(w.alm, date(2027, 1, 31), user=viewer)
    with pytest.raises(NotImplementedError, match="OQ-5"):
        services.translate_trial_balance(w.egy, date(2027, 1, 31), date(2027, 1, 1), "SAR", user=viewer)
    with pytest.raises(access.AccessDenied):
        services.translate_trial_balance(w.alm, date(2027, 1, 31), date(2027, 1, 1), "SAR", user=make_user("x"))
    assert JournalEntry.objects.count() == before


def test_trial_balance_functional_result_unchanged_with_per_currency_columns(w):
    f1_invoice(w)
    plain = services.trial_balance(w.alm, date(2027, 1, 31), user=w.clerk)
    detailed = services.trial_balance(w.alm, date(2027, 1, 31), user=w.clerk, by_currency=True)
    assert [{k: v for k, v in r.items() if k != "by_currency"} for r in detailed] == plain
    assert {r["code"]: r["by_currency"] for r in detailed} == {"1200": {"EUR": D("10000.000")}, "4100": {"EUR": D("-10000.000")}}


# --- R37 / AT-27: bilingual ---------------------------------------------------------

def _has_arabic(text):
    return any("؀" <= ch <= "ۿ" for ch in str(text))


def test_r37_at27_arabic_text_for_errors_currencies_accounts_and_labels(w):
    with pytest.raises(LedgerError) as exc:
        draft(w, w.alm, date(2027, 3, 10), [{"account": "1200", "currency": "USD", "txn_debit": "1"}, {"account": "4100", "credit": 1}])
    assert _has_arabic(problems_text(exc)) and "USD->SAR" in problems_text(exc)
    assert all(c.name_en and _has_arabic(c.name_ar) for c in Currency.objects.all())
    assert Currency.objects.filter(code__in=["SAR", "USD", "EUR", "GBP", "CNY", "EGP", "AED", "KWD", "BHD", "OMR",
                                             "QAR", "JOD", "JPY", "CHF", "INR", "PKR", "TRY"]).count() == 17
    for code in ("3400", "4700", "4710", "5750", "5760", "5770"):
        assert _has_arabic(Account.objects.get(entity=w.alm, code=code).name_ar)
    for choices in (JournalLine.LineKind, JournalLine.RateSource, ExchangeRate.RateType, ExchangeRate.Source):
        assert all(_has_arabic(label) for label in choices.labels)
    for model, names in ((JournalLine, ["currency", "txn_amount", "fx_rate", "line_kind"]),
                         (ExchangeRate, ["rate", "date", "source"]), (Account, ["is_monetary", "currency"])):
        for name in names:
            assert _has_arabic(model._meta.get_field(name).verbose_name)


def test_r37_revaluation_texts_are_bilingual(w):
    f1_invoice(w)
    rate("EUR", "SAR", date(2027, 1, 31), "4.0800")
    run = revalue_and_post(w, w.alm, "2027-01")
    assert _has_arabic(JournalEntry.objects.get(pk=run.entry_id).description)
    assert _has_arabic(JournalEntry.objects.get(pk=run.reversal_id).description)


# --- R11 to R13 / AT-28: CSV import ------------------------------------------------------

CSV = (
    "date,from_currency,to_currency,rate,rate_type,period_start,period_end\n"
    "2027-01-15,EUR,SAR,4.1000,,,\n"
    "2027-01-15,USD,SAR,3.7500,,,\n"
    "2027-01-31,EUR,SAR,4.0950,average,2027-01-01,2027-01-31\n"
)


def test_at28_csv_import_is_idempotent_and_atomic(w, tmp_path):
    from io import StringIO

    path = tmp_path / "rates-2027-01.csv"
    path.write_text(CSV, encoding="utf-8")
    first = services.import_rates(path, user=w.treasury)
    assert first["created"] == 3 and first["batch"]
    rows = ExchangeRate.objects.filter(import_batch=first["batch"])
    assert rows.count() == 3 and all(r.source == "file" and r.source_reference == "rates-2027-01.csv" and r.entity is None for r in rows)
    assert rows.get(rate_type="average").period_start == date(2027, 1, 1)
    second = services.import_rates(path, user=w.treasury)
    assert (second["created"], second["unchanged"]) == (0, 3) and ExchangeRate.objects.count() == 3
    conflict = CSV.replace("4.1000", "4.2000") + "2027-02-01,GBP,SAR,4.7000,,,\n"
    with pytest.raises(LedgerError, match="already exists"):
        services.import_rates(StringIO(conflict), user=w.treasury)
    assert ExchangeRate.objects.count() == 3  # nothing from the failed file
    for bad, message in (("date,from_currency,rate\n", "Missing columns"),
                         ("date,from_currency,to_currency,rate\n15/01/2027,EUR,SAR,4.1\n", "not a date"),
                         ("date,from_currency,to_currency,rate\n2027-01-16,EUR,SAR,-1\n", "above zero"),
                         ("date,from_currency,to_currency,rate\n2027-01-16,EUR,EUR,1\n", "differ")):
        with pytest.raises(LedgerError, match=message):
            services.import_rates(StringIO(bad), user=w.treasury)


def test_oq2_automatic_sources_not_enabled_and_import_needs_rights(w):
    from io import StringIO

    with pytest.raises(LedgerError, match="not enabled"):
        services.import_rates(StringIO(CSV), user=w.treasury, source="ecb")
    with pytest.raises(access.AccessDenied):
        services.import_rates(StringIO(CSV), user=w.clerk)  # global rates: group-level right only
    result = services.import_rates(StringIO(CSV), user=w.clerk, entity=w.alm)  # own entity's overrides
    assert result["created"] == 3 and set(ExchangeRate.objects.values_list("entity", flat=True)) == {w.alm.pk}


# --- AT-29: agents ------------------------------------------------------------------------

def test_at29_agent_drafts_with_table_rates_only_and_is_logged(w):
    rate("EUR", "SAR", date(2027, 1, 15), "4.1000")
    agent = drafting_agent(w.alm)
    with pytest.raises(access.AccessDenied):
        services.create_rate(user=agent, from_currency="EUR", to_currency="SAR", on=date(2027, 1, 16), rate="4.1")
    entry = draft(w, w.alm, date(2027, 1, 15), [
        {"account": "1200", "currency": "EUR", "txn_debit": "10.00"}, {"account": "4100", "currency": "EUR", "txn_credit": "10.00"},
    ], user=agent)
    assert entry.status == "draft" and entry.created_by == agent
    assert AccessLog.objects.filter(actor=agent, event="permission_granted", permission="ledger.create_entry",
                                    details__document=["ledger.journalentry", entry.pk]).exists()
    with pytest.raises(access.AccessDenied):
        access.require(agent, w.alm, "ledger.post_entry", document=entry)
    with pytest.raises(access.AccessDenied):
        services.revalue_period(w.alm, w.alm.periods.get(name="2027-01"), user=agent)
    assert post(w, entry).status == "posted"  # a person approves the agent's draft


def test_rerate_needs_draft_rights_in_that_entity(w):
    rate("USD", "SAR", date(2027, 3, 10), "3.7500")
    entry = draft(w, w.alm, date(2027, 3, 10), [
        {"account": "1200", "currency": "USD", "txn_debit": "1.00"}, {"account": "4100", "currency": "USD", "txn_credit": "1.00"},
    ])
    outsider = make_user("lzm.only")
    member(outsider, w.lzm, "accountant")
    with pytest.raises(access.AccessDenied):
        services.rerate_entry(entry, user=outsider)
    post(w, entry)
    with pytest.raises(LedgerError, match="Only draft"):
        services.rerate_entry(entry, user=w.clerk)
