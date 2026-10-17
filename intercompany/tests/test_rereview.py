"""Re-review (17 Oct 2026): A suspended days never billed, B no double-billed pool adjustments,
C adjustments skip fully credited originals and still bill late items of ended agreements."""
import datetime
from decimal import Decimal

import pytest

from access.errors import BilingualError, RuleError
from approvals import services as approvals
from intercompany import services
from intercompany.models import AgreementSuspension, InvoiceLine
from ledger import services as ledger
from ledger.models import Account, FiscalPeriod

from .conftest import TODAY, approve

pytestmark = pytest.mark.django_db
D = Decimal


def period(entity, name):
    return FiscalPeriod.objects.get(entity=entity, name=name)


def post_cost(entity, day, amount, account="5100"):
    return ledger.create_entry(entity, day, "Cost", [{"account": account, "debit": amount},
                                                     {"account": "1100", "credit": amount}], system=True, post=True)


def issue(inv, accountant, mp):
    approvals.submit(inv, accountant)
    approve(inv, mp)
    inv.refresh_from_db()
    return inv


def billed(agreement, key):
    inv = agreement.invoices.filter(billing_period=key).exclude(status__in=["cancelled_draft", "rejected"]).first()
    return inv.net_total if inv else None


@pytest.fixture
def pool(accountant, alm):
    return services.create_cost_pool(accountant, alm, code="FIN", name_en="Finance", name_ar="المالية",
                                     accounts=[Account.objects.get(entity=alm, code="5100")])


# ---------------------------------------------------------------- A: suspensions


def test_a_suspended_then_ended_months_are_not_billed(make_agreement, accountant, alm):
    """The reviewer's case: suspended from 1 Oct, ended at 30 Nov -> Oct and Nov are not billed."""
    a = make_agreement()
    services.suspend_agreement(accountant, a, "Plant shutdown", from_date=datetime.date(2026, 10, 1))
    services.end_agreement(accountant, a, datetime.date(2026, 11, 30), "Closed")
    a.refresh_from_db()
    assert a.status == "ended"
    for name in ("2026-09", "2026-10", "2026-11"):
        run = services.run_billing(accountant, alm, period(alm, name))
        if name == "2026-09":
            assert run.invoices_created == 1
        else:
            assert run.invoices_created == 0 and run.log[0]["code"] == "suspended"
    assert billed(a, "2026-09") == D("10000.00") and billed(a, "2026-10") is None and billed(a, "2026-11") is None
    services.close_ended_agreements(alm)
    assert services.run_billing(accountant, alm, period(alm, "2026-11")).invoices_created == 0


def test_a_partial_suspension_pro_rated_and_hours_excluded(make_agreement, accountant, mp, alm, monkeypatch):
    fee = make_agreement()
    hours = make_agreement("timesheet")
    monkeypatch.setattr(services, "today", lambda: datetime.date(2026, 11, 8))
    kw = dict(description_en="Work", description_ar="عمل")
    in_gap = services.create_timesheet(accountant, hours, date=datetime.date(2026, 11, 7), hours=D("2"), **kw)
    ok = services.create_timesheet(accountant, hours, date=datetime.date(2026, 11, 3), hours=D("1"), **kw)
    for e in (in_gap, ok):
        services.submit_timesheet(accountant, e)
        services.approve_timesheet(mp, e)
    monkeypatch.setattr(services, "today", lambda: datetime.date(2026, 11, 11))
    for a in (fee, hours):
        services.suspend_agreement(accountant, a, "Pause", from_date=datetime.date(2026, 11, 5))
    monkeypatch.setattr(services, "today", lambda: datetime.date(2026, 11, 21))
    for a in (fee, hours):  # resume through approval
        approvals.submit(a, accountant)
        approve(a, mp)
        a.refresh_from_db()
        assert a.status == "active"
    sp = AgreementSuspension.objects.get(agreement=fee)
    assert (sp.start_date, sp.end_date) == (datetime.date(2026, 11, 5), datetime.date(2026, 11, 20))
    monkeypatch.setattr(services, "today", lambda: TODAY)
    services.run_billing(accountant, alm, period(alm, "2026-11"))
    line = fee.invoices.get().lines.get()
    assert line.net_amount == D("4666.67") and "14/30 days" in line.pricing_basis  # 16 suspended days
    assert hours.invoices.get().net_total == D("450.00")  # the 7 Nov hours fall in the suspension
    in_gap.refresh_from_db()
    assert in_gap.status == "approved" and in_gap.invoice_line is None


