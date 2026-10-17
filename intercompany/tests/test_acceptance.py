"""Acceptance tests T1-T23 of docs/specs/intercompany-billing.md §8."""
import datetime
from decimal import Decimal

import pytest

from access.errors import AccessDenied, BilingualError, RuleError
from access.models import AuditLog
from approvals import services as approvals
from approvals.models import ApprovalPolicy, ApprovalRequest, ApprovalStep
from approvals.services import PostingFailed
from core.models import Entity
from intercompany import services
from intercompany.models import (
    CostPool, CostPoolPeriod, IntercompanyBill, IntercompanyInvoice, NumberSequence, ServiceAgreement, TimesheetEntry,
)
from ledger import services as ledger
from ledger.models import Account, FiscalPeriod, JournalEntry, JournalLine
from ledger.services_setup import create_monthly_periods, install_chart_of_accounts

from .conftest import approve

pytestmark = pytest.mark.django_db
D = Decimal
INV = IntercompanyInvoice
NOV30 = datetime.date(2026, 11, 30)


def lines_of(entry):
    return sorted((l.account.code, l.debit, l.credit, l.counterparty_entity.code) for l in entry.lines.all())


# ---------------------------------------------------------------- T1, T2: fixed fee, idempotent run


def test_t1_fixed_fee_run_creates_one_draft_without_journal_entry(make_agreement, accountant, alm, nov):
    a = make_agreement()
    entries_before = JournalEntry.objects.count()
    run = services.run_billing(accountant, alm, nov)
    assert run.status == "completed" and run.invoices_created == 1
    inv = a.invoices.get()
    assert inv.status == INV.Status.DRAFT and inv.number == "" and inv.journal_entry is None
    assert (inv.net_total, inv.vat_total, inv.gross_total) == (D("10000.00"), D("1500.00"), D("11500.00"))
    assert inv.issue_date == NOV30 and inv.billing_period == "2026-11"  # D12
    assert (inv.supply_date_from, inv.supply_date_to) == (datetime.date(2026, 11, 1), NOV30)
    assert inv.due_date == NOV30 + datetime.timedelta(days=30)
    assert JournalEntry.objects.count() == entries_before


def test_t2_second_run_creates_nothing_and_logs_already_billed(make_agreement, accountant, alm, nov):
    a = make_agreement()
    services.run_billing(accountant, alm, nov)
    run2 = services.run_billing(accountant, alm, nov)
    assert run2.invoices_created == 0
    assert a.invoices.count() == 1
    assert [r["result"] for r in run2.log] == ["already billed"]


# ---------------------------------------------------------------- T3, T4: timesheets


def test_t3_timesheet_bills_only_approved_entries(make_agreement, accountant, make_user, alm, nov):
    a = make_agreement("timesheet")
    clerk = make_user("clerk", (alm, "pa_drafter"))
    e1 = services.create_timesheet(clerk, a, date=datetime.date(2026, 11, 3), hours=D("10"),
                                   description_en="Month-end close", description_ar="إقفال الشهر")
    e2 = services.create_timesheet(clerk, a, date=datetime.date(2026, 11, 4), hours=D("2.5"),
                                   description_en="VAT return", description_ar="إقرار الضريبة")
    e3 = services.create_timesheet(clerk, a, date=datetime.date(2026, 11, 5), hours=D("3"),
                                   description_en="Audit queries", description_ar="استفسارات المراجع")
    for e in (e1, e2, e3):
        services.submit_timesheet(clerk, e)
    services.approve_timesheet(accountant, e1)
    services.approve_timesheet(accountant, e2)
    services.run_billing(accountant, alm, nov)
    inv = a.invoices.get()
    line = inv.lines.get()
    assert line.quantity == D("12.5") and line.unit_price == D("450") and line.net_amount == D("5625.00")
    assert line.pricing_basis == "12.5 h x 450.00"
    for e in (e1, e2):
        e.refresh_from_db()
        assert e.status == "billed" and e.invoice_line == line
    e3.refresh_from_db()
    assert e3.status == "submitted" and e3.invoice_line is None


