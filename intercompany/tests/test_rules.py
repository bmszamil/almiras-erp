"""One test (or more) per rule R1-R22 and decisions D8, D10, D11, D12, with failure cases."""
import datetime
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from access.errors import AccessDenied, BilingualError, Conflict, RuleError
from access.models import AuditLog
from approvals import services as approvals
from approvals.models import ApprovalRequest
from core.models import Entity
from intercompany import services, zatca
from intercompany.models import (
    AllocationDriver, BillableItem, BillingRun, CostPoolPeriod, DraftEditor, IntercompanyBill, IntercompanyInvoice,
    InvoiceLine, ServiceAgreement, ServiceType, TimesheetEntry, VatRate, workflow,
)
from ledger import services as ledger
from ledger.models import Account, FiscalPeriod, JournalEntry

from .conftest import approve

pytestmark = pytest.mark.django_db
D = Decimal
INV = IntercompanyInvoice
A = ServiceAgreement


def period(entity, name):
    return FiscalPeriod.objects.get(entity=entity, name=name)


def post_cost(entity, day, account, amount):
    return ledger.create_entry(entity, day, "Cost", [{"account": account, "debit": amount},
                                                     {"account": "1100", "credit": amount}], system=True, post=True)


def bill_nov(accountant, alm, nov, agreement):
    services.run_billing(accountant, alm, nov)
    return agreement.invoices.get()


def issue(inv, accountant, mp):
    approvals.submit(inv, accountant)
    approve(inv, mp)
    inv.refresh_from_db()
    return inv


BASE = dict(description_en="Services", description_ar="خدمات", vat_treatment="standard", billing_frequency="monthly",
            payment_terms_days=30, start_date=datetime.date(2026, 1, 1))


# ---------------------------------------------------------------- R1


def test_r1_inactive_counterparty_rejected(accountant, alm, lfn, fin):
    lfn.is_active = False
    lfn.save()
    with pytest.raises(RuleError, match="active"):
        services.create_agreement(accountant, entity=alm, counterparty=lfn, service_type=fin, pricing_method="fixed_fee",
                                  fixed_fee=D("1"), **BASE)


def test_r1_db_refuses_invoice_to_same_entity(make_agreement, accountant, alm, nov):
    inv = bill_nov(accountant, alm, nov, make_agreement())
    with pytest.raises(IntegrityError), transaction.atomic():
        INV.objects.create(entity=alm, counterparty=alm, agreement=inv.agreement, issue_date=inv.issue_date,
                           supply_date_from=inv.issue_date, supply_date_to=inv.issue_date, due_date=inv.issue_date,
                           billing_period="2026-10", currency="SAR", invoice_type_code="388")


# ---------------------------------------------------------------- R2 (one test per method)


@pytest.mark.parametrize("pricing,ok,extra_field", [
    ("fixed_fee", {"fixed_fee": D("100")}, {"hourly_rate": D("5")}),
    ("timesheet", {"hourly_rate": D("450")}, {"fixed_fee": D("5")}),
    ("cost_plus", "pool", {"hourly_rate": D("5")}),
])
def test_r2_each_method_needs_its_fields_and_no_others(accountant, alm, lfn, fin, pricing, ok, extra_field):
    pool = services.create_cost_pool(accountant, alm, code="P", name_en="P", name_ar="ب")
    if ok == "pool":
        ok = {"markup_pct": D("10"), "cost_pool": pool, "allocation_basis": "fixed_pct", "allocation_pct": D("20")}
    kw = dict(entity=alm, counterparty=lfn, service_type=fin, pricing_method=pricing, **BASE)
    with pytest.raises(RuleError, match="Required"):
        services.create_agreement(accountant, **kw)  # missing its own fields
    with pytest.raises(RuleError, match="Not used"):
        services.create_agreement(accountant, **kw, **ok, **extra_field)  # another method's field
    a = services.create_agreement(accountant, **kw, **ok)
    assert a.status == "draft" and a.number == "ALM-AGR-2026-0001"


def test_r2_cost_plus_fixed_pct_needs_allocation_pct_and_drivers_do_not(accountant, alm, lfn, fin):
    pool = services.create_cost_pool(accountant, alm, code="P", name_en="P", name_ar="ب")
    kw = dict(entity=alm, counterparty=lfn, service_type=fin, pricing_method="cost_plus", markup_pct=D("5"),
              cost_pool=pool, **BASE)
    with pytest.raises(RuleError, match="allocation_pct"):
        services.create_agreement(accountant, allocation_basis="fixed_pct", **kw)
    with pytest.raises(RuleError, match="allocation_pct"):
        services.create_agreement(accountant, allocation_basis="headcount", allocation_pct=D("5"), **kw)
    assert services.create_agreement(accountant, allocation_basis="headcount", **kw).allocation_pct is None


def test_r2_invalid_values_rejected(accountant, alm, lfn, fin):
    kw = dict(entity=alm, counterparty=lfn, service_type=fin, **BASE)
    with pytest.raises(RuleError):
        services.create_agreement(accountant, pricing_method="fixed_fee", fixed_fee=D("0"), **kw)
    with pytest.raises(RuleError):
        services.create_agreement(accountant, pricing_method="timesheet", hourly_rate=D("-1"), **kw)
    with pytest.raises(RuleError, match="ends before"):
        services.create_agreement(accountant, pricing_method="fixed_fee", fixed_fee=D("1"),
                                  **{**kw, "end_date": datetime.date(2025, 1, 1)})


