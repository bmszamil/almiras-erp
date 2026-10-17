"""Approved versions, generated periods, corrections and run records change only through billing.services
(R10, R11, R38, R41, R42, R46). Model and queryset guards run everywhere; the triggers (migration 0002) and the
advisory locks (AT-3, AT-27) only on PostgreSQL."""
import threading
import time
from datetime import date

import pytest
from django.db import DatabaseError, connection, connections, transaction

from billing import services
from billing.conftest import approved, clear, period, postgres_only, run, version
from billing.models import (
    BillingAdjustment,
    BillingError,
    BillingPeriod,
    BillingRunItem,
    ContractEvent,
    ContractLine,
    ContractVersion,
)
from invoicing.models import SalesDocument


@pytest.fixture
def billed(w):
    c = approved(w)
    run(w, date(2027, 1, 1))
    clear(w, period(c, date(2027, 1, 1)).sales_document)
    w.clock.set(date(2027, 1, 20))
    services.approve_version(version(w, c, effective=date(2027, 1, 16), amount="12000.00"), user=w.c)
    return c


def _raw(sql, params=()):
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql, params)


def _refused(sql, params=()):
    with pytest.raises(DatabaseError):
        _raw(sql, params)


# --- everywhere: model and queryset guards -------------------------------------------------------------

def test_generated_period_never_changes_or_bills_again(w, billed):
    p = period(billed, date(2027, 1, 1))
    p.invoice_lines = [{"line_key": "x", "net": "1.00"}]
    p._service_write = True
    with pytest.raises(BillingError):
        p.save()
    p = period(billed, date(2027, 1, 1))
    p.status = "planned"
    p._service_write = True
    with pytest.raises(BillingError):
        p.save()
    with pytest.raises(BillingError):
        BillingPeriod.objects.filter(pk=p.pk).update(status="planned")
    with pytest.raises(BillingError):
        p.delete()
    plain = period(billed, date(2027, 2, 1))
    plain.status = "generated"
    with pytest.raises(BillingError):  # not through a service
        plain.save()


def test_contract_header_frozen_after_draft(w, billed):
    """R7, R39 (review finding 7): customer, currency, number, start, due days, lead days and counterparty never
    change once approved; due days of later invoices come from the version."""
    from billing.models import Contract

    for name, value in (("customer", w.ic_customer), ("due_days", 45), ("bill_lead_days", 3),
                        ("start_date", date(2026, 12, 1)), ("number", "ALM-CTR-2027-999999"),
                        ("counterparty_entity", w.sub)):
        stored = Contract.objects.get(pk=billed.pk)
        setattr(stored, name, value)
        stored._service_write = True
        with pytest.raises(BillingError) as exc:
            stored.save()
        assert exc.value.code == "contract_frozen" and "نسخة" in str(exc.value)
    stored = Contract.objects.get(pk=billed.pk)
    stored._service_write = True
    with pytest.raises(BillingError):
        stored.delete()
    stored = Contract.objects.get(pk=billed.pk)
    stored.title_en = "Renamed"
    stored._service_write = True
    stored.save()  # header texts are not frozen


def test_due_days_amended_by_version_reach_the_invoice_contract_header_unchanged(w):
    c = approved(w, due_days=10)
    run(w, date(2027, 1, 1))
    w.clock.set(date(2027, 1, 20))
    services.approve_version(version(w, c, effective=date(2027, 2, 1), due_days=30), user=w.c)
    c.refresh_from_db()
    assert c.due_days == 10 and c.current_version.due_days == 30
    run(w, date(2027, 2, 1))
    feb = period(c, date(2027, 2, 1))
    assert feb.billed_version.due_days == 30 and feb.sales_document.due_date == date(2027, 3, 3)


def test_generated_period_goes_to_failed_only_needing_release(w, billed):
    """R12 (review finding 8): the model guard refuses an automatically retried failed for a generated period."""
    p = period(billed, date(2027, 1, 1))
    p.status, p.sales_document = "failed", None
    p._service_write = True
    with pytest.raises(BillingError):
        p.save()
    p = period(billed, date(2027, 1, 1))
    p.status, p.sales_document, p.needs_release = "failed", None, True
    p._service_write = True
    p.save()
    assert BillingPeriod.objects.get(pk=p.pk).status == "failed"