def test_t4_billed_timesheet_entry_cannot_be_edited(make_agreement, accountant, alm, nov):
    a = make_agreement("timesheet")
    e = services.create_timesheet(accountant, a, worker=accountant, date=datetime.date(2026, 11, 3), hours=D("4"),
                                  description_en="Close", description_ar="إقفال")
    services.submit_timesheet(accountant, e)
    TimesheetEntry.objects.filter(pk=e.pk)  # noqa: B018
    from intercompany.models import workflow

    with workflow(e):  # approval by a second person (not the worker) is tested elsewhere
        e.status = "approved"
        e.save()
    services.run_billing(accountant, alm, nov)
    e.refresh_from_db()
    assert e.status == "billed"
    e.hours = D("5")
    with pytest.raises(BilingualError, match="locked"):
        e.save()
    with pytest.raises(BilingualError):
        services.update_timesheet(accountant, e, hours=D("5"))
    assert TimesheetEntry.objects.get(pk=e.pk).hours == D("4.00")


# ---------------------------------------------------------------- T5, T6: cost plus


def _post_cost(entity, day, account, amount):
    return ledger.create_entry(entity, day, "Cost", [{"account": account, "debit": amount},
                                                     {"account": "1100", "credit": amount}], system=True, post=True)


def test_t5_cost_plus_amount_and_pool_lock(make_agreement, accountant, alm, nov):
    pool = services.create_cost_pool(accountant, alm, code="FINPOOL", name_en="Finance team", name_ar="فريق المالية",
                                     accounts=[Account.objects.get(entity=alm, code="5100")])
    _post_cost(alm, datetime.date(2026, 11, 10), "5100", "80000")
    _post_cost(alm, datetime.date(2026, 10, 10), "5100", "999")  # other period, ignored
    a = make_agreement("cost_plus", cost_pool=pool, allocation_basis="fixed_pct", allocation_pct=D("25"),
                       markup_pct=D("10"))
    services.run_billing(accountant, alm, nov)
    inv = a.invoices.get()
    assert inv.net_total == D("22000.00") and inv.vat_total == D("3300.00")
    cpp = CostPoolPeriod.objects.get(cost_pool=pool, period=nov)
    assert cpp.amount == D("80000.00") and cpp.locked
    assert inv.lines.get().pricing_basis == "cost 80,000.00 x 25% + 10%"
    cpp.amount = D("1")
    with pytest.raises(BilingualError, match="locked"):
        cpp.save()


def test_t6_pool_with_prohibited_account_rejected(accountant, alm):
    prohibited = Account.objects.get(entity=alm, code="5610")
    with pytest.raises(RuleError, match="Sharia-prohibited"):
        services.create_cost_pool(accountant, alm, code="BAD", name_en="Bad", name_ar="سيئ", accounts=[prohibited])
    assert not CostPool.objects.filter(code="BAD").exists()
    pool = services.create_cost_pool(accountant, alm, code="OK", name_en="Ok", name_ar="جيد")
    from django.db import transaction

    with pytest.raises(RuleError, match="Sharia-prohibited"), transaction.atomic():
        pool.accounts.add(prohibited)  # any path (admin, shell) is checked
    assert pool.accounts.count() == 0


# ---------------------------------------------------------------- T7, T8: approval and issue


def test_t7_accountant_cannot_issue_without_mp_approval(make_agreement, accountant, alm, nov):
    a = make_agreement()
    services.run_billing(accountant, alm, nov)
    inv = a.invoices.get()
    with pytest.raises(RuleError, match="Managing Partner approval"):
        services.issue_invoice(inv, accountant)
    approvals.submit(inv, accountant)
    with pytest.raises(RuleError):
        services.issue_invoice(inv, accountant)
    inv.refresh_from_db()
    assert inv.status == "pending_approval" and inv.number == "" and inv.journal_entry is None
    assert AuditLog.objects.filter(action="intercompany.invoice.issue_refused", object_id=str(inv.pk)).count() == 2
    with pytest.raises(AccessDenied):  # creator cannot approve (approvals R8)
        approve(inv, accountant)