def test_r2_fixed_allocations_of_a_pool_cannot_exceed_100(make_agreement, accountant, alm, lfn, mp, make_user):
    pool = services.create_cost_pool(accountant, alm, code="P", name_en="P", name_ar="ب")
    make_agreement("cost_plus", cost_pool=pool, allocation_basis="fixed_pct", allocation_pct=D("70"), markup_pct=D("5"))
    second = make_agreement("cost_plus", cost_pool=pool, allocation_basis="fixed_pct", allocation_pct=D("40"),
                            markup_pct=D("5"), activate=False)
    with pytest.raises(RuleError, match="exceed 100"):
        approvals.submit(second, accountant)


def test_r2_vat_exempt_needs_bilingual_reason(accountant, alm, lfn, fin):
    kw = dict(entity=alm, counterparty=lfn, service_type=fin, pricing_method="fixed_fee", fixed_fee=D("1"),
              **{**BASE, "vat_treatment": "exempt"})
    with pytest.raises(RuleError, match="vat_exemption_reason"):
        services.create_agreement(accountant, **kw)
    a = services.create_agreement(accountant, vat_exemption_reason_en="Exempt supply", vat_exemption_reason_ar="توريد معفى", **kw)
    assert a.vat_treatment == "exempt"


def test_r2_cost_plus_must_be_monthly_and_one_off_fixed_fee_only(accountant, alm, lfn, fin):
    pool = services.create_cost_pool(accountant, alm, code="P", name_en="P", name_ar="ب")
    with pytest.raises(RuleError, match="monthly"):
        services.create_agreement(accountant, entity=alm, counterparty=lfn, service_type=fin, pricing_method="cost_plus",
                                  markup_pct=D("5"), cost_pool=pool, allocation_basis="fixed_pct", allocation_pct=D("5"),
                                  **{**BASE, "billing_frequency": "quarterly"})
    with pytest.raises(RuleError, match="fixed fees"):
        services.create_agreement(accountant, entity=alm, counterparty=lfn, service_type=fin, pricing_method="timesheet",
                                  hourly_rate=D("5"), **{**BASE, "billing_frequency": "one_off"})


def test_r2_approved_agreement_locked(make_agreement):
    a = make_agreement()
    a.fixed_fee = D("1")
    with pytest.raises(BilingualError, match="draft"):
        a.save()
    with pytest.raises(BilingualError):
        A.objects.get(pk=a.pk).delete()
    a.refresh_from_db()
    a.status = "draft"
    with pytest.raises(BilingualError):
        a.save()


# ---------------------------------------------------------------- R3


def test_r3_only_active_agreements_covering_the_period_are_billed(make_agreement, accountant, mp, alm, nov):
    draft = make_agreement(activate=False)
    ended_before = make_agreement(end_date=datetime.date(2026, 10, 31))
    starts_mid = make_agreement(start_date=datetime.date(2026, 11, 15))
    suspended = make_agreement()
    services.suspend_agreement(accountant, suspended, "Paused")
    later = make_agreement(start_date=datetime.date(2026, 12, 1))
    ok = make_agreement()
    run = services.run_billing(accountant, alm, nov)
    assert run.invoices_created == 1 and ok.invoices.count() == 1
    for a in (draft, ended_before, starts_mid, suspended, later):
        assert a.invoices.count() == 0
    results = {r["agreement"]: r for r in run.log}
    assert results[starts_mid.number]["code"] == "partial_period"
    assert results[suspended.number]["code"] == "agreement_not_active"
    assert draft.number not in results and later.number not in results


def test_r3_quarterly_annual_and_one_off_windows(make_agreement, accountant, alm, monkeypatch):
    q = make_agreement(billing_frequency="quarterly")
    y = make_agreement(billing_frequency="annual")
    once = make_agreement(billing_frequency="one_off", start_date=datetime.date(2026, 11, 10),
                          end_date=datetime.date(2026, 11, 12))
    monkeypatch.setattr(services, "today", lambda: datetime.date(2027, 1, 10))
    services.run_billing(accountant, alm, period(alm, "2026-11"))
    assert q.invoices.count() == 0 and y.invoices.count() == 0
    one = once.invoices.get()
    assert (one.supply_date_from, one.supply_date_to, one.issue_date) == (
        datetime.date(2026, 11, 10), datetime.date(2026, 11, 12), datetime.date(2026, 11, 30))
    services.run_billing(accountant, alm, period(alm, "2026-12"))
    qi, yi = q.invoices.get(), y.invoices.get()
    assert qi.billing_period == "2026-Q4" and qi.supply_date_from == datetime.date(2026, 10, 1)
    assert yi.billing_period == "2026" and yi.supply_date_from == datetime.date(2026, 1, 1)
    assert qi.issue_date == yi.issue_date == datetime.date(2026, 12, 31)  # D12: last day of the month
    assert once.invoices.count() == 1


def test_r3_run_refuses_unfinished_closed_or_foreign_periods(make_agreement, accountant, alm, lfn):
    with pytest.raises(RuleError, match="not ended"):
        services.run_billing(accountant, alm, period(alm, "2026-12"))
    oct_ = period(alm, "2026-10")
    ledger.close_period(oct_)
    with pytest.raises(RuleError, match="closed"):
        services.run_billing(accountant, alm, oct_)
    with pytest.raises(RuleError, match="another entity"):
        services.run_billing(accountant, alm, period(lfn, "2026-11"))
    assert not BillingRun.objects.exists()


# ---------------------------------------------------------------- R4


