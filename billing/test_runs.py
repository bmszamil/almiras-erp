"""Billing runs (R9 to R18, R22, R23, R32 to R35, R37, R52 to R55, R66, R67; AT-2, AT-4 to AT-6, AT-9, AT-10,
AT-17 to AT-19, AT-35, AT-39 to AT-41, AT-45, AT-46, AT-52)."""
from datetime import date
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from billing import services
from billing.conftest import approved, clear, contract, period, run, set_auto_issue, submitted, version
from billing.models import BillingNotification, BillingPeriod, BillingRun
from core import access
from core.access.testing import make_user, member
from invoicing import services as inv
from invoicing.conftest import ADDRESS, make_entity
from invoicing.models import DocumentSequence, InvoicingError, SalesDocument
from ledger import services as ledger
from ledger.models import FiscalPeriod

D = Decimal


def _numbers_used(entity):
    return sum(DocumentSequence.objects.filter(entity=entity).values_list("last_number", flat=True))


def _close(entity, name):
    FiscalPeriod.objects.filter(entity=entity, name=name).update(status="closed")


def _open(entity, name):
    FiscalPeriod.objects.filter(entity=entity, name=name).update(status="open")


def _rate(w, currency, rate, on):
    treasurer = make_user(f"treasury.{currency}.{on}")
    member(treasurer, w.alm, "group_treasury")
    return ledger.create_rate(user=treasurer, from_currency=currency, to_currency="SAR", on=on, rate=D(rate),
                              group=w.alm)


# --- AT-2, AT-4, R11, R13 to R15, R35, R53 ------------------------------------------------------------------------

def test_at2_rerun_never_bills_twice(w):
    c = approved(w, start=date(2027, 3, 1))
    first = run(w, date(2027, 3, 1))
    assert first.generated_count == 1 and first.status == "completed"
    used = _numbers_used(w.alm)
    second = run(w, date(2027, 3, 1))
    third = run(w, date(2027, 3, 2))
    for r in (second, third):
        assert r.generated_count == 0
        assert list(r.items.values_list("outcome", flat=True)) == ["skipped_already_billed"]
    assert SalesDocument.objects.filter(entity=w.alm).count() == 1
    assert _numbers_used(w.alm) == used  # no number consumed by the re-runs
    assert w.fake.invoice_calls == [period(c, date(2027, 3, 1)).pk]  # invoicing was not even asked again


def test_at4_at39_invoice_from_contract_issued_without_per_invoice_approval(w):
    c = approved(w, start=date(2027, 3, 1))
    run(w, date(2027, 3, 1))
    p = period(c, date(2027, 3, 1))
    doc = SalesDocument.objects.get(pk=p.sales_document_id)
    assert (doc.status, doc.issue_date, doc.supply_date, doc.supply_end_date) == (
        "issued", date(2027, 3, 1), date(2027, 3, 1), date(2027, 3, 31))
    assert doc.approved_by == w.c  # the contract approver (R53), no per-invoice approval (D8)
    assert doc.retention_class == "capital_asset_real_estate"  # R28: rent on real estate
    assert p.billed_version == c.current_version and p.invoice_lines[0]["net"] == "10000.00"
    line = doc.lines.get()
    assert (line.vat_category, line.net_amount, line.income_account.code) == ("S", D("10000.00"), "4100")
    assert "2027-03-01 to 2027-03-31" in line.description_en and "من 2027-03-01" in line.description_ar
    final = clear(w, doc)
    entry = final.journal_entry
    assert entry.posted_by == w.c
    assert sorted((ln.account.code, ln.debit, ln.credit) for ln in entry.lines.all()) == [  # B1
        ("1200", D("11500.00"), D("0.00")), ("2200", D("0.00"), D("1500.00")), ("4100", D("0.00"), D("10000.00"))]
    assert run(w, date(2027, 3, 1)).generated_count == 0


def test_r52_due_days_on_the_invoice(w):
    c = approved(w, start=date(2027, 3, 1), due_days=10)
    run(w, date(2027, 3, 1))
    assert period(c, date(2027, 3, 1)).sales_document.due_date == date(2027, 3, 11)