def test_approved_lines_and_computed_corrections_are_read_only(w, billed):
    line = ContractLine.objects.filter(version__version_no=1, contract=billed).get()
    line._service_write = True
    with pytest.raises(BillingError):
        line.delete()
    adj = BillingAdjustment.objects.get(contract=billed)
    adj.lines = []
    adj._service_write = True
    with pytest.raises(BillingError):
        adj.save()
    with pytest.raises(BillingError):
        BillingAdjustment.objects.filter(pk=adj.pk).update(status="final")
    with pytest.raises(BillingError):
        BillingAdjustment.objects.get(pk=adj.pk).delete()
    with pytest.raises(BillingError):
        ContractVersion.objects.filter(contract=billed).update(reason_ar="x")


# --- PostgreSQL: triggers ---------------------------------------------------------------------------------

@postgres_only
def test_triggers_refuse_raw_sql(w, billed):
    v1 = ContractVersion.objects.get(contract=billed, version_no=1)
    line = ContractLine.objects.get(version=v1)
    p = period(billed, date(2027, 1, 1))
    adj = BillingAdjustment.objects.get(contract=billed)
    item = BillingRunItem.objects.filter(billing_period=p).first()
    event = ContractEvent.objects.filter(contract=billed).first()
    _refused("UPDATE billing_contractversion SET reason_ar = 'x' WHERE id = %s", [v1.pk])
    _refused("DELETE FROM billing_contractversion WHERE id = %s", [v1.pk])
    _refused("UPDATE billing_contractline SET amount = 1 WHERE id = %s", [line.pk])
    _refused("DELETE FROM billing_contractline WHERE id = %s", [line.pk])
    _refused("UPDATE billing_billingperiod SET invoice_lines = '[]' WHERE id = %s", [p.pk])
    _refused("UPDATE billing_billingperiod SET status = 'planned' WHERE id = %s", [p.pk])
    _refused("UPDATE billing_billingperiod SET sales_document_id = NULL WHERE id = %s", [p.pk])
    _refused("DELETE FROM billing_billingperiod WHERE id = %s", [p.pk])
    _refused("UPDATE billing_billingadjustment SET lines = '[]' WHERE id = %s", [adj.pk])
    _refused("DELETE FROM billing_billingadjustment WHERE id = %s", [adj.pk])
    _refused("UPDATE billing_billingrunitem SET message_en = 'x' WHERE id = %s", [item.pk])
    _refused("DELETE FROM billing_contractevent WHERE id = %s", [event.pk])
    _refused("UPDATE billing_billingrun SET status = 'running' WHERE id = %s", [item.run_id])
    _refused(
        "INSERT INTO billing_contractversion (entity_id, contract_id, version_no, kind, effective_date, "
        "billing_frequency, billing_timing, period_anchor, reason_en, reason_ar, status, rejected_reason_en, "
        "rejected_reason_ar, created_by_id, created_at, updated_at, source, approved_by_id) VALUES "
        "(%s, %s, 99, 'amendment', '2027-01-01', 'monthly', 'advance', 'calendar', '', 'x', 'approved', '', '', %s, "
        "now(), now(), 'human', %s)", [w.alm.pk, billed.pk, w.s.pk, w.c.pk])
    # a generated period never goes to an automatically retried failed (R12, review finding 8, migration 0004)
    _refused("UPDATE billing_billingperiod SET status = 'failed', sales_document_id = NULL WHERE id = %s", [p.pk])
    # the one allowed change of a generated period: to failed with no document, needing release (R12)
    _raw("UPDATE billing_billingperiod SET status = 'failed', sales_document_id = NULL, needs_release = true "
         "WHERE id = %s", [p.pk])
    assert BillingPeriod.objects.get(pk=p.pk).status == "failed"