def test_r4_db_constraint_one_live_invoice_per_agreement_period(make_agreement, accountant, alm, nov):
    inv = bill_nov(accountant, alm, nov, make_agreement())
    dup = dict(entity=alm, counterparty=inv.counterparty, agreement=inv.agreement, issue_date=inv.issue_date,
               supply_date_from=inv.supply_date_from, supply_date_to=inv.supply_date_to, due_date=inv.due_date,
               billing_period="2026-11", currency="SAR", invoice_type_code="388")
    with pytest.raises(IntegrityError), transaction.atomic():
        INV.objects.create(**dup)
    with pytest.raises(Conflict, match="already billed"):
        services.create_invoice_draft(accountant, inv.agreement, nov)
    services.cancel_draft(accountant, inv)
    again = services.create_invoice_draft(accountant, inv.agreement, nov)  # cancelled drafts free the period (R14)
    assert again.pk != inv.pk and again.gross_total == inv.gross_total


# ---------------------------------------------------------------- R5


def test_r5_timesheet_rules(make_agreement, accountant, make_user, alm, nov, mp):
    a = make_agreement("timesheet")
    clerk = make_user("clerk", (alm, "pa_drafter"))
    kw = dict(description_en="Work", description_ar="عمل")
    with pytest.raises(AccessDenied, match="own hours"):
        services.create_timesheet(clerk, a, worker=accountant, date=datetime.date(2026, 11, 2), hours=D("1"), **kw)
    with pytest.raises(RuleError, match="above zero"):
        services.create_timesheet(clerk, a, date=datetime.date(2026, 11, 2), hours=D("0"), **kw)
    with pytest.raises(RuleError, match="outside"):
        services.create_timesheet(clerk, a, date=datetime.date(2025, 12, 31), hours=D("1"), **kw)
    services.create_timesheet(clerk, a, date=datetime.date(2026, 11, 2), hours=D("20"), **kw)
    with pytest.raises(RuleError, match="24 hours"):
        services.create_timesheet(clerk, a, date=datetime.date(2026, 11, 2), hours=D("4.5"), **kw)
    e = services.create_timesheet(clerk, a, date=datetime.date(2026, 11, 3), hours=D("2"), **kw)
    with pytest.raises(AccessDenied):
        services.approve_timesheet(clerk, e)  # no approve right
    services.submit_timesheet(clerk, e)
    e.hours = D("3")
    with pytest.raises(BilingualError):
        e.save()  # submitted entries are locked
    e.refresh_from_db()
    with pytest.raises(RuleError, match="reason"):
        services.reject_timesheet(accountant, e, "")
    services.reject_timesheet(accountant, e, "Wrong agreement")
    e.refresh_from_db()
    assert e.status == "rejected"
    own = services.create_timesheet(accountant, a, date=datetime.date(2026, 11, 4), hours=D("1"), **kw)
    services.submit_timesheet(accountant, own)
    with pytest.raises(AccessDenied, match="own hours"):
        services.approve_timesheet(accountant, own)  # no self-approval of hours
    services.approve_timesheet(mp, own)
    e.status = "approved"
    with pytest.raises(BilingualError):
        e.save()  # status moves only through services


def test_r5_per_role_rates_and_fixed_agreement_rates_locked(accountant, mp, alm, lfn, fin, nov, make_user):
    a = services.create_agreement(accountant, entity=alm, counterparty=lfn, service_type=fin, pricing_method="timesheet",
                                  hourly_rate=D("400"), **BASE)
    senior = services.add_rate(accountant, a, role_en="Senior accountant", role_ar="محاسب أول", hourly_rate=D("600"),
                               valid_from=datetime.date(2026, 1, 1))
    approvals.submit(a, accountant)
    approve(a, mp)
    with pytest.raises(BilingualError, match="locked"):
        services.add_rate(accountant, a, role_en="x", role_ar="س", hourly_rate=D("1"), valid_from=datetime.date(2026, 1, 1))
    worker = make_user("w", (alm, "pa_drafter"))
    kw = dict(description_en="Work", description_ar="عمل")
    e1 = services.create_timesheet(accountant, a, worker=worker, date=datetime.date(2026, 11, 2), hours=D("2"), **kw)
    e2 = services.create_timesheet(accountant, a, worker=worker, date=datetime.date(2026, 11, 3), hours=D("1"),
                                   rate=senior, **kw)
    for e in (e1, e2):
        services.submit_timesheet(worker, e)
        with pytest.raises(AccessDenied):
            services.approve_timesheet(accountant, e)  # recorded it, so cannot approve it
        services.approve_timesheet(mp, e)
    inv = bill_nov(accountant, alm, nov, a)
    assert sorted((l.unit_price, l.net_amount) for l in inv.lines.all()) == [(D("400"), D("800.00")),
                                                                            (D("600"), D("600.00"))]
    assert any("Senior accountant" in l.description_en and "محاسب أول" in l.description_ar for l in inv.lines.all())


# ---------------------------------------------------------------- R6, D12


def test_r6_rounding_half_up_once(make_agreement, accountant, alm, nov):
    pool = services.create_cost_pool(accountant, alm, code="P", name_en="P", name_ar="ب",
                                     accounts=[Account.objects.get(entity=alm, code="5900")])
    post_cost(alm, datetime.date(2026, 11, 5), "5900", "1000.05")
    a = make_agreement("cost_plus", cost_pool=pool, allocation_basis="fixed_pct", allocation_pct=D("33.333"),
                       markup_pct=D("7.5"))
    inv = bill_nov(accountant, alm, nov, a)
    # 1000.05 x 0.33333 x 1.075 = 358.3477 -> 358.35; VAT 53.7525 -> 53.75
    assert inv.net_total == D("358.35") and inv.vat_total == D("53.75")


