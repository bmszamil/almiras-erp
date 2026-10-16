from datetime import date
from decimal import Decimal

import pytest

from core import access
from core.access.testing import make_user, member
from core.models import Entity
from ledger import services
from ledger.models import Account, JournalEntry, JournalLine, LedgerError
from ledger.services_setup import create_monthly_periods, install_chart_of_accounts

pytestmark = pytest.mark.django_db
D = Decimal


def staff_for(entity):
    """Accountant drafts, approver (CFO) posts: separate people, per entity (access.md A1, R23)."""
    from django.contrib.auth import get_user_model

    User = get_user_model()
    clerk = User.objects.filter(username="clerk").first() or make_user("clerk")
    cfo = User.objects.filter(username="cfo").first() or make_user("cfo")
    member(clerk, entity, "accountant")
    member(cfo, entity, "approver")
    return clerk, cfo


@pytest.fixture
def alm():
    e = Entity.objects.create(code="ALM", name_en="Almiras", name_ar="المراس", consolidation="parent")
    install_chart_of_accounts(e)
    create_monthly_periods(e, 2027)
    staff_for(e)
    return e


@pytest.fixture
def sub(alm):
    e = Entity.objects.create(code="LZM", name_en="Lizam", name_ar="لزام", parent=alm, ownership_pct=D("60"))
    install_chart_of_accounts(e)
    create_monthly_periods(e, 2027)
    staff_for(e)
    return e


def clerk_and_cfo():
    from django.contrib.auth import get_user_model

    User = get_user_model()
    return User.objects.get(username="clerk"), User.objects.get(username="cfo")


def book(entity, entry_date, description, lines, post=True):
    """Draft by the accountant; if ``post``, posted by the approver with a grant for that draft."""
    clerk, cfo = clerk_and_cfo()
    entry = services.create_entry(entity, entry_date, description, lines, user=clerk)
    if not post:
        return entry
    grant = access.require(cfo, entity, "ledger.post_entry", document=entry)
    return services.post_entry(entry, grant)


def reverse(entry, **kw):
    clerk, _ = clerk_and_cfo()
    return services.reverse_entry(entry, access.require(clerk, entry.entity, "ledger.reverse_entry"), **kw)


def rent_invoice(entity, counterparty=None, post=True, amount="1000"):
    return book(
        entity, date(2027, 1, 15), "Warehouse rent Jan",
        [
            {"account": "1210", "debit": D(amount) * D("1.15"), "counterparty": counterparty},
            {"account": "4110", "credit": amount},
            {"account": "2200", "credit": D(amount) * D("0.15")},
        ],
        post=post,
    )


def test_balanced_entry_posts_and_is_numbered(alm, sub):
    e = rent_invoice(alm, counterparty=sub)
    assert e.status == JournalEntry.Status.POSTED
    assert e.number == "ALM-JE-2027-000001"
    assert e.period.name == "2027-01"
    assert e.posted_by.username == "cfo" and e.created_by.username == "clerk"


def test_numbers_are_sequential(alm):
    for _ in range(2):
        book(alm, date(2027, 2, 1), "x", [{"account": "1100", "debit": 5}, {"account": "3100", "credit": 5}])
    numbers = list(JournalEntry.objects.values_list("number", flat=True).order_by("number"))
    assert numbers == ["ALM-JE-2027-000001", "ALM-JE-2027-000002"]


def test_unbalanced_entry_is_rejected(alm):
    with pytest.raises(LedgerError, match="does not balance"):
        book(alm, date(2027, 1, 5), "bad", [{"account": "1100", "debit": 100}, {"account": "3100", "credit": 90}])


def test_posted_entry_cannot_be_edited_or_deleted(alm, sub):
    e = rent_invoice(alm, counterparty=sub)
    e.description = "changed"
    with pytest.raises(LedgerError):
        e.save()
    with pytest.raises(LedgerError):
        e.delete()
    line = e.lines.first()
    line.debit = D("1")
    with pytest.raises(LedgerError):
        line.save()
    with pytest.raises(LedgerError):
        line.delete()