def test_at39_switch_off_leaves_invoice_submitted_for_the_cfo(w):
    set_auto_issue(w.alm, False)
    c = approved(w, start=date(2027, 3, 1))
    run(w, date(2027, 3, 1))
    p = period(c, date(2027, 3, 1))
    doc = SalesDocument.objects.get(pk=p.sales_document_id)
    assert p.status == "generated" and doc.status == "submitted" and doc.number == ""
    issued = inv.approve_sales_document(doc, user=w.c)
    assert clear(w, issued).journal_entry.posted_by == w.c
    assert run(w, date(2027, 3, 1)).generated_count == 0


def test_at35_billing_system_user_cannot_submit_or_approve(w):
    c = approved(w)
    run(w, date(2027, 1, 1))
    doc = period(c, date(2027, 1, 1)).sales_document
    with pytest.raises(access.AccessDenied):
        inv.approve_sales_document(doc, user=w.sys)
    draft = inv.create_sales_document(w.alm, user=w.s, customer=w.customer, issue_date=date(2027, 1, 1))
    with pytest.raises(access.AccessDenied):
        inv.submit_sales_document(draft, user=w.sys)


# --- AT-5, AT-6, R9, R16, R17, R22 ----------------------------------------------------------------------------

def test_at5_first_partial_period_prorated(w):
    c = approved(w, start=date(2027, 1, 15))
    run(w, date(2027, 1, 15))
    jan = period(c, date(2027, 1, 15))
    assert (jan.proration_factor, jan.full_period_days) == (D("0.548387"), 31)
    assert jan.sales_document.total_net == D("5483.87")
    run(w, date(2027, 2, 1))
    assert period(c, date(2027, 2, 1)).sales_document.total_net == D("10000.00")


def test_at6_last_partial_period_then_ended_and_nothing_more(w):
    c = approved(w, end=date(2027, 6, 20), billing_timing="arrears", lines=(("rent_commercial", "9000.00"),))
    for month in range(2, 8):
        run(w, date(2027, month, 1))
    june = c.periods.get(period_start=date(2027, 6, 1))
    assert (june.period_end, june.bill_date) == (date(2027, 6, 20), date(2027, 6, 21))
    assert june.sales_document.total_net == D("6000.00")  # 9,000.00 x 20/30
    c.refresh_from_db()
    assert c.status == "ended"
    later = run(w, date(2027, 8, 1))
    assert later.generated_count == 0
    assert SalesDocument.objects.filter(entity=w.alm).count() == 6


def test_at9_termination_prorates_and_cancels(w):
    c = approved(w, contract_type="vehicle_rental", lease_use="", vehicle_ref="TEST-PLATE-2",
                 billing_timing="arrears", lines=(("car_rental", "3100.00"),))
    v = version(w, c, kind="termination", effective=date(2027, 5, 16), reason_ar="إنهاء مبكر بطلب العميل")
    assert period(c, date(2027, 5, 1)).period_end == date(2027, 5, 31)  # before approval: unchanged
    services.approve_version(v, user=w.c)
    c.refresh_from_db()
    assert (c.status, c.termination_kind, c.terminated_on) == ("terminated", "early_termination", date(2027, 5, 15))
    may = period(c, date(2027, 5, 1))
    assert (may.period_end, may.status) == (date(2027, 5, 15), "planned")
    assert set(c.periods.filter(period_start__gte=date(2027, 6, 1)).values_list("status", flat=True)) == {"cancelled"}
    run(w, date(2027, 5, 16))
    assert period(c, date(2027, 5, 1)).sales_document.total_net == D("1500.00")  # 3,100.00 x 15/31
    assert run(w, date(2027, 7, 1)).generated_count == 0  # R22


# --- AT-10, AT-40, R23, R54 -----------------------------------------------------------------------------------

def test_at10_closed_run_date_fails_and_retries_catch_up_in_closed_period_held(w):
    c = approved(w, start=date(2027, 3, 1))
    _close(w.alm, "2027-03")
    used = _numbers_used(w.alm)
    r = run(w, date(2027, 3, 2))
    item = r.items.get()
    assert (item.outcome, item.error_code) == ("failed", "period_closed")
    assert "closed" in item.message_en and "مقفلة" in item.message_ar
    p = period(c, date(2027, 3, 1))
    assert p.status == "failed" and p.sales_document is None and _numbers_used(w.alm) == used
    assert r.status == "completed_with_errors"
    _open(w.alm, "2027-03")
    assert run(w, date(2027, 3, 3)).generated_count == 1  # retried automatically

    w.clock.set(date(2027, 4, 1))
    late = approved(w, start=date(2027, 2, 1))
    _close(w.alm, "2027-02")
    r = run(w, date(2027, 4, 1))
    feb = period(late, date(2027, 2, 1))
    assert feb.status == "held" and feb.error_code == "supply_period_closed" and feb.sales_document is None
    assert "مقفلة" in feb.hold_reason_ar
    assert period(late, date(2027, 3, 1)).status == "generated" and period(late, date(2027, 4, 1)).status == "generated"
    run(w, date(2027, 4, 2))
    assert period(late, date(2027, 2, 1)).status == "held"  # not retried without a person
    services.release_period(feb, user=w.s)
    assert period(late, date(2027, 2, 1)).status == "planned"