def test_d12_headcount_basis_uses_driver_share(make_agreement, accountant, alm, lfn, nov):
    pool = services.create_cost_pool(accountant, alm, code="HR", name_en="HR team", name_ar="فريق الموارد",
                                     accounts=[Account.objects.get(entity=alm, code="5100")])
    post_cost(alm, datetime.date(2026, 11, 5), "5100", "60000")
    a = make_agreement("cost_plus", cost_pool=pool, allocation_basis="headcount", markup_pct=D("10"))
    run = services.run_billing(accountant, alm, nov)
    assert run.log[0]["code"] == "drivers_missing" and a.invoices.count() == 0
    services.set_driver(accountant, pool, nov, "headcount", lfn, "30")
    services.set_driver(accountant, pool, nov, "headcount", alm, "10")  # issuer's own share stays unbilled
    services.run_billing(accountant, alm, nov)
    inv = a.invoices.get()
    assert inv.net_total == D("49500.00")  # 60,000 x 30/40 x 1.10
    assert inv.lines.get().pricing_basis == "cost 60,000.00 x headcount 30/40 + 10%"
    with pytest.raises(BilingualError, match="locked"):
        services.set_driver(accountant, pool, nov, "headcount", lfn, "1")
    with pytest.raises(RuleError):
        services.set_driver(accountant, pool, period(alm, "2026-12"), "revenue", lfn, "-1")


def test_d12_revenue_basis(make_agreement, accountant, alm, lfn, nov):
    pool = services.create_cost_pool(accountant, alm, code="LEG", name_en="Legal", name_ar="القانونية",
                                     accounts=[Account.objects.get(entity=alm, code="5300")])
    post_cost(alm, datetime.date(2026, 11, 5), "5300", "10000")
    a = make_agreement("cost_plus", cost_pool=pool, allocation_basis="revenue", markup_pct=D("0"))
    services.set_driver(accountant, pool, nov, "revenue", lfn, "250000")
    services.set_driver(accountant, pool, nov, "revenue", alm, "750000")
    services.set_driver(accountant, pool, nov, "headcount", lfn, "99")  # other basis ignored
    assert bill_nov(accountant, alm, nov, a).net_total == D("2500.00")


def test_r6_empty_pool_bills_nothing(make_agreement, accountant, alm, nov):
    pool = services.create_cost_pool(accountant, alm, code="P", name_en="P", name_ar="ب",
                                     accounts=[Account.objects.get(entity=alm, code="5100")])
    a = make_agreement("cost_plus", cost_pool=pool, allocation_basis="fixed_pct", allocation_pct=D("50"),
                       markup_pct=D("5"))
    run = services.run_billing(accountant, alm, nov)
    assert run.log[0]["code"] == "pool_empty" and not a.invoices.exists()


# ---------------------------------------------------------------- R7


def test_r7_pool_accounts_rules_and_posted_entries_only(accountant, alm, lfn, nov, make_user):
    pool = services.create_cost_pool(accountant, alm, code="P", name_en="P", name_ar="ب")
    for acc, msg in [(Account.objects.get(entity=lfn, code="5100"), "another entity"),
                     (Account.objects.get(entity=alm, code="1100"), "expense"),
                     (Account.objects.get(entity=alm, code="5000"), "postable"),
                     (Account.objects.get(entity=alm, code="5800"), "cannot be recharged")]:
        with pytest.raises(RuleError, match=msg):
            services.set_pool_accounts(accountant, pool, [acc])
    services.set_pool_accounts(accountant, pool, [Account.objects.get(entity=alm, code="5100")])
    post_cost(alm, datetime.date(2026, 11, 5), "5100", "100")
    draft = ledger.create_entry(alm, datetime.date(2026, 11, 6), "Draft",
                                [{"account": "5100", "debit": "999"}, {"account": "1100", "credit": "999"}],
                                user=accountant)
    assert draft.status == "draft"
    reversed_ = post_cost(alm, datetime.date(2026, 11, 7), "5100", "50")
    ledger.reverse_entry(reversed_, user=accountant, reason="Wrong")
    post_cost(lfn, datetime.date(2026, 11, 5), "5100", "777")  # another entity
    assert services.pool_cost(pool, nov) == D("100.00")
    with pytest.raises(AccessDenied):
        services.create_cost_pool(make_user("pa", (alm, "pa_drafter")), alm, code="X", name_en="X", name_ar="س")


# ---------------------------------------------------------------- R8


def test_r8_vat_from_tax_settings_at_supply_date_and_zero_rated(make_agreement, accountant, mp, alm, nov):
    VatRate.objects.create(country="SA", rate=D("20"), valid_from=datetime.date(2026, 11, 15))
    std = make_agreement()
    zero = make_agreement(vat_treatment="zero_rated", vat_exemption_reason_en="Export of services",
                          vat_exemption_reason_ar="تصدير خدمات")
    services.run_billing(accountant, alm, nov)
    s_inv, z_inv = std.invoices.get(), zero.invoices.get()
    assert s_inv.lines.get().vat_rate == D("20.00") and s_inv.vat_total == D("2000.00")
    zl = z_inv.lines.get()
    assert (zl.vat_rate, zl.vat_amount, zl.vat_category) == (D("0"), D("0"), "Z")
    assert zl.vat_exemption_reason_en == "Export of services"
    z_inv = issue(z_inv, accountant, mp)
    codes = sorted(l.account.code for l in z_inv.journal_entry.lines.all())
    assert codes == ["1210", "4210"]  # 5.1: no 2200 line; 1210 = net