def test_reversal_nets_to_zero_and_locks_original(alm, sub):
    e = rent_invoice(alm, counterparty=sub)
    rev = reverse(e, reason="wrong amount")
    e.refresh_from_db()
    assert e.status == JournalEntry.Status.REVERSED
    assert rev.reversal_of == e and rev.status == JournalEntry.Status.POSTED
    assert services.trial_balance(alm, date(2027, 12, 31)) == []
    with pytest.raises(LedgerError):
        reverse(e)


def test_closed_period_rejects_posting(alm):
    period = alm.periods.get(name="2027-01")
    services.close_period(period)
    with pytest.raises(LedgerError, match="closed"):
        book(alm, date(2027, 1, 20), "late", [{"account": "1100", "debit": 1}, {"account": "3100", "credit": 1}])


def test_no_period_rejects_posting(alm):
    with pytest.raises(LedgerError, match="No fiscal period"):
        book(alm, date(2028, 1, 1), "x", [{"account": "1100", "debit": 1}, {"account": "3100", "credit": 1}])


def test_sharia_prohibited_account_is_blocked(alm):
    with pytest.raises(LedgerError, match="Sharia-prohibited"):
        book(alm, date(2027, 1, 31), "interest", [{"account": "5610", "debit": 50}, {"account": "1100", "credit": 50}])


def test_sharia_block_off_when_entity_does_not_enforce(alm):
    alm.sharia_enforced = False
    alm.save()
    e = book(alm, date(2027, 1, 31), "interest", [{"account": "5610", "debit": 50}, {"account": "1100", "credit": 50}])
    assert e.status == JournalEntry.Status.POSTED


def test_intercompany_needs_counterparty(alm):
    with pytest.raises(LedgerError, match="counterparty"):
        rent_invoice(alm, counterparty=None)


def test_header_account_not_postable(alm):
    with pytest.raises(LedgerError, match="not open for posting"):
        book(alm, date(2027, 1, 2), "x", [{"account": "1000", "debit": 1}, {"account": "3100", "credit": 1}])


def test_cannot_use_another_entitys_account(alm, sub):
    sub_cash = Account.objects.get(entity=sub, code="1100")
    with pytest.raises(LedgerError, match="another entity"):
        book(alm, date(2027, 1, 2), "x", [{"account": sub_cash, "debit": 1}, {"account": "3100", "credit": 1}])


def test_line_must_be_one_sided():
    from django.db import IntegrityError
    e = Entity.objects.create(code="TST", name_en="T", name_ar="ت", consolidation="parent")
    install_chart_of_accounts(e)
    entry = JournalEntry.objects.create(entity=e, date=date(2027, 1, 1), description="x")
    with pytest.raises(IntegrityError):
        JournalLine.objects.create(entry=entry, account=Account.objects.get(entity=e, code="1100"), debit=1, credit=1)


def test_trial_balance_balances(alm, sub):
    rent_invoice(alm, counterparty=sub)
    tb = services.trial_balance(alm, date(2027, 1, 31))
    assert sum(r["debit"] for r in tb) == sum(r["credit"] for r in tb) == D("1150.00")


def test_intercompany_balances_mirror(alm, sub):
    rent_invoice(alm, counterparty=sub)
    book(
        sub, date(2027, 1, 15), "Warehouse rent Jan from ALM",
        [{"account": "5200", "debit": 1000}, {"account": "1310", "debit": 150}, {"account": "2110", "credit": 1150, "counterparty": alm}],
    )
    ic = services.intercompany_balances(date(2027, 1, 31))
    assert ic[("ALM", "LZM")] == -ic[("LZM", "ALM")] == D("1150.00")


def test_draft_can_be_deleted(alm):
    e = book(alm, date(2027, 1, 2), "x", [{"account": "1100", "debit": 1}, {"account": "3100", "credit": 1}], post=False)
    e.delete()
    assert not JournalEntry.objects.exists()