def test_t8_mp_approval_issues_with_number_and_journal_entry(issued_invoice, mp, lfn, alm):
    inv = issued_invoice()
    assert inv.status == INV.Status.ISSUED and inv.number == "ALM-ICI-2026-000001"
    assert inv.approved_by == mp and inv.zatca_ready and inv.zatca_uuid and inv.zatca_status == "not_submitted"
    je = inv.journal_entry
    assert je.status == JournalEntry.Status.POSTED and je.entity == alm and je.date == NOV30
    assert not je.requires_approval and je.reference == inv.number
    assert lines_of(je) == [("1210", D("11500.00"), D("0"), "LFN"), ("2200", D("0"), D("1500.00"), "LFN"),
                            ("4210", D("0"), D("10000.00"), "LFN")]
    assert all(l.counterparty_entity == lfn for l in je.lines.all())  # R12


# ---------------------------------------------------------------- T9, T10: mirror bills


def test_t9_mirror_bill_draft_in_receiver(issued_invoice, lfn):
    inv = issued_invoice()
    bill = inv.mirror_bill
    assert bill.entity == lfn and bill.counterparty.code == "ALM" and bill.status == "draft"
    assert bill.number == "LFN-ICB-2026-000001" and bill.source == "import" and bill.source_agent == ""
    assert (bill.net_total, bill.vat_total, bill.gross_total) == (D("10000.00"), D("1500.00"), D("11500.00"))
    assert bill.bill_date == inv.issue_date and bill.due_date == inv.due_date
    line = bill.lines.get()
    assert line.expense_account.code == "5300" and line.expense_account.entity == lfn
    assert (line.net_amount, line.vat_amount) == (D("10000.00"), D("1500.00"))
    assert not JournalEntry.objects.filter(entity=lfn).exists()  # nothing posted in LFN yet


def test_t10_counterparty_not_on_erp_gets_no_bill(issued_invoice, lfn_off, alm, accountant):
    inv = issued_invoice(counterparty=lfn_off)
    assert inv.status == "issued"
    assert not IntercompanyBill.objects.exists()
    assert not JournalEntry.objects.filter(entity=lfn_off).exists()
    report = services.reconciliation_report(accountant, NOV30)
    row = report["rows"][0]
    assert row["counterparty"] == "LFX" and row["receiver_status"]["status"] == "not_on_erp"
    assert row["receiver_status"]["label_en"] == "Counterparty not on ERP"
    assert row["issuer_balance"] == D("11500.00")


# ---------------------------------------------------------------- T11: mirror bill amounts locked


def test_t11_receiver_changes_coding_but_not_amounts(issued_invoice, lfn_acc, lfn):
    bill = issued_invoice().mirror_bill
    line = bill.lines.get()
    with pytest.raises(BilingualError, match="R16"):
        line.net_amount = D("9000")
        line.save()
    line.refresh_from_db()
    bill.gross_total = D("1")
    with pytest.raises(BilingualError, match="R16"):
        bill.save()
    with pytest.raises(RuleError):
        services.update_bill_coding(lfn_acc, bill, {line.pk: {"net_amount": "9000"}})
    services.update_bill_coding(lfn_acc, bill, {line.pk: {"expense_account": "5900", "cost_centre": "PLANT"}})
    line.refresh_from_db()
    assert line.expense_account.code == "5900" and line.cost_centre == "PLANT" and line.net_amount == D("10000.00")


# ---------------------------------------------------------------- T12: issued invoices locked