def test_r8_no_vat_rate_skips_with_reason(make_agreement, accountant, alm, nov):
    VatRate.objects.all().delete()
    a = make_agreement()
    run = services.run_billing(accountant, alm, nov)
    assert run.log[0]["code"] == "no_vat_rate" and not a.invoices.exists()


def test_r8_totals_equal_sum_of_lines_with_billable_items(make_agreement, accountant, alm, nov):
    a = make_agreement()
    services.add_billable_item(accountant, a, period_start=datetime.date(2026, 11, 1),
                               period_end=datetime.date(2026, 11, 30), description_en="Fuel recharge",
                               description_ar="إعادة تحميل الوقود", quantity="12.5", unit_price="2.333")
    services.add_billable_item(accountant, a, period_start=datetime.date(2026, 12, 1),
                               period_end=datetime.date(2026, 12, 31), description_en="Later", description_ar="لاحق",
                               quantity="1", unit_price="5")
    inv = bill_nov(accountant, alm, nov, a)
    nets = sorted(l.net_amount for l in inv.lines.all())
    assert nets == [D("29.16"), D("10000.00")]  # 12.5 x 2.333 = 29.1625 -> 29.16
    assert inv.net_total == sum(nets) and inv.vat_total == sum(l.vat_amount for l in inv.lines.all())
    assert inv.gross_total == inv.net_total + inv.vat_total
    assert BillableItem.objects.filter(status="billed").count() == 1


# ---------------------------------------------------------------- R9


def test_r9_run_creates_drafts_only_and_needs_a_human_with_run_right(make_agreement, accountant, make_user, alm, nov):
    make_agreement()
    pa = make_user("pa", (alm, "pa_drafter"))
    with pytest.raises(AccessDenied):
        services.run_billing(pa, alm, nov)
    auditor = make_user("aud", (alm, "auditor"))
    with pytest.raises(AccessDenied):
        services.run_billing(auditor, alm, nov)
    services.run_billing(accountant, alm, nov)
    assert set(INV.objects.values_list("status", flat=True)) == {"draft"}
    assert not ApprovalRequest.objects.filter(document_type__code="intercompany.invoice").exists()
    assert AuditLog.objects.filter(action="intercompany.billing.run", actor=accountant).exists()


def test_r9_unexpected_failure_rolls_back_and_records_failed_run(make_agreement, accountant, alm, nov, monkeypatch):
    make_agreement()

    def boom(*a, **k):
        raise ZeroDivisionError

    monkeypatch.setattr(services, "_build_lines", boom)
    with pytest.raises(ZeroDivisionError):
        services.run_billing(accountant, alm, nov)
    run = BillingRun.objects.get()
    assert run.status == "failed" and not INV.objects.exists()


# ---------------------------------------------------------------- R10


def test_r10_reject_releases_and_return_goes_back_to_draft(make_agreement, accountant, mp, alm, nov):
    a = make_agreement("timesheet")
    e = services.create_timesheet(accountant, a, date=datetime.date(2026, 11, 3), hours=D("2"),
                                  description_en="Work", description_ar="عمل")
    services.submit_timesheet(accountant, e)
    services.approve_timesheet(mp, e)
    inv = bill_nov(accountant, alm, nov, a)
    r = approvals.submit(inv, accountant)
    approvals.act(r, mp, "return", comment="Check hours", document_version=r.document_version)
    inv.refresh_from_db()
    assert inv.status == "draft"
    r = approvals.submit(inv, accountant)
    approvals.act(r, mp, "reject", comment="Not billable", document_version=r.document_version)
    inv.refresh_from_db()
    e.refresh_from_db()
    assert inv.status == "rejected" and e.status == "approved" and e.invoice_line is None
    again = services.create_invoice_draft(accountant, a, nov)  # the period can be billed again
    assert again.net_total == D("900.00")
    with pytest.raises(BilingualError):
        inv.status = "draft"
        inv.save()


def test_r10_agreement_lifecycle_through_approvals(make_agreement, accountant, mp, alm, nov):
    a = make_agreement(activate=False)
    r = approvals.submit(a, accountant)
    a.refresh_from_db()
    assert a.status == "pending_approval"
    approvals.act(r, mp, "reject", comment="No", document_version=r.document_version)
    a.refresh_from_db()
    assert a.status == "ended"
    b = make_agreement()
    assert b.status == "active" and b.activated_at
    services.suspend_agreement(accountant, b, "Pause")
    approvals.submit(b, accountant)  # resuming needs approval again
    b.refresh_from_db()
    assert b.status == "pending_approval"
    approve(b, mp)
    b.refresh_from_db()
    assert b.status == "active"
    with pytest.raises(RuleError, match="reason"):
        services.end_agreement(accountant, b, datetime.date(2026, 12, 31), "")
    services.end_agreement(accountant, b, datetime.date(2026, 10, 31), "Restructured")
    b.refresh_from_db()
    assert b.status == "ended" and b.end_date == datetime.date(2026, 10, 31)
    assert services.run_billing(accountant, alm, nov).invoices_created == 0


def test_r10_editor_of_a_draft_cannot_approve_it(make_agreement, accountant, mp, alm, nov):
    """approvals R8 / review A: editors are recorded (DraftEditor) and count as creators."""
    a = make_agreement(user=mp)  # MP creates; the accountant could not approve anyway
    inv = services.create_invoice_draft(accountant, a, nov)
    line = inv.lines.get()
    services.update_invoice_draft(accountant, inv, lines={line.pk: {"description_en": "Finance services Nov"}})
    assert DraftEditor.objects.filter(object_id=str(inv.pk), user=accountant).count() == 2
    with pytest.raises(BilingualError):
        services.update_invoice_draft(mp, inv, lines={line.pk: {"description_en": "x"}})  # creator only
    r = approvals.submit(inv, accountant)
    assert set(r.document_editors.values_list("username", flat=True)) == {"acc"}
    with pytest.raises(BilingualError):
        DraftEditor.objects.first().delete()