def test_at40_rate_required_fails_and_retries_skipped_entity(w):
    c = approved(w, start=date(2027, 3, 1))
    w.fake.fail("rate_required")
    r = run(w, date(2027, 3, 1))
    p = period(c, date(2027, 3, 1))
    assert (p.status, p.error_code, p.sales_document) == ("failed", "rate_required", None)
    assert "exchange rate" in r.items.get().message_en and "سعر صرف" in r.items.get().message_ar
    assert run(w, date(2027, 3, 2)).generated_count == 1
    oth = make_entity("OTH", "full", w.alm, vat="314444444400003", cr="1010000004")
    cfo, staff = make_user("cfo_oth"), make_user("staff_oth")
    member(cfo, oth, "approver")
    member(staff, oth, "accountant")
    cust = inv.create_customer(oth, user=staff, code="O1", name_ar="عميل", is_vat_registered=True,
                               vat_number="315555555500003", **ADDRESS)
    other = services.approve_contract(submitted(contract(w, entity=oth, customer=cust, user=staff)), user=cfo)
    w.clock.set(date(2027, 4, 1))
    runs = services.run_billing(date(2027, 4, 1), user=w.sys)  # every entity
    by_entity = {r.entity.code: r for r in runs}
    assert by_entity["OTH"].status == "skipped" and "OTH" in by_entity["OTH"].messages[0]["en"]
    assert "عضوية" in by_entity["OTH"].messages[0]["ar"]
    assert by_entity["ALM"].generated_count == 1
    assert not other.periods.exclude(status="planned").exists()


def test_at52_entity_without_vat_number_holds(w):
    novat = make_entity("NOV", "full", w.alm, cr="1010000005")
    cfo, staff = make_user("cfo_nov"), make_user("staff_nov")
    member(cfo, novat, "approver")
    member(staff, novat, "accountant")
    member(w.sys, novat, "system_billing")
    cust = inv.create_customer(novat, user=staff, code="N1", name_ar="عميل", is_vat_registered=True,
                               vat_number="316666666600003", **ADDRESS)
    c = services.approve_contract(submitted(contract(w, entity=novat, customer=cust, user=staff)), user=cfo)
    run(w, date(2027, 1, 1), entity=novat)
    p = period(c, date(2027, 1, 1))
    assert (p.status, p.error_code) == ("held", "entity_not_vat_registered")
    assert "R94" in p.hold_reason_en and "رقم ضريبي" in p.hold_reason_ar
    assert not SalesDocument.objects.filter(entity=novat).exists()


# --- AT-17, AT-41, R12, R33, R34, R55 -------------------------------------------------------------------------

def test_at17_one_failure_never_stops_the_others_rejection_needs_release(w):
    contracts = [approved(w, start=date(2027, 3, 1)) for _ in range(3)]
    first, second, third = (period(c, date(2027, 3, 1)) for c in contracts)
    w.fake.fail_periods[second.pk] = "document_invalid"
    r = run(w, date(2027, 3, 1))
    assert r.status == "completed_with_errors" and (r.generated_count, r.failed_count) == (2, 1)
    second.refresh_from_db()
    assert second.status == "failed" and "Fake invoicing refused" in second.failure_reason_en
    third.refresh_from_db()
    rejected = SalesDocument.objects.get(pk=third.sales_document_id)
    w.zatca.reject(rejected)
    del w.fake.fail_periods[second.pk]
    nxt = run(w, date(2027, 3, 2))
    assert nxt.sync_count == 1
    third.refresh_from_db()
    assert third.status == "failed" and third.needs_release and third.sales_document is None
    assert list(third.superseded_documents.all()) == [rejected]
    second.refresh_from_db()
    assert second.status == "generated"  # retried automatically
    assert nxt.generated_count == 1  # the rejected one waits for a person
    services.release_period(third, user=w.s)
    run(w, date(2027, 3, 3))
    third.refresh_from_db()
    assert third.status == "generated" and third.sales_document.number != rejected.number
    assert SalesDocument.objects.get(pk=rejected.pk).status == "rejected"  # kept, with its number (AT-41)
    staff = set(BillingNotification.objects.filter(run=r).values_list("recipient__username", flat=True))
    assert staff == {"s", "s2", "c", "c_b"}  # ALM accounting staff and CFOs only, never SUB's
    assert BillingNotification.objects.filter(run=r, message_ar__contains="تعثرت").exists()


