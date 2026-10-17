"""Review round 1 (17 Oct 2026): decisions D13-D16, code-review findings 1-10 and compliance findings."""
import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from access import services as access
from access.errors import AccessDenied, BilingualError, RuleError
from access.models import AuditLog, Permission, Role
from approvals import services as approvals
from approvals.models import ApprovalAction, ApprovalRequest, Notification
from approvals.services import PostingFailed
from core.models import Entity
from intercompany import services, zatca
from intercompany.models import DraftEditor, IntercompanyInvoice, InvoiceLine, ServiceType, VatRate
from ledger import services as ledger
from ledger.models import Account, FiscalPeriod

from .conftest import ADDRESS, TODAY, approve

pytestmark = pytest.mark.django_db
D = Decimal
INV = IntercompanyInvoice
BASE_API = "/api/v1/intercompany"


def period(entity, name):
    return FiscalPeriod.objects.get(entity=entity, name=name)


def post_cost(entity, day, account, amount):
    return ledger.create_entry(entity, day, "Cost", [{"account": account, "debit": amount},
                                                     {"account": "1100", "credit": amount}], system=True, post=True)


def issue(inv, accountant, mp):
    approvals.submit(inv, accountant)
    approve(inv, mp)
    inv.refresh_from_db()
    return inv


@pytest.fixture
def pool(accountant, alm):
    return services.create_cost_pool(accountant, alm, code="FIN", name_en="Finance", name_ar="المالية",
                                     accounts=[Account.objects.get(entity=alm, code="5100")])


def feeder_role():
    role, _ = Role.objects.get_or_create(code="ic_feeder", defaults={"name_en": "Feeder", "name_ar": "مُدخل"})
    role.permissions.add(Permission.objects.get(code="intercompany.billable_item.feed"),
                         Permission.objects.get(code="core.entity.view"))
    return role


# ---------------------------------------------------------------- D13 dating, tax point, deadlines


def test_d13_vat_rate_at_tax_point_refuses_issue_when_rate_changed(make_agreement, accountant, mp, alm, nov):
    a = make_agreement()
    services.run_billing(accountant, alm, nov)
    inv = a.invoices.get()
    approvals.submit(inv, accountant)
    VatRate.objects.create(country="SA", rate=D("20"), valid_from=TODAY)  # rate changes at the tax point
    with pytest.raises(PostingFailed, match="VAT rate"):
        approve(inv, mp)
    inv.refresh_from_db()
    assert inv.status == "pending_approval" and inv.number == ""


def test_d13_fifteen_day_alert_once_per_unissued_invoice(make_agreement, accountant, mp, alm, nov):
    a = make_agreement()
    services.run_billing(accountant, alm, nov)
    inv = a.invoices.get()
    assert services.issue_deadline(inv) == datetime.date(2026, 12, 15)
    assert services.deadline_alerts(on=datetime.date(2026, 12, 15)) == []
    approvals.submit(inv, accountant)  # pending invoices are alerted too, without breaking the approval
    assert services.deadline_alerts(on=datetime.date(2026, 12, 16)) == [inv]
    recipients = set(Notification.objects.filter(alert_code="ic_issue_deadline").values_list("user__username", flat=True))
    assert recipients == {"mp", "acc"}
    assert services.deadline_alerts(on=datetime.date(2026, 12, 17)) == []  # once only
    approve(inv, mp)  # the alert did not change the document version
    inv.refresh_from_db()
    assert inv.status == "issued"


def test_d13_issued_invoices_are_not_alerted_and_command_runs(issued_invoice, monkeypatch):
    from io import StringIO

    from django.core.management import call_command

    issued_invoice()
    monkeypatch.setattr(services, "today", lambda: datetime.date(2027, 1, 20))
    out = StringIO()
    call_command("intercompany_deadline_alerts", stdout=out)
    assert "0 invoice(s) alerted" in out.getvalue()