# ---------------------------------------------------------------- R11, R12


def test_r11_numbers_are_gap_free_per_entity_and_year(make_agreement, accountant, mp, alm, nov, monkeypatch):
    a1, a2 = make_agreement(), make_agreement()
    services.run_billing(accountant, alm, nov)
    i1, i2 = a1.invoices.get(), a2.invoices.get()
    issue(i2, accountant, mp)
    issue(i1, accountant, mp)
    assert sorted([i1.number, i2.number]) == ["ALM-ICI-2026-000001", "ALM-ICI-2026-000002"]
    monkeypatch.setattr(services, "today", lambda: datetime.date(2027, 2, 2))
    services.run_billing(accountant, alm, period(alm, "2027-01"))
    jan = issue(a1.invoices.get(billing_period="2027-01"), accountant, mp)
    assert jan.number == "ALM-ICI-2027-000001" and jan.journal_entry.number.startswith("ALM-JE-2027-")


def test_r11_ledger_failure_rolls_back_and_status_never_set_directly(make_agreement, accountant, mp, alm, nov):
    inv = bill_nov(accountant, alm, nov, make_agreement())
    inv.status = "issued"
    with pytest.raises(BilingualError):
        inv.save()
    inv.refresh_from_db()
    inv.number = "ALM-ICI-2026-999999"
    with pytest.raises(BilingualError):
        inv.save()


def test_r11_missing_vat_number_blocks_vat_invoice(make_agreement, accountant, alm, nov):
    alm.vat_number = ""
    alm.save()
    inv = bill_nov(accountant, alm, nov, make_agreement())
    with pytest.raises(RuleError, match="VAT number"):
        approvals.submit(inv, accountant)


def test_r12_every_line_carries_the_counterparty_including_bills(issued_invoice, lfn_acc, mp, alm, lfn):
    inv = issued_invoice()
    services.submit_bill(lfn_acc, inv.mirror_bill)
    approve(inv.mirror_bill, mp)
    for je in JournalEntry.objects.filter(pk__in=[inv.journal_entry_id, IntercompanyBill.objects.get().journal_entry_id]):
        cps = {l.counterparty_entity_id for l in je.lines.all()}
        assert cps == {lfn.pk if je.entity == alm else alm.pk}


# ---------------------------------------------------------------- R13, R14


def test_r13_credit_note_checks(issued_invoice, make_agreement, accountant, mp, alm, nov):
    inv = issued_invoice()
    with pytest.raises(RuleError, match="reason"):
        services.create_credit_note(accountant, inv, reason_en="Fix", reason_ar="")
    with pytest.raises(RuleError, match="before"):
        services.create_credit_note(accountant, inv, reason_en="Fix", reason_ar="تصحيح",
                                    issue_date=datetime.date(2026, 11, 1))
    with pytest.raises(RuleError, match="exceeds"):
        services.create_credit_note(accountant, inv, reason_en="Fix", reason_ar="تصحيح",
                                    lines=[{"line": inv.lines.get(), "net_amount": "10000.01"}])
    cn = services.create_credit_note(accountant, inv, reason_en="Fix", reason_ar="تصحيح")
    with pytest.raises(Conflict):
        services.create_credit_note(accountant, cn, reason_en="x", reason_ar="س")  # no credit of a credit
    draft = make_agreement()
    services.run_billing(accountant, alm, nov)
    with pytest.raises(Conflict, match="issued"):
        services.create_credit_note(accountant, draft.invoices.get(), reason_en="x", reason_ar="س")


def test_r14_cancel_draft_releases_everything_and_numbers_never_reused(make_agreement, accountant, mp, alm, nov):
    pool = services.create_cost_pool(accountant, alm, code="P", name_en="P", name_ar="ب",
                                     accounts=[Account.objects.get(entity=alm, code="5100")])
    post_cost(alm, datetime.date(2026, 11, 5), "5100", "1000")
    a = make_agreement("cost_plus", cost_pool=pool, allocation_basis="fixed_pct", allocation_pct=D("50"),
                       markup_pct=D("0"))
    t = make_agreement("timesheet")
    e = services.create_timesheet(accountant, t, date=datetime.date(2026, 11, 3), hours=D("1"),
                                  description_en="Work", description_ar="عمل")
    services.submit_timesheet(accountant, e)
    services.approve_timesheet(mp, e)
    item = services.add_billable_item(accountant, t, period_start=datetime.date(2026, 11, 1),
                                      period_end=datetime.date(2026, 11, 30), description_en="Fuel",
                                      description_ar="وقود", quantity="1", unit_price="10")
    services.run_billing(accountant, alm, nov)
    cp_inv, ts_inv = a.invoices.get(), t.invoices.get()
    assert CostPoolPeriod.objects.get().locked
    issued = issue(ts_inv, accountant, mp)
    with pytest.raises(Conflict):
        services.cancel_draft(accountant, issued)
    services.cancel_draft(accountant, cp_inv, "Wrong pool")
    assert not CostPoolPeriod.objects.get().locked
    assert INV.objects.get(pk=cp_inv.pk).status == "cancelled_draft"
    post_cost(alm, datetime.date(2026, 11, 6), "5100", "1000")  # pool changed; recomputed when billed again
    again = services.create_invoice_draft(accountant, a, nov)
    assert again.net_total == D("1000.00") and again.number == ""
    e.refresh_from_db()
    item.refresh_from_db()
    assert e.status == "billed" and item.status == "billed"  # still on the issued invoice