def test_t12_issued_invoice_cannot_be_edited_or_deleted(issued_invoice, mp, accountant):
    inv = issued_invoice()
    inv.due_date = inv.due_date + datetime.timedelta(days=5)
    with pytest.raises(BilingualError, match="credit note"):
        inv.save()
    with pytest.raises(BilingualError, match="credit note"):
        INV.objects.get(pk=inv.pk).delete()
    with pytest.raises(BilingualError):
        INV.objects.filter(pk=inv.pk).update(net_total=D("1"))
    with pytest.raises(BilingualError):
        INV.objects.filter(pk=inv.pk).delete()
    line = inv.lines.first()
    line.net_amount = D("1")
    with pytest.raises(BilingualError):
        line.save()
    with pytest.raises(BilingualError):
        line.delete()
    assert INV.objects.get(pk=inv.pk).gross_total == D("11500.00")


# ---------------------------------------------------------------- T13, T14: credit notes


def _credit(inv, user, net, reason="Overbilled hours"):
    return services.create_credit_note(user, inv, reason_en=reason, reason_ar="ساعات زائدة",
                                       lines=[{"line": inv.lines.first(), "net_amount": D(net)}])


def test_t13_partial_credit_note_posts_and_mirrors(issued_invoice, accountant, mp, lfn, alm):
    inv = issued_invoice()
    cn = _credit(inv, accountant, "5000")
    assert cn.gross_total == D("5750.00") and cn.vat_total == D("750.00") and cn.status == "draft"
    approvals.submit(cn, accountant)
    approve(cn, mp)
    cn.refresh_from_db()
    assert cn.status == "issued" and cn.number == "ALM-ICC-2026-000001" and cn.invoice_type_code == "381"
    assert lines_of(cn.journal_entry) == [("1210", D("0"), D("5750.00"), "LFN"), ("2200", D("750.00"), D("0"), "LFN"),
                                          ("4210", D("5000.00"), D("0"), "LFN")]
    assert services.uncredited_balance(inv) == D("5750.00")
    bill_credit = cn.mirror_bill
    assert bill_credit.entity == lfn and bill_credit.kind == "credit" and bill_credit.status == "draft"
    assert bill_credit.credit_of == inv.mirror_bill and bill_credit.gross_total == D("5750.00")
    assert not JournalEntry.objects.filter(entity=lfn).exists()


def test_t14_second_credit_note_over_balance_rejected(issued_invoice, accountant, mp):
    inv = issued_invoice()
    cn = _credit(inv, accountant, "5000")
    approvals.submit(cn, accountant)
    approve(cn, mp)
    with pytest.raises(RuleError, match="R13"):
        _credit(inv, accountant, "5217.39")  # 6,000.00 gross > 5,750 uncredited
    assert INV.objects.filter(credit_of=inv).count() == 1
    full = services.create_credit_note(accountant, inv, reason_en="Close out", reason_ar="إقفال")
    assert full.gross_total == D("5750.00")  # exactly the remainder, VAT not re-rounded
    with pytest.raises(RuleError):
        _credit(inv, accountant, "0.01")  # pending drafts count against the balance too


# ---------------------------------------------------------------- T15: closed period


def test_t15_closed_period_rejects_issue_and_keeps_number(make_agreement, accountant, mp, alm, nov):
    a = make_agreement()
    services.run_billing(accountant, alm, nov)
    inv = a.invoices.get()
    approvals.submit(inv, accountant)
    ledger.close_period(nov)
    with pytest.raises(PostingFailed, match="closed"):
        approve(inv, mp)
    inv.refresh_from_db()
    assert inv.status == "pending_approval" and inv.number == "" and inv.journal_entry is None
    assert not NumberSequence.objects.filter(entity=alm, prefix="ICI").exists() or \
        NumberSequence.objects.get(entity=alm, prefix="ICI").last_number == 0
    assert ApprovalRequest.objects.get(object_id=str(inv.pk), document_type__code="intercompany.invoice").status == "pending"
    assert not IntercompanyBill.objects.exists()


# ---------------------------------------------------------------- T16: same entity