def test_d13_late_credit_note_warns(issued_invoice, accountant, monkeypatch):
    inv = issued_invoice()
    on_time = services.create_credit_note(accountant, inv, reason_en="Fix", reason_ar="تصحيح",
                                          trigger_date=datetime.date(2026, 11, 20),
                                          lines=[{"line": inv.lines.get(), "net_amount": "100"}])
    assert not on_time.late_credit_warning and on_time.trigger_date == datetime.date(2026, 11, 20)
    monkeypatch.setattr(services, "today", lambda: datetime.date(2026, 12, 20))
    late = services.create_credit_note(accountant, inv, reason_en="Fix", reason_ar="تصحيح",
                                       trigger_date=datetime.date(2026, 11, 20),
                                       lines=[{"line": inv.lines.get(), "net_amount": "100"}])
    assert late.late_credit_warning
    assert AuditLog.objects.filter(action="intercompany.credit_note.late_warning", object_id=str(late.pk)).exists()
    assert set(Notification.objects.filter(alert_code="ic_late_credit_note").values_list("user__username", flat=True)) \
        == {"acc", "mp"}
    assert services.credit_note_late(datetime.date(2026, 12, 3), datetime.date(2027, 1, 15)) is False
    assert services.credit_note_late(datetime.date(2026, 12, 3), datetime.date(2027, 1, 16)) is True


# ---------------------------------------------------------------- D14 pro-rating


def test_d14_cost_plus_share_pro_rated_by_active_days(make_agreement, accountant, alm, nov, pool):
    post_cost(alm, datetime.date(2026, 11, 3), "5100", "30000")
    a = make_agreement("cost_plus", cost_pool=pool, allocation_basis="fixed_pct", allocation_pct=D("50"),
                       markup_pct=D("0"), start_date=datetime.date(2026, 11, 16))
    services.run_billing(accountant, alm, nov)
    line = a.invoices.get().lines.get()
    assert line.pool_share == D("0.25") and line.net_amount == D("7500.00")  # 30,000 x 50% x 15/30
    assert "15/30 days" in line.pricing_basis


# ---------------------------------------------------------------- D15 agents and integrations


def test_d15_integration_drafts_with_own_source_and_cannot_submit(make_agreement, make_user, alm, nov):
    a = make_agreement()
    sync = make_user("rental-sync", (alm, "agent_drafter"), user_type="service_integration")
    inv = services.create_invoice_draft(sync, a, nov)
    assert (inv.source, inv.source_agent, inv.source_label) == ("integration", "rental-sync", "integration:rental-sync")
    with pytest.raises(AccessDenied, match="D15|D20"):  # engine-level D20 check fires first
        approvals.submit(inv, sync)
    assert services.source_fields(None) == {"source": "import", "source_agent": ""}
    assert services.source_fields(access.system_user())["source"] == "import"


def test_d15_agent_cannot_submit_agreement(make_agreement, make_user, alm):
    bot = make_user("ic-bot", (alm, "agent_drafter"), user_type="service_agent", agent_name="ic-bot")
    a = make_agreement(user=bot, activate=False)
    with pytest.raises(AccessDenied, match="D15|D20"):  # engine-level D20 check fires first
        approvals.submit(a, bot)
    a.refresh_from_db()
    assert a.status == "draft" and not ApprovalRequest.objects.exists()


# ---------------------------------------------------------------- review 1: cost pools never above 100%


def test_review1_one_basis_and_one_agreement_per_counterparty_per_pool(make_agreement, accountant, lfn_off, pool):
    make_agreement("cost_plus", cost_pool=pool, allocation_basis="fixed_pct", allocation_pct=D("30"), markup_pct=D("5"))
    twice = make_agreement("cost_plus", cost_pool=pool, allocation_basis="fixed_pct", allocation_pct=D("10"),
                           markup_pct=D("5"), activate=False)
    with pytest.raises(RuleError, match="already has an agreement"):
        approvals.submit(twice, accountant)
    mixed = make_agreement("cost_plus", cost_pool=pool, allocation_basis="headcount", markup_pct=D("5"),
                           counterparty=lfn_off, activate=False)
    with pytest.raises(RuleError, match="another allocation basis"):
        approvals.submit(mixed, accountant)