# ---------------------------------------------------------------- R15, R16, R17 (D8)


def test_r15_no_mirror_before_books_in_erp_from(issued_invoice, lfn):
    lfn.books_in_erp_from = datetime.date(2026, 12, 1)
    lfn.save()
    inv = issued_invoice()
    assert inv.status == "issued" and not IntercompanyBill.objects.exists()


def test_r15_entity_books_in_erp_needs_a_start_date(alm):
    from django.core.exceptions import ValidationError

    e = Entity(code="NEW", name_en="New", name_ar="جديد", parent=alm, books_in_erp=True)
    with pytest.raises(ValidationError):
        e.full_clean()


def test_r16_dispute_and_locked_coding_after_posting(issued_invoice, lfn_acc, mp, lfn):
    bill = issued_invoice().mirror_bill
    with pytest.raises(RuleError, match="reason"):
        services.dispute_bill(lfn_acc, bill, "")
    services.dispute_bill(lfn_acc, bill, "Amount not agreed")
    bill.refresh_from_db()
    assert bill.status == "disputed"
    with pytest.raises(Conflict):
        services.submit_bill(lfn_acc, bill)
    services.update_bill_coding(lfn_acc, bill, {bill.lines.get().pk: {"cost_centre": "HQ"}})  # coding still allowed
    services.undispute_bill(lfn_acc, bill)
    with pytest.raises(RuleError, match="Sharia"):
        services.update_bill_coding(lfn_acc, bill, {bill.lines.get().pk: {"expense_account": "5610"}})
    with pytest.raises(RuleError, match="expense"):
        services.update_bill_coding(lfn_acc, bill, {bill.lines.get().pk: {"expense_account": "1100"}})
    services.submit_bill(lfn_acc, bill)
    approve(bill, mp)
    bill.refresh_from_db()
    assert bill.status == "posted" and bill.journal_entry.entity == lfn and bill.prepared_by == lfn_acc
    with pytest.raises(Conflict):
        services.update_bill_coding(lfn_acc, bill, {bill.lines.get().pk: {"cost_centre": "X"}})
    line = bill.lines.get()
    line.cost_centre = "X"
    with pytest.raises(BilingualError):
        line.save()


def test_r16_rejected_bill_becomes_disputed_and_bills_are_system_made(issued_invoice, lfn_acc, mp, lfn, make_user):
    bill = issued_invoice().mirror_bill
    r = services.submit_bill(lfn_acc, bill)
    approvals.act(r, mp, "reject", comment="Fee not agreed", document_version=r.document_version)
    bill.refresh_from_db()
    assert bill.status == "disputed" and bill.dispute_reason == "Fee not agreed"
    other = make_user("other", (lfn, "accountant"))
    services.undispute_bill(other, bill)
    with pytest.raises(AccessDenied, match="prepared"):
        services.submit_bill(other, bill)
    with pytest.raises(BilingualError, match="system"):
        IntercompanyBill.objects.create(entity=lfn, counterparty=bill.counterparty, invoice=bill.invoice, number="X",
                                        bill_date=bill.bill_date, due_date=bill.due_date, currency="SAR",
                                        net_total=1, vat_total=0, gross_total=1)


def test_r17_bill_credit_follows_original_coding_and_waits_for_it(issued_invoice, accountant, lfn_acc, mp):
    inv = issued_invoice()
    bill = inv.mirror_bill
    services.update_bill_coding(lfn_acc, bill, {bill.lines.get().pk: {"expense_account": "5900"}})
    cn = services.create_credit_note(accountant, inv, reason_en="Full credit", reason_ar="إشعار كامل")
    issue(cn, accountant, mp)
    credit = cn.mirror_bill
    assert credit.kind == "credit" and credit.lines.get().expense_account.code == "5900"
    with pytest.raises(RuleError, match="original bill"):
        services.submit_bill(lfn_acc, credit)
    services.submit_bill(lfn_acc, bill)
    approve(bill, mp)
    services.submit_bill(lfn_acc, credit)
    approve(credit, mp)
    credit.refresh_from_db()
    codes = sorted((l.account.code, l.debit, l.credit) for l in credit.journal_entry.lines.all())
    assert codes == [("1310", D("0"), D("1500.00")), ("2110", D("11500.00"), D("0")), ("5900", D("0"), D("10000.00"))]
    bal = ledger.intercompany_balances(datetime.date(2026, 12, 31))
    assert bal[("ALM", "LFN")] == 0 and bal[("LFN", "ALM")] == 0


def test_r17_no_bill_credit_when_original_had_no_mirror(issued_invoice, lfn_off, accountant, mp):
    inv = issued_invoice(counterparty=lfn_off)
    cn = issue(services.create_credit_note(accountant, inv, reason_en="Full", reason_ar="كامل"), accountant, mp)
    assert cn.status == "issued" and not IntercompanyBill.objects.exists()


# ---------------------------------------------------------------- R18


def test_r18_income_and_expense_overrides_must_be_compliant(accountant, alm, lfn, fin):
    kw = dict(entity=alm, counterparty=lfn, service_type=fin, pricing_method="fixed_fee", fixed_fee=D("1"), **BASE)
    with pytest.raises(RuleError, match="Sharia"):
        a = services.create_agreement(accountant, expense_account_code="5610", **kw)
        approvals.submit(a, accountant)
    with pytest.raises(RuleError):
        services.create_agreement(accountant, income_account_code="4900", **kw)  # purification account
    with pytest.raises(RuleError):
        services.create_agreement(accountant, income_account_code="1100", **kw)  # not income