def test_t16_counterparty_equal_to_issuer_rejected(accountant, alm, fin):
    with pytest.raises(RuleError, match="another entity"):
        services.create_agreement(accountant, entity=alm, counterparty=alm, service_type=fin,
                                  description_en="x", description_ar="س", pricing_method="fixed_fee",
                                  fixed_fee=D("1"), vat_treatment="standard", billing_frequency="monthly",
                                  payment_terms_days=30, start_date=datetime.date(2026, 1, 1))
    assert not ServiceAgreement.objects.exists()


# ---------------------------------------------------------------- T17: agents draft only


def test_t17_agent_drafts_but_cannot_approve_or_issue(make_agreement, make_user, alm, nov, mp):
    a = make_agreement()
    bot = make_user("billing-bot", (alm, "agent_drafter"), user_type="service_agent", agent_name="billing-bot")
    inv = services.create_invoice_draft(bot, a, nov)
    assert inv.source == "agent" and inv.source_agent == "billing-bot" and inv.status == "draft"
    with pytest.raises(AccessDenied):
        services.run_billing(bot, alm, nov)
    r = approvals.submit(inv, bot)
    assert r.source == "agent:billing-bot"
    with pytest.raises(AccessDenied):
        approvals.act(r, bot, "approve", document_version=r.document_version)
    with pytest.raises(AccessDenied):
        services.issue_invoice(inv, bot)
    assert AuditLog.objects.filter(action="intercompany.invoice.issue_refused", actor=bot).exists()
    assert AuditLog.objects.filter(action="approval.submit", actor=bot, source="agent:billing-bot").exists()
    inv.refresh_from_db()
    assert inv.status == "pending_approval" and inv.number == ""


# ---------------------------------------------------------------- T18: entity scoping


def test_t18_user_with_alm_rights_sees_no_lfn_bills(issued_invoice, accountant, lfn_acc, make_user, alm, lfn):
    inv = issued_invoice()
    assert list(services.visible_bills(accountant)) == []
    assert list(services.visible_bills(lfn_acc)) == [inv.mirror_bill]
    assert list(services.visible_invoices(lfn_acc)) == []
    with pytest.raises(AccessDenied):
        services.update_bill_coding(accountant, inv.mirror_bill, {})


# ---------------------------------------------------------------- T19: no penalties (D11)


def test_t19_no_penalty_or_interest_can_be_configured_or_created(issued_invoice, accountant, alm, monkeypatch):
    inv = issued_invoice()
    names = {f.name for m in (ServiceAgreement, IntercompanyInvoice) for f in m._meta.get_fields()}
    assert not {n for n in names if any(w in n for w in ("penalt", "interest", "late_fee", "finance_charge"))}
    assert not [n for n in dir(services) if "penalt" in n.lower() or "interest" in n.lower()]
    monkeypatch.setattr(services, "today", lambda: datetime.date(2027, 3, 1))  # the invoice is long overdue
    dec = FiscalPeriod.objects.get(entity=alm, name="2026-12")
    services.run_billing(accountant, alm, dec)
    dec_inv = INV.objects.get(billing_period="2026-12")
    assert dec_inv.gross_total == inv.gross_total and dec_inv.lines.count() == 1  # the fee only, no penalty line
    inv.refresh_from_db()
    assert inv.gross_total == D("11500.00")
    assert not JournalLine.objects.filter(entry__entity=alm, account__code__in=["4900", "2600"]).exists()


# ---------------------------------------------------------------- T20, T21: reconciliation