def test_review1_run_time_check_stops_billing_above_100(make_agreement, accountant, mp, alm, lfn_off, nov, pool):
    post_cost(alm, datetime.date(2026, 11, 3), "5100", "1000")
    first = make_agreement("cost_plus", cost_pool=pool, allocation_basis="fixed_pct", allocation_pct=D("70"),
                           markup_pct=D("0"))
    services.run_billing(accountant, alm, nov)
    services.end_agreement(accountant, first, datetime.date(2026, 11, 30), "Replaced")  # no longer live
    second = make_agreement("cost_plus", cost_pool=pool, allocation_basis="fixed_pct", allocation_pct=D("40"),
                            markup_pct=D("0"), counterparty=lfn_off)
    run = services.run_billing(accountant, alm, nov)
    row = {r["agreement"]: r for r in run.log}[second.number]
    assert row["code"] == "pool_over_allocated" and not second.invoices.exists()


# ---------------------------------------------------------------- review 2 and 6: end dates


def test_review2_future_end_date_keeps_billing_until_then(make_agreement, accountant, alm, monkeypatch):
    a = make_agreement()
    services.end_agreement(accountant, a, datetime.date(2026, 12, 20), "Moving in-house")
    a.refresh_from_db()
    assert a.status == "active" and a.end_date == datetime.date(2026, 12, 20)
    monkeypatch.setattr(services, "today", lambda: datetime.date(2027, 1, 5))
    services.run_billing(accountant, alm, period(alm, "2026-12"))
    a.refresh_from_db()
    assert a.status == "ended"  # the date has passed
    dec = a.invoices.get(billing_period="2026-12")
    assert dec.net_total == D("6451.61") and dec.supply_date_to == datetime.date(2026, 12, 20)  # 20/31 days
    assert services.run_billing(accountant, alm, period(alm, "2026-11")).invoices_created == 1  # still billable
    monkeypatch.setattr(services, "today", lambda: datetime.date(2027, 2, 5))
    assert services.run_billing(accountant, alm, period(alm, "2027-01")).invoices_created == 0


def test_review6_end_date_compared_with_supply_end(make_agreement, accountant, alm, nov):
    a = make_agreement()
    services.run_billing(accountant, alm, nov)
    with pytest.raises(RuleError, match="Invoices exist after"):
        services.end_agreement(accountant, a, datetime.date(2026, 11, 15), "Mid-month")


# ---------------------------------------------------------------- review 3: feeders


def test_review3_feed_needs_permission_and_feeder_cannot_approve(make_agreement, accountant, mp, make_user, alm, nov,
                                                               api):
    a = make_agreement()
    kw = dict(period_start=datetime.date(2026, 11, 1), period_end=datetime.date(2026, 11, 30),
              description_en="Fuel", description_ar="وقود", quantity="1", unit_price="50")
    with pytest.raises(AccessDenied):
        services.add_billable_item(accountant, a, **kw)
    r = api(accountant).post(f"{BASE_API}/billable-items", {"agreement": a.pk, **{k: str(v) for k, v in kw.items()}},
                             format="json")
    assert r.status_code == 404  # no feed right: the agreement is not offered
    access.grant_access(None, mp, alm, feeder_role(), valid_from=datetime.date(2026, 1, 1))
    services.add_billable_item(mp, a, **kw)
    services.run_billing(accountant, alm, nov)
    inv = a.invoices.get()
    assert DraftEditor.objects.filter(object_id=str(inv.pk), user=mp).exists()
    r = approvals.submit(inv, accountant)
    assert "mp" in set(r.document_editors.values_list("username", flat=True))
    approve(inv, mp)  # D10: the MP is the only approver, so this is a logged self-approval override
    assert ApprovalAction.objects.get(request=r, action="approve").self_approval_override
    assert AuditLog.objects.filter(action="approval.self_approval_override").exists()