# ---------------------------------------------------------------- R19


def test_r19_visibility_by_entity_and_own_drafts(make_agreement, accountant, make_user, alm, lfn, nov):
    a = make_agreement()
    services.run_billing(accountant, alm, nov)
    pa = make_user("pa", (alm, "pa_drafter"))
    auditor = make_user("aud", (alm, "auditor"))
    analyst = make_user("ana", (alm, "business_analyst"))
    outsider = make_user("out", (lfn, "accountant"))
    assert list(services.visible_agreements(auditor)) == [a]
    assert services.visible_invoices(auditor).count() == 1
    assert not services.visible_agreements(analyst).exists() and not services.visible_invoices(analyst).exists()
    assert not services.visible_agreements(pa).exists()  # PA sees own drafts only
    own = make_agreement(user=pa, activate=False)
    assert list(services.visible_agreements(pa)) == [own]
    assert not services.visible_agreements(outsider).exists() and not services.visible_invoices(outsider).exists()
    with pytest.raises(AccessDenied):
        services.create_agreement(analyst, entity=alm, counterparty=lfn, service_type=ServiceType.objects.first(),
                                  pricing_method="fixed_fee", fixed_fee=D("1"), **BASE)


# ---------------------------------------------------------------- R20 (agents) and D10 (source)


def test_r20_agent_records_are_drafts_with_source(make_agreement, make_user, alm, nov, accountant):
    bot = make_user("ic-bot", (alm, "agent_drafter"), user_type="service_agent", agent_name="ic-bot")
    a = make_agreement(user=bot, activate=False)
    assert (a.source, a.source_agent, a.status) == ("agent", "ic-bot", "draft")
    active = make_agreement("timesheet")
    e = services.create_timesheet(bot, active, worker=accountant, date=datetime.date(2026, 11, 3), hours=D("2"),
                                  description_en="Parsed", description_ar="مستخرج")
    assert e.source == "agent" and e.status == "draft"
    with pytest.raises(AccessDenied):
        services.submit_timesheet(bot, e)
    services.submit_timesheet(accountant, e)  # the worker submits the agent's draft
    with pytest.raises(AccessDenied):
        services.approve_timesheet(bot, e)
    with pytest.raises(AccessDenied):
        services.suspend_agreement(bot, active, "x")
    human = services.create_invoice_draft(accountant, make_agreement(), nov)
    assert (human.source, human.source_agent) == ("human", "")
    from django.core.exceptions import ValidationError

    bad = INV(entity=alm, counterparty=human.counterparty, agreement=human.agreement, source="agent", source_agent="")
    with pytest.raises(ValidationError):
        bad.validate_constraints()


# ---------------------------------------------------------------- R21


def test_r21_bilingual_fields_required(accountant, alm, lfn, fin):
    with pytest.raises(RuleError, match="description_ar"):
        services.create_agreement(accountant, entity=alm, counterparty=lfn, service_type=fin, pricing_method="fixed_fee",
                                  fixed_fee=D("1"), **{**BASE, "description_ar": ""})
    with pytest.raises(RuleError, match="name_ar"):
        services.create_cost_pool(accountant, alm, code="P", name_en="P", name_ar=" ")


# ---------------------------------------------------------------- R22 (ZATCA hand-off)


def test_r22_zatca_status_written_only_by_zatca_module(issued_invoice):
    inv = issued_invoice()
    inv.zatca_status = "cleared"
    with pytest.raises(BilingualError, match="ZATCA"):
        inv.save()
    with pytest.raises(NotImplementedError):
        zatca.zatca_submit(inv)
    payload = zatca.handoff_payload(INV.objects.get(pk=inv.pk))
    assert payload["number"] == inv.number and payload["invoice_type_code"] == "388"
    assert payload["seller"]["vat_number"] == "300000000000003" and payload["buyer"]["vat_number"] == "300000000000013"
    assert payload["lines"][0]["vat_category"] == "S" and payload["vat_total"] == "1500.00"
    inv = zatca.set_zatca_status(inv, "reported", "abc")
    assert inv.zatca_status == "reported" and inv.zatca_hash == "abc"


def test_r22_zatca_submit_not_called_on_issue(make_agreement, accountant, mp, alm, nov, monkeypatch):
    called = []
    monkeypatch.setattr(zatca, "zatca_submit", lambda inv: called.append(inv))
    issue(bill_nov(accountant, alm, nov, make_agreement()), accountant, mp)
    assert called == []


def test_r22_draft_is_not_handed_off(make_agreement, accountant, alm, nov):
    inv = bill_nov(accountant, alm, nov, make_agreement())
    with pytest.raises(RuleError):
        zatca.handoff_payload(inv)


# ---------------------------------------------------------------- bulk changes and line guards


def test_bulk_update_and_delete_refused(make_agreement, accountant, alm, nov):
    inv = bill_nov(accountant, alm, nov, make_agreement())
    for qs in (INV.objects.all(), InvoiceLine.objects.all(), A.objects.all(), TimesheetEntry.objects.all(),
               AllocationDriver.objects.all(), DraftEditor.objects.all()):
        with pytest.raises(BilingualError):
            qs.update(created_at=None) if hasattr(qs.model, "created_at") else qs.update(id=1)
        with pytest.raises(BilingualError):
            qs.delete()
    with pytest.raises(BilingualError, match="cancel"):
        inv.delete()
    with workflow(inv):
        pass  # the flag alone changes nothing
    assert INV.objects.filter(pk=inv.pk, status="draft").exists()