def test_t20_equity_method_associate_balances_match(make_agreement, accountant, mp, make_user, alm, nov):
    assoc = Entity.objects.create(code="EXG", name_en="Associate", name_ar="زميلة", parent=alm,
                                  consolidation="equity", ownership_pct=D("30"), vat_number="300000000000033",
                                  books_in_erp=True, books_in_erp_from=datetime.date(2026, 1, 1))
    install_chart_of_accounts(assoc)
    create_monthly_periods(assoc, 2026)
    from access import services as access

    access.grant_access(None, mp, assoc, "managing_partner", valid_from=datetime.date(2026, 1, 1))
    exg_acc = make_user("exgacc", (assoc, "accountant"))
    a = make_agreement(counterparty=assoc)
    services.run_billing(accountant, alm, nov)
    inv = a.invoices.get()
    approvals.submit(inv, accountant)
    approve(inv, mp)
    inv.refresh_from_db()
    services.submit_bill(exg_acc, inv.mirror_bill)
    approve(inv.mirror_bill, mp)
    report = services.reconciliation_report(mp, NOV30)
    row = report["rows"][0]
    assert row["issuer_balance"] == row["receiver_balance"] == D("11500.00") and row["difference"] == D("0")
    assert "equity method 30%" in row["consolidation"]["label_en"].lower()
    assert row["unmatched"] == []


def test_t21_intercompany_balances_net_to_zero(issued_invoice, lfn_acc, mp, alm, lfn):
    inv = issued_invoice()
    bill = inv.mirror_bill
    services.submit_bill(lfn_acc, bill)
    approve(bill, mp)
    bill.refresh_from_db()
    assert bill.status == "posted"
    assert lines_of(bill.journal_entry) == [("1310", D("1500.00"), D("0"), "ALM"), ("2110", D("0"), D("11500.00"), "ALM"),
                                            ("5300", D("10000.00"), D("0"), "ALM")]
    bal = ledger.intercompany_balances(NOV30)
    assert bal[("ALM", "LFN")] == D("11500.00") and bal[("LFN", "ALM")] == D("-11500.00")
    assert bal[("ALM", "LFN")] + bal[("LFN", "ALM")] == 0


# ---------------------------------------------------------------- T22: bilingual


def test_t22_missing_arabic_description_blocks_approval(make_agreement, accountant, alm, nov):
    a = make_agreement()
    services.run_billing(accountant, alm, nov)
    inv = a.invoices.get()
    line = inv.lines.get()
    with pytest.raises(RuleError, match="Arabic"):
        services.update_invoice_draft(accountant, inv, lines={line.pk: {"description_ar": " "}})
    line.description_ar = ""  # bypassing the service: the approval check still refuses
    line.save()
    with pytest.raises(RuleError, match="Arabic"):
        approvals.submit(inv, accountant)
    with pytest.raises(RuleError, match="Arabic"):
        services.validate_for_issue(inv)
    assert not ApprovalRequest.objects.filter(object_id=str(inv.pk), document_type__code="intercompany.invoice").exists()


# ---------------------------------------------------------------- T23: approval matrix ready


def test_t23_two_level_policy_keeps_invoice_pending_after_one_approval(make_agreement, accountant, mp, chairman_alm,
                                                                         alm, nov):
    from access.models import Role
    from approvals.models import ApprovableType

    t = ApprovableType.objects.get(code="intercompany.invoice")
    p = ApprovalPolicy.objects.create(entity=alm, document_type=t, currency="SAR", priority=0,
                                      name_en="Two levels", name_ar="مستويان", valid_from=datetime.date(2026, 1, 1))
    ApprovalStep.objects.create(policy=p, sequence=1, approver_role=Role.objects.get(code="managing_partner"))
    ApprovalStep.objects.create(policy=p, sequence=2, approver_role=Role.objects.get(code="chairman"))
    a = make_agreement()
    services.run_billing(accountant, alm, nov)
    inv = a.invoices.get()
    r = approvals.submit(inv, accountant)
    r = approve(inv, mp)
    inv.refresh_from_db()
    assert r.status == "pending" and inv.status == "pending_approval" and inv.number == ""
    r = approve(inv, chairman_alm)
    inv.refresh_from_db()
    assert r.status == "approved" and inv.status == "issued"


@pytest.fixture
def chairman_alm(make_user, alm):
    return make_user("chair", (alm, "chairman"))