def test_review3_other_approver_blocked_when_feeder(make_agreement, accountant, mp, make_user, alm, nov):
    other_mp = make_user("mp2", (alm, "managing_partner"))
    access.grant_access(None, other_mp, alm, feeder_role(), valid_from=datetime.date(2026, 1, 1))
    a = make_agreement()
    services.add_billable_item(other_mp, a, period_start=datetime.date(2026, 11, 1),
                               period_end=datetime.date(2026, 11, 30), description_en="Fuel", description_ar="وقود",
                               quantity="1", unit_price="50")
    services.run_billing(accountant, alm, nov)
    inv = a.invoices.get()
    approvals.submit(inv, accountant)
    with pytest.raises(AccessDenied):
        approve(inv, other_mp)
    approve(inv, mp)


# ---------------------------------------------------------------- review 4: credit VAT cap


def test_review4_credit_vat_capped_at_line_remaining_vat(make_agreement, accountant, mp, alm, nov):
    a = make_agreement()
    services.add_billable_item(None, a, period_start=datetime.date(2026, 11, 1), period_end=datetime.date(2026, 11, 30),
                               description_en="Small item", description_ar="بند صغير", quantity="1", unit_price="1.03")
    services.run_billing(accountant, alm, nov)
    inv = issue(a.invoices.get(), accountant, mp)
    small = inv.lines.get(net_amount=D("1.03"))
    assert small.vat_amount == D("0.15")
    kw = dict(reason_en="Fix", reason_ar="تصحيح", trigger_date=TODAY)
    services.create_credit_note(accountant, inv, lines=[{"line": small, "net_amount": "0.97"}], **kw)  # VAT 0.15
    with pytest.raises(RuleError, match="uncredited VAT") as exc:
        services.create_credit_note(accountant, inv, lines=[{"line": small, "net_amount": "0.05"}], **kw)
    assert exc.value.code == "credit_vat_exceeds_line"


# ---------------------------------------------------------------- review 5: codes frozen at approval


def test_review5_account_codes_copied_at_approval(make_agreement, accountant, mp, alm, nov):
    a = make_agreement()
    assert (a.income_account_code, a.expense_account_code) == ("4210", "5300")
    st = ServiceType.objects.get(code="FIN")
    st.income_account_code, st.expense_account_code = "4200", "5900"
    st.save()
    services.run_billing(accountant, alm, nov)
    inv = issue(a.invoices.get(), accountant, mp)
    assert inv.lines.get().income_account.code == "4210"
    assert inv.mirror_bill.lines.get().expense_account.code == "5300"


# ---------------------------------------------------------------- review 7 and 8: API input and scoping


def test_review7_malformed_patch_bodies_are_400_or_404(api, make_agreement, issued_invoice, accountant, lfn_acc, alm,
                                                       nov):
    inv = issued_invoice()
    a = make_agreement()
    services.run_billing(accountant, alm, nov)
    draft = a.invoices.get()
    acc, rec = api(accountant), api(lfn_acc)
    assert acc.patch(f"{BASE_API}/invoices/{draft.pk}", {"lines": [{"description_en": "x"}]}, format="json").status_code == 400
    assert acc.patch(f"{BASE_API}/invoices/{draft.pk}", {"lines": [{"id": "abc"}]}, format="json").status_code == 400
    assert acc.patch(f"{BASE_API}/invoices/{draft.pk}", {"lines": "nope"}, format="json").status_code == 400
    assert acc.patch(f"{BASE_API}/invoices/{draft.pk}", {"lines": [{"id": 999999}]}, format="json").status_code == 404
    assert acc.patch(f"{BASE_API}/invoices/{draft.pk}", ["x"], format="json").status_code == 400
    bill = inv.mirror_bill
    assert rec.patch(f"{BASE_API}/bills/{bill.pk}", {}, format="json").status_code == 400
    assert rec.patch(f"{BASE_API}/bills/{bill.pk}", {"lines": [{"id": "x"}]}, format="json").status_code == 400
    assert rec.patch(f"{BASE_API}/bills/{bill.pk}", {"lines": [{"id": 999999, "cost_centre": "A"}]},
                     format="json").status_code == 404
    assert acc.post(f"{BASE_API}/invoices/{inv.pk}/credit-notes", {"reason_en": "x", "reason_ar": "س"},
                    format="json").status_code == 400  # trigger_date missing
    assert acc.post(f"{BASE_API}/agreements", {"entity": "ALM", "bogus": 1}, format="json").status_code == 400