@postgres_only
def test_contract_header_trigger_after_draft(w, billed):
    """R7, R39 (review finding 7, migration 0003): raw SQL cannot change a non-draft contract's header or delete it."""
    from billing.conftest import contract
    from core.models import Currency

    for sql, params in (
        ("UPDATE billing_contract SET customer_id = %s WHERE id = %s", [w.ic_customer.pk, billed.pk]),
        ("UPDATE billing_contract SET currency_id = 'USD' WHERE id = %s", [billed.pk]),
        ("UPDATE billing_contract SET due_days = 99 WHERE id = %s", [billed.pk]),
        ("UPDATE billing_contract SET bill_lead_days = 5 WHERE id = %s", [billed.pk]),
        ("UPDATE billing_contract SET start_date = '2026-12-01' WHERE id = %s", [billed.pk]),
        ("UPDATE billing_contract SET counterparty_entity_id = %s WHERE id = %s", [w.sub.pk, billed.pk]),
        ("UPDATE billing_contract SET number = 'X-1' WHERE id = %s", [billed.pk]),
        ("UPDATE billing_contract SET entity_id = %s WHERE id = %s", [w.sub.pk, billed.pk]),
        ("DELETE FROM billing_contract WHERE id = %s", [billed.pk]),
    ):
        _refused(sql, params)
    _raw("UPDATE billing_contract SET title_en = 'Renamed' WHERE id = %s", [billed.pk])  # not frozen
    draft = contract(w)
    _raw("UPDATE billing_contract SET currency_id = 'USD', due_days = 7 WHERE id = %s", [draft.pk])  # drafts change
    assert Currency.objects.get(code="USD").code == draft.__class__._base_manager.get(pk=draft.pk).currency_id


# --- PostgreSQL: advisory locks (AT-3, AT-27) --------------------------------------------------------------

def _seeded_world():
    from types import SimpleNamespace

    from django.apps import apps
    from django.contrib.auth import get_user_model

    from billing.testing import FakeInvoicing
    from core import access
    from core.access.models import Role, RolePermission, UserProfile
    from core.access.seed import apply_seed, apply_system_users
    from core.access.testing import make_user, member
    from core.currency_seed import apply_currency_seed
    from core.models import Currency
    from invoicing import reference_seed
    from invoicing import services as inv
    from invoicing.conftest import ADDRESS, make_entity

    apply_seed(Role, RolePermission)
    apply_currency_seed(Currency)
    apply_system_users(get_user_model(), UserProfile)
    reference_seed.apply_all(apps)
    alm = make_entity("ALM", "parent", vat="300000000000003", cr="1010000001")
    s, c, c2 = make_user("s"), make_user("c"), make_user("c_b")
    member(s, alm, "accountant")
    member(c, alm, "approver")
    member(c2, alm, "approver")
    sys = access.system_user("billing")
    member(sys, alm, "system_billing")
    customer = inv.create_customer(alm, user=s, code="C1", name_ar="عميل", is_vat_registered=True,
                                   vat_number="310000000000003", **ADDRESS)
    return SimpleNamespace(alm=alm, s=s, c=c, c_b=c2, sys=sys, customer=customer, fake=FakeInvoicing())


@postgres_only
@pytest.mark.django_db(transaction=True)
def test_at3_concurrent_runs_bill_once(monkeypatch):
    from billing import invoicing_port
    from billing.conftest import contract

    from datetime import datetime, time as clock_time

    from invoicing import services as inv

    w = _seeded_world()
    monkeypatch.setattr(invoicing_port, "_override", w.fake)
    monkeypatch.setattr(services, "_today", lambda: date(2027, 1, 1))
    monkeypatch.setattr(inv, "_riyadh_now", lambda: datetime.combine(date(2027, 1, 1), clock_time(10),
                                                                    tzinfo=services.RIYADH))
    c = services.approve_contract(services.submit_contract(contract(w), user=w.s), user=w.c)
    results, errors = [], []

    def worker():
        try:
            results.append(services.run_billing(date(2027, 1, 1), entity=w.alm, user=w.sys)[0])
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(repr(exc))
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    assert sorted((r.generated_count, r.failed_count) for r in results) == [(0, 0), (1, 0)]
    assert SalesDocument.objects.filter(entity=w.alm, doc_type="invoice").count() == 1
    assert BillingPeriod.objects.get(contract=c, period_start=date(2027, 1, 1)).status == "generated"


@postgres_only
@pytest.mark.django_db(transaction=True)
def test_at27_version_approval_waits_for_a_running_billing_run(monkeypatch):
    from billing import invoicing_port
    from billing.conftest import contract

    w = _seeded_world()
    monkeypatch.setattr(invoicing_port, "_override", w.fake)
    monkeypatch.setattr(services, "_today", lambda: date(2027, 1, 1))
    c = services.approve_contract(services.submit_contract(contract(w), user=w.s), user=w.c)
    v = services.create_version(c, user=w.s, kind="amendment", effective_date=date(2027, 2, 1), reason_ar="تعديل")
    v = services.submit_version(v, user=w.s)
    held, release = threading.Event(), threading.Event()

    def runner():  # a billing run in progress holds the entity's lock
        try:
            services._run_lock(w.alm)
            held.set()
            release.wait(10)
            services._run_unlock(w.alm)
        finally:
            connections.close_all()

    t = threading.Thread(target=runner)
    t.start()
    assert held.wait(10)
    with pytest.raises(BillingError) as exc:
        services.approve_version(v, user=w.c)
    assert exc.value.code == "run_in_progress" and "قيد التنفيذ" in str(exc.value)
    assert exc.value.retryable  # HR's delivery retries it (billing.hr_port)
    assert ContractVersion.objects.get(pk=v.pk).status == "submitted"
    release.set()
    t.join(10)
    time.sleep(0.1)
    assert services.approve_version(v, user=w.c).status == "approved"