def test_r34_cfo_alerted_after_three_failed_runs(w):
    c = approved(w, start=date(2027, 3, 1))
    w.fake.fail_periods[period(c, date(2027, 3, 1)).pk] = "document_invalid"
    for day in (1, 2, 3):
        r = run(w, date(2027, 3, day))
    alerts = BillingNotification.objects.filter(run=r, kind="failure_escalation")
    assert set(alerts.values_list("recipient__username", flat=True)) == {"c", "c_b"}
    assert not BillingNotification.objects.filter(kind="failure_escalation").exclude(run=r).exists()


# --- AT-18, AT-19, R18, R32 ----------------------------------------------------------------------------------

def test_at18_price_steps(w):
    a = contract(w)
    services.add_price_step(a.lines.get(), user=w.s, effective_from=date(2027, 7, 1), new_amount="11000.00")
    a = services.approve_contract(submitted(a), user=w.c)
    b = contract(w)
    services.add_price_step(b.lines.get(), user=w.s, effective_from=date(2027, 7, 15), new_amount="11000.00")
    b = services.approve_contract(submitted(b), user=w.c)
    run(w, date(2027, 7, 1))
    run(w, date(2027, 8, 1))
    assert period(a, date(2027, 7, 1)).sales_document.total_net == D("11000.00")
    assert period(b, date(2027, 7, 1)).sales_document.total_net == D("10000.00")
    assert period(b, date(2027, 8, 1)).sales_document.total_net == D("11000.00")


def test_at19_foreign_currency_rate_on_issue_date_or_failed(w):
    _rate(w, "USD", "3.75", date(2027, 3, 1))
    usd = approved(w, start=date(2027, 3, 1), currency="USD", lines=(("rent_commercial", "1000.00"),))
    eur = approved(w, start=date(2027, 3, 1), currency="EUR", lines=(("rent_commercial", "1000.00"),))
    run(w, date(2027, 3, 1))
    doc = period(usd, date(2027, 3, 1)).sales_document
    assert (doc.currency_id, doc.total_net, doc.total_vat) == ("USD", D("1000.00"), D("150.00"))
    final = clear(w, doc)
    assert sorted((ln.account.code, ln.debit, ln.credit) for ln in final.journal_entry.lines.all()) == [  # B5
        ("1200", D("4312.50"), D("0.00")), ("2200", D("0.00"), D("562.50")), ("4100", D("0.00"), D("3750.00"))]
    p = period(eur, date(2027, 3, 1))
    assert (p.status, p.error_code, p.sales_document) == ("failed", "rate_required", None)
    assert not SalesDocument.objects.filter(currency_id="EUR").exists()


# --- AT-45, R37: 3-decimal currencies ------------------------------------------------------------------------

def test_at45_kwd_third_decimal_held_round_amounts_issued(w):
    _rate(w, "KWD", "12.2", date(2027, 2, 1))
    w.clock.set(date(2027, 1, 10))
    c = contract(w, start=date(2027, 1, 15), currency="KWD", lines=(("rent_commercial", "1000.000"),))
    c = services.approve_contract(submitted(c), user=w.c)
    assert any("third decimal" in m and "الثالثة" in m for m in c.warnings)
    used = _numbers_used(w.alm)
    run(w, date(2027, 1, 15))
    jan = period(c, date(2027, 1, 15))
    assert (jan.status, jan.error_code) == ("held", "third_decimal_pending")
    assert "OQ-62" in jan.hold_reason_en and _numbers_used(w.alm) == used
    run(w, date(2027, 2, 1))
    feb = period(c, date(2027, 2, 1)).sales_document
    assert (feb.currency_id, feb.total_net, feb.total_vat) == ("KWD", D("1000.000"), D("150.000"))
    services.release_period(jan, user=w.s)
    run(w, date(2027, 2, 2))
    assert period(c, date(2027, 1, 15)).status == "held"  # held again, no number used
    assert _numbers_used(w.alm) == used + 1