def test_review8_scoped_lookups(api, make_agreement, accountant, make_user, alm, lfn, fin):
    analyst = make_user("ana", (alm, "business_analyst"))
    assert api(analyst).get(f"{BASE_API}/service-types").status_code == 403
    a = make_agreement("timesheet")
    other = services.create_agreement(accountant, entity=alm, counterparty=lfn, service_type=fin,
                                      pricing_method="timesheet", hourly_rate=D("100"), description_en="x",
                                      description_ar="س", vat_treatment="standard", billing_frequency="monthly",
                                      payment_terms_days=30, start_date=datetime.date(2026, 1, 1))
    foreign_rate = services.add_rate(accountant, other, role_en="Senior", role_ar="أول", hourly_rate=D("1"),
                                     valid_from=datetime.date(2026, 1, 1))
    outsider = make_user("outsider", (lfn, "accountant"))
    c = api(accountant)
    body = {"agreement": a.pk, "date": "2026-11-02", "hours": "1", "description_en": "x", "description_ar": "س"}
    assert c.post(f"{BASE_API}/timesheets", {**body, "rate": foreign_rate.pk}, format="json").status_code == 404
    assert c.post(f"{BASE_API}/timesheets", {**body, "worker": outsider.username}, format="json").status_code == 404
    assert c.post(f"{BASE_API}/timesheets", body, format="json").status_code == 201


# ---------------------------------------------------------------- review 10: adjustments


def test_review10_late_approved_hours_billed_as_adjustment(make_agreement, accountant, mp, alm, nov, monkeypatch):
    a = make_agreement("timesheet")
    kw = dict(description_en="Work", description_ar="عمل")
    oct_entry = services.create_timesheet(accountant, a, date=datetime.date(2026, 10, 20), hours=D("2"), **kw)
    nov_entry = services.create_timesheet(accountant, a, date=datetime.date(2026, 11, 3), hours=D("1"), **kw)
    for e in (oct_entry, nov_entry):
        services.submit_timesheet(accountant, e)
        services.approve_timesheet(mp, e)  # October hours approved late, after October was billed
    inv = services.run_billing(accountant, alm, nov) and a.invoices.get(billing_period="2026-11")
    adj = inv.lines.get(adjustment_period="2026-10")
    assert adj.net_amount == D("900.00") and adj.pricing_basis.startswith("adjustment 2026-10")
    assert adj.description_en.startswith("Adjustment 2026-10") and adj.description_ar.startswith("تسوية 2026-10")
    assert inv.lines.get(adjustment_period="").net_amount == D("450.00")
    oct_entry.refresh_from_db()
    assert oct_entry.status == "billed" and oct_entry.invoice_line == adj


def test_review10_late_pool_costs_billed_once_as_adjustment(make_agreement, accountant, mp, alm, nov, pool,
                                                            monkeypatch):
    post_cost(alm, datetime.date(2026, 11, 3), "5100", "80000")
    a = make_agreement("cost_plus", cost_pool=pool, allocation_basis="fixed_pct", allocation_pct=D("25"),
                       markup_pct=D("10"))
    services.run_billing(accountant, alm, nov)
    issue(a.invoices.get(), accountant, mp)
    post_cost(alm, datetime.date(2026, 11, 28), "5100", "4000")  # posted after November was billed
    post_cost(alm, datetime.date(2026, 12, 3), "5100", "10000")
    monkeypatch.setattr(services, "today", lambda: datetime.date(2027, 1, 5))
    services.run_billing(accountant, alm, period(alm, "2026-12"))
    dec = a.invoices.get(billing_period="2026-12")
    adj = dec.lines.get(adjustment_period="2026-11")
    assert adj.net_amount == D("1100.00") and adj.pool_amount == D("4000.00")  # 4,000 x 25% x 1.10
    assert dec.lines.get(adjustment_period="").net_amount == D("2750.00")
    issue(dec, accountant, mp)
    monkeypatch.setattr(services, "today", lambda: datetime.date(2027, 2, 5))
    run = services.run_billing(accountant, alm, period(alm, "2027-01"))
    assert not a.invoices.filter(billing_period="2027-01").exists()  # nothing new: no double adjustment
    assert run.log[0]["code"] == "pool_empty"