# --- PostgreSQL: lock timeouts (review nit 11) --------------------------------------------------------------

def _hold(sql_params, held, release):
    """Another session holds a lock until ``release`` is set."""
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(*sql_params)
            held.set()
            release.wait(20)
    finally:
        connections.close_all()


@postgres_only
@pytest.mark.django_db(transaction=True)
def test_row_lock_timeout_fails_the_period_and_the_run_goes_on(monkeypatch, settings):
    from billing import invoicing_port
    from billing.conftest import contract

    from datetime import datetime, time as clock_time

    from invoicing import services as inv

    settings.BILLING_ROW_LOCK_TIMEOUT = "300ms"
    w = _seeded_world()
    monkeypatch.setattr(invoicing_port, "_override", w.fake)
    monkeypatch.setattr(services, "_today", lambda: date(2027, 1, 1))
    monkeypatch.setattr(inv, "_riyadh_now", lambda: datetime.combine(date(2027, 1, 1), clock_time(10),
                                                                    tzinfo=services.RIYADH))
    c1 = services.approve_contract(services.submit_contract(contract(w), user=w.s), user=w.c)
    c2 = services.approve_contract(services.submit_contract(contract(w), user=w.s), user=w.c)
    locked = BillingPeriod.objects.get(contract=c1, period_start=date(2027, 1, 1))
    held, release = threading.Event(), threading.Event()
    t = threading.Thread(target=_hold, args=(("SELECT id FROM billing_billingperiod WHERE id = %s FOR UPDATE",
                                              [locked.pk]), held, release))
    t.start()
    try:
        assert held.wait(10)
        started = time.monotonic()
        r = services.run_billing(date(2027, 1, 1), entity=w.alm, user=w.sys)[0]
        assert time.monotonic() - started < 15  # never hangs on the lock
    finally:
        release.set()
        t.join(20)
    assert (r.status, r.generated_count, r.failed_count) == ("completed_with_errors", 1, 1)
    assert any(m.get("code") == "lock_timeout" and "مقفلة" in m["ar"] for m in r.messages)
    assert BillingPeriod.objects.get(pk=locked.pk).status == "planned"  # untouched, tried next run
    assert BillingPeriod.objects.get(contract=c2, period_start=date(2027, 1, 1)).status == "generated"
    again = services.run_billing(date(2027, 1, 1), entity=w.alm, user=w.sys)[0]
    assert again.generated_count == 1 and BillingPeriod.objects.get(pk=locked.pk).status == "generated"


@postgres_only
@pytest.mark.django_db(transaction=True)
def test_run_lock_timeout_aborts_the_second_run(monkeypatch, settings):
    from billing import invoicing_port
    from billing.conftest import contract

    settings.BILLING_RUN_LOCK_TIMEOUT = "300ms"
    w = _seeded_world()
    monkeypatch.setattr(invoicing_port, "_override", w.fake)
    monkeypatch.setattr(services, "_today", lambda: date(2027, 1, 1))
    services.approve_contract(services.submit_contract(contract(w), user=w.s), user=w.c)
    held, release = threading.Event(), threading.Event()
    t = threading.Thread(target=_hold, args=(("SELECT pg_advisory_xact_lock(%s, %s)", [services.LOCK_CLASS,
                                                                                         w.alm.pk]), held, release))
    t.start()
    try:
        assert held.wait(10)
        r = services.run_billing(date(2027, 1, 1), entity=w.alm, user=w.sys)[0]
    finally:
        release.set()
        t.join(20)
    assert r.status == "aborted" and "in progress" in r.messages[0]["en"] and "قيد التنفيذ" in r.messages[0]["ar"]
    assert not BillingPeriod.objects.filter(status="generated").exists()