def test_a_suspension_rules(make_agreement, accountant, alm, nov):
    a = make_agreement()
    services.run_billing(accountant, alm, nov)
    with pytest.raises(RuleError, match="credit them first"):
        services.suspend_agreement(accountant, a, "Pause", from_date=datetime.date(2026, 11, 15))
    with pytest.raises(RuleError, match="within the agreement"):
        services.suspend_agreement(accountant, a, "Pause", from_date=datetime.date(2025, 1, 1))
    services.suspend_agreement(accountant, a, "Pause")
    sp = AgreementSuspension.objects.get()
    assert sp.start_date == TODAY and sp.end_date is None
    sp.start_date = datetime.date(2026, 12, 31)
    with pytest.raises(BilingualError):
        sp.save()
    with pytest.raises(BilingualError):
        AgreementSuspension.objects.get().delete()


# ---------------------------------------------------------------- B: no double billing of late pool costs


def test_b_cancelled_and_rebilled_draft_is_not_double_adjusted(make_agreement, accountant, mp, alm, pool,
                                                                monkeypatch):
    """The reviewer's case: October pool 1,000 billed as a draft, 200 posted late, draft cancelled and
    re-billed: October must total 1,200, not 1,400."""
    post_cost(alm, datetime.date(2026, 10, 10), "1000")
    a = make_agreement("cost_plus", cost_pool=pool, allocation_basis="fixed_pct", allocation_pct=D("100"),
                       markup_pct=D("0"))
    services.run_billing(accountant, alm, period(alm, "2026-10"))
    oct_draft = a.invoices.get(billing_period="2026-10")
    assert oct_draft.net_total == D("1000.00")
    post_cost(alm, datetime.date(2026, 10, 25), "200")  # late October cost
    post_cost(alm, datetime.date(2026, 11, 10), "500")
    services.run_billing(accountant, alm, period(alm, "2026-11"))
    nov = a.invoices.get(billing_period="2026-11")
    assert not nov.lines.exclude(adjustment_period="").exists()  # no adjustment against a draft original
    services.cancel_draft(accountant, oct_draft, "Late costs")
    rebilled = services.create_invoice_draft(accountant, a, period(alm, "2026-10"))
    assert rebilled.net_total == D("1200.00")
    issue(rebilled, accountant, mp)
    issue(nov, accountant, mp)
    monkeypatch.setattr(services, "today", lambda: datetime.date(2027, 1, 5))
    services.run_billing(accountant, alm, period(alm, "2026-12"))
    oct_total = sum(l.net_amount for l in InvoiceLine.objects.filter(
        agreement=a, cost_pool_period__period__name="2026-10").exclude(invoice__status="cancelled_draft"))
    assert oct_total == D("1200.00")


def test_b_adjustment_against_issued_original_billed_once(make_agreement, accountant, mp, alm, pool, monkeypatch):
    post_cost(alm, datetime.date(2026, 10, 10), "1000")
    a = make_agreement("cost_plus", cost_pool=pool, allocation_basis="fixed_pct", allocation_pct=D("100"),
                       markup_pct=D("0"))
    services.run_billing(accountant, alm, period(alm, "2026-10"))
    issue(a.invoices.get(billing_period="2026-10"), accountant, mp)
    post_cost(alm, datetime.date(2026, 10, 25), "200")
    post_cost(alm, datetime.date(2026, 11, 10), "500")
    services.run_billing(accountant, alm, period(alm, "2026-11"))
    nov = a.invoices.get(billing_period="2026-11")
    assert nov.lines.get(adjustment_period="2026-10").net_amount == D("200.00")
    monkeypatch.setattr(services, "today", lambda: datetime.date(2027, 1, 5))
    post_cost(alm, datetime.date(2026, 12, 10), "50")
    services.run_billing(accountant, alm, period(alm, "2026-12"))  # Nov adjustment still a draft: not re-billed
    dec = a.invoices.get(billing_period="2026-12")
    assert not dec.lines.filter(adjustment_period="2026-10").exists()