def test_review10_pool_decrease_is_noted_not_billed(make_agreement, accountant, mp, alm, nov, pool, monkeypatch):
    entry = post_cost(alm, datetime.date(2026, 11, 3), "5100", "1000")
    a = make_agreement("cost_plus", cost_pool=pool, allocation_basis="fixed_pct", allocation_pct=D("50"),
                       markup_pct=D("0"))
    services.run_billing(accountant, alm, nov)
    issue(a.invoices.get(), accountant, mp)
    ledger.reverse_entry(entry, user=accountant, reason="Wrong pool", date=datetime.date(2026, 11, 30))
    post_cost(alm, datetime.date(2026, 12, 3), "5100", "200")
    monkeypatch.setattr(services, "today", lambda: datetime.date(2027, 1, 5))
    run = services.run_billing(accountant, alm, period(alm, "2026-12"))
    assert "credit note" in run.log[0]["notes"][0]
    assert not a.invoices.get(billing_period="2026-12").lines.exclude(adjustment_period="").exists()


# ---------------------------------------------------------------- compliance: parties, VATEX, document VAT


def test_compliance_vat_number_format_on_entity(alm):
    e = Entity(code="BAD", name_en="Bad", name_ar="سيئ", parent=alm, vat_number="123456789012345")
    with pytest.raises(ValidationError, match="15 digits"):
        e.full_clean()
    e.vat_number = "300000000000003"
    e.full_clean()


def test_compliance_buyer_address_and_identifier_required(make_agreement, accountant, alm, lfn, nov):
    a = make_agreement()
    services.run_billing(accountant, alm, nov)
    inv = a.invoices.get()
    lfn.city = ""
    lfn.vat_number = ""
    lfn.save()
    with pytest.raises(RuleError) as exc:
        approvals.submit(inv, accountant)
    assert any("address is incomplete (city)" in p for p in exc.value.detail)
    assert any("VAT number or another identifier" in p for p in exc.value.detail)
    lfn.city, lfn.cr_number = "Buraydah", "1010000000"
    lfn.save()
    approvals.submit(inv, accountant)  # a CR number is an accepted buyer identifier


def test_compliance_seller_vat_number_must_be_valid(make_agreement, accountant, alm, nov):
    a = make_agreement()
    services.run_billing(accountant, alm, nov)
    Entity.objects.filter(pk=alm.pk).update(vat_number="300000000000001")  # wrong last digit (bypassing clean)
    with pytest.raises(RuleError, match="valid VAT number"):
        approvals.submit(a.invoices.get(), accountant)


def test_compliance_out_of_scope_needs_oos_code_and_zero_rate(make_agreement, accountant, mp, alm, nov):
    a = make_agreement(vat_treatment="out_of_scope", vat_exemption_reason_en="Outside VAT scope",
                       vat_exemption_reason_ar="خارج نطاق الضريبة", vat_exemption_code="VATEX-SA-OOS")
    services.run_billing(accountant, alm, nov)
    inv = a.invoices.get()
    line = inv.lines.get()
    assert (line.vat_category, line.vat_rate, line.vat_exemption_code) == ("O", D("0"), "VATEX-SA-OOS")
    line.vat_rate = D("15")
    line.save()
    with pytest.raises(RuleError, match="0% rate"):
        services.validate_for_issue(inv)