# --- AT-46, R66, R67: command, manual runs ---------------------------------------------------------------------

def test_at46_command_twice_and_manual_run(w):
    alm_c = approved(w, start=date(2027, 3, 1))
    sub_c = approved(w, entity=w.sub, customer=w.sub_customer, start=date(2027, 3, 1))
    w.clock.set(date(2027, 3, 1))
    out = StringIO()
    call_command("billing_run", "--date", "2027-03-01", stdout=out)
    assert "ALM 2027-03-01 completed: generated 1" in out.getvalue()
    call_command("billing_run", "--date", "2027-03-01", stdout=StringIO())
    second = BillingRun.objects.filter(entity=w.alm).order_by("-pk").first()
    assert second.generated_count == 0 and set(second.items.values_list("outcome", flat=True)) == {
        "skipped_already_billed"}
    assert SalesDocument.objects.filter(entity=w.alm).count() == 1 and SalesDocument.objects.filter(
        entity=w.sub).count() == 1
    assert period(alm_c, date(2027, 3, 1)).status == period(sub_c, date(2027, 3, 1)).status == "generated"
    manual = services.start_manual_run(w.alm, user=w.s, run_date=date(2027, 3, 1))
    assert (manual.started_by, manual.initiated_by, manual.trigger) == (w.sys, w.s, "manual")
    with pytest.raises(access.AccessDenied):
        services.start_manual_run(w.alm, user=w.v)  # viewer
    with pytest.raises(access.AccessDenied):
        services.run_billing(date(2027, 3, 1), entity=w.alm, user=w.s)  # a person never runs as themselves
    with pytest.raises(CommandError):
        call_command("billing_run", "--entity", "NOPE", stdout=StringIO())


def test_r66_aborted_run_exits_non_zero(w, monkeypatch):
    """An error outside the per-period loop (here: activating contracts) aborts the run; inside it, see
    test_regressions.test_p3_*."""
    approved(w, start=date(2027, 3, 1))

    def boom(*args, **kwargs):
        raise RuntimeError("unexpected failure (test)")

    monkeypatch.setattr(services, "_prepare_contracts", boom)
    w.clock.set(date(2027, 3, 1))
    with pytest.raises(CommandError):
        call_command("billing_run", "--date", "2027-03-01", "--entity", "ALM", stdout=StringIO())
    r = BillingRun.objects.filter(entity=w.alm).latest("pk")
    assert r.status == "aborted" and "unexpected failure" in r.messages[0]["en"]
    assert "unexpected" not in r.messages[0]["ar"] and r.messages[0]["ar"] == services.ENGLISH_ONLY_AR  # nit 14


def test_split_keeps_english_out_of_the_arabic_side():
    assert services._split("Plain English only") == ("Plain English only", services.ENGLISH_ONLY_AR)
    assert services._split("Refused. / مرفوض.") == ("Refused.", "مرفوض.")
    assert services._split("Trailing / ") == ("Trailing / ", services.ENGLISH_ONLY_AR)


def test_runs_are_append_only_after_finish(w):
    approved(w)
    r = run(w, date(2027, 1, 1))
    from billing.models import BillingError

    with pytest.raises(BillingError):
        r._service_write = True
        r.save()
    item = r.items.get()
    with pytest.raises(BillingError):
        item._service_write = True
        item.save()
    assert BillingPeriod.objects.filter(status="generated").count() == 1
    with pytest.raises(InvoicingError):  # the invoice of a generated period is never deleted
        SalesDocument.objects.filter(pk=item.sales_document_id).delete()


def test_r33_run_refused_inside_a_callers_transaction(w):
    """Each period commits on its own (R33): a run inside another transaction would only make savepoints."""
    from django.db import transaction

    from billing.models import BillingError

    approved(w, start=date(2027, 3, 1))
    w.clock.set(date(2027, 3, 1))
    with transaction.atomic():
        with pytest.raises(BillingError) as exc:
            services.run_billing(date(2027, 3, 1), entity=w.alm, user=w.sys)
    assert exc.value.code == "run_in_transaction" and "معاملة" in str(exc.value)
    assert not BillingRun.objects.exists()
    assert run(w, date(2027, 3, 1)).generated_count == 1  # outside a transaction it runs