# ---------------------------------------------------------------- C: credited originals, ended agreements


def test_c_fully_credited_original_gets_no_adjustment(make_agreement, accountant, mp, alm, pool):
    post_cost(alm, datetime.date(2026, 10, 10), "1000")
    a = make_agreement("cost_plus", cost_pool=pool, allocation_basis="fixed_pct", allocation_pct=D("100"),
                       markup_pct=D("0"))
    services.run_billing(accountant, alm, period(alm, "2026-10"))
    oct_inv = issue(a.invoices.get(billing_period="2026-10"), accountant, mp)
    issue(services.create_credit_note(accountant, oct_inv, reason_en="Not due", reason_ar="غير مستحق",
                                      trigger_date=TODAY), accountant, mp)
    post_cost(alm, datetime.date(2026, 10, 25), "200")
    post_cost(alm, datetime.date(2026, 11, 10), "500")
    services.run_billing(accountant, alm, period(alm, "2026-11"))
    assert not a.invoices.get(billing_period="2026-11").lines.exclude(adjustment_period="").exists()


def test_c_ended_agreement_still_bills_late_hours_as_adjustment(make_agreement, accountant, mp, alm, monkeypatch):
    a = make_agreement("timesheet", end_date=datetime.date(2026, 10, 31))
    monkeypatch.setattr(services, "today", lambda: datetime.date(2026, 10, 30))
    e = services.create_timesheet(accountant, a, date=datetime.date(2026, 10, 20), hours=D("2"),
                                  description_en="Work", description_ar="عمل")
    services.submit_timesheet(accountant, e)
    monkeypatch.setattr(services, "today", lambda: TODAY)
    run = services.run_billing(accountant, alm, period(alm, "2026-10"))  # nothing approved yet
    assert run.log[0]["code"] == "nothing_to_bill"
    services.approve_timesheet(mp, e)  # approved after October was run and the agreement ended
    run = services.run_billing(accountant, alm, period(alm, "2026-11"))
    a.refresh_from_db()
    assert a.status == "ended" and run.invoices_created == 1
    inv = a.invoices.get(billing_period="2026-11")
    assert (inv.supply_date_from, inv.supply_date_to) == (datetime.date(2026, 10, 1), datetime.date(2026, 10, 31))
    line = inv.lines.get()
    assert line.adjustment_period == "2026-10" and line.net_amount == D("900.00")
    monkeypatch.setattr(services, "today", lambda: datetime.date(2027, 1, 5))
    run = services.run_billing(accountant, alm, period(alm, "2026-12"))
    assert run.invoices_created == 0 and a.number not in [r["agreement"] for r in run.log]  # nothing late: silent


def test_c_ended_cost_plus_bills_late_pool_cost(make_agreement, accountant, mp, alm, pool):
    post_cost(alm, datetime.date(2026, 10, 10), "1000")
    a = make_agreement("cost_plus", cost_pool=pool, allocation_basis="fixed_pct", allocation_pct=D("50"),
                       markup_pct=D("10"), end_date=datetime.date(2026, 10, 31))
    services.run_billing(accountant, alm, period(alm, "2026-10"))
    issue(a.invoices.get(billing_period="2026-10"), accountant, mp)
    post_cost(alm, datetime.date(2026, 10, 25), "200")
    services.run_billing(accountant, alm, period(alm, "2026-11"))
    line = a.invoices.get(billing_period="2026-11").lines.get()
    assert line.adjustment_period == "2026-10" and line.net_amount == D("110.00")  # 200 x 50% x 1.10