def test_compliance_document_vat_per_category_not_sum_of_lines(make_agreement, accountant, mp, alm, lfn_acc, nov):
    a = make_agreement(fixed_fee=D("0.10"))
    for _ in range(2):
        services.add_billable_item(None, a, period_start=datetime.date(2026, 11, 1),
                                   period_end=datetime.date(2026, 11, 30), description_en="Item",
                                   description_ar="بند", quantity="1", unit_price="0.10")
    services.run_billing(accountant, alm, nov)
    inv = a.invoices.get()
    assert [l.vat_amount for l in inv.lines.all()] == [D("0.02")] * 3  # 0.015 rounded per line
    assert inv.net_total == D("0.30") and inv.vat_total == D("0.05") and inv.gross_total == D("0.35")
    inv = issue(inv, accountant, mp)
    vat_line = inv.journal_entry.lines.get(account__code="2200")
    assert vat_line.credit == D("0.05")
    payload = zatca.handoff_payload(inv)
    assert payload["vat_breakdown"] == [{"category": "S", "rate": "15.00", "taxable_amount": "0.30",
                                         "vat_amount": "0.05", "exemption_code": ""}]
    assert payload["seller"]["address"]["city"] == "Jeddah" and payload["buyer"]["address"]["postal_code"] == "23456"
    bill = inv.mirror_bill
    assert bill.vat_total == D("0.05")
    services.submit_bill(lfn_acc, bill)
    approve(bill, mp)
    bill.refresh_from_db()
    assert bill.journal_entry.lines.get(account__code="1310").debit == D("0.05")
    full = services.create_credit_note(accountant, inv, reason_en="Full", reason_ar="كامل", trigger_date=TODAY)
    assert full.vat_total == D("0.05") and full.gross_total == D("0.35")  # the exact uncredited balance


# ---------------------------------------------------------------- compliance: transfer pricing


def test_compliance_tp_required_outside_zakat_group(accountant, alm, fin, make_user):
    partly = Entity.objects.create(code="PRT", name_en="Partly owned", name_ar="مملوكة جزئياً", parent=alm,
                                   ownership_pct=D("60"), vat_number="300000000000043", **ADDRESS)
    kw = dict(entity=alm, counterparty=partly, service_type=fin, pricing_method="fixed_fee", fixed_fee=D("1"),
              description_en="x", description_ar="س", vat_treatment="standard", billing_frequency="monthly",
              payment_terms_days=30, start_date=datetime.date(2026, 1, 1))
    with pytest.raises(RuleError) as exc:
        services.create_agreement(accountant, **kw)
    assert set(exc.value.detail) >= {"tp_method", "tp_documentation_ref"}
    a = services.create_agreement(accountant, tp_method="cost_plus", tp_documentation_ref="TP-2026-07", **kw)
    assert a.tp_method == "cost_plus"


def test_compliance_related_party_report(issued_invoice, accountant, mp, api, make_user, alm):
    inv = issued_invoice()
    issue(services.create_credit_note(accountant, inv, reason_en="Part", reason_ar="جزء", trigger_date=TODAY,
                                      lines=[{"line": inv.lines.get(), "net_amount": "1000"}]), accountant, mp)
    rep = services.related_party_report(accountant, 2026)
    assert rep["rows"] == [{"issuer": "ALM", "counterparty": "LFN",
                            "consolidation": {"type": "full", "ownership_pct": "100.000"}, "service_type": "FIN",
                            "tp_method": None, "currency": "SAR", "net_amount": D("9000.00"), "documents": 2}]
    assert rep["headings"]["counterparty"]["ar"] == "الطرف ذو العلاقة"
    body = api(accountant).get(f"{BASE_API}/related-party?year=2026").json()
    assert body["rows"][0]["net_amount"] == "9000.00"
    assert services.related_party_report(accountant, 2025)["rows"] == []
    analyst = make_user("ana", (alm, "business_analyst"))
    with pytest.raises(AccessDenied):
        services.related_party_report(analyst, 2026)


def test_line_fields_exist_for_zatca():
    names = {f.name for f in InvoiceLine._meta.get_fields()}
    assert {"vat_exemption_code", "vat_category", "pool_share", "pool_amount", "adjustment_period"} <= names
    with pytest.raises(BilingualError):
        IntercompanyInvoice.objects.all().update(late_credit_warning=True)
