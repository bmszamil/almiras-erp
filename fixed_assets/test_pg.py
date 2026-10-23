"""PostgreSQL-only guards of the register (fixed-assets.md R15, R30, R40, R42; AT-34): triggers refuse deletes and
entity changes made in SQL, and the row locks serialise two preparations of one period and a register load against the
posting of the opening entry. Skipped on SQLite; the PostgreSQL CI job runs them (REQUIRE_POSTGRES=1)."""
import threading
from datetime import date
from decimal import Decimal

import pytest
from django.db import connection, connections, transaction

from fixed_assets import services, testing as t
from fixed_assets.messages import FixedAssetError

postgres_only = pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL triggers and row locks")
D = Decimal
TABLES = ("assetcategory", "assetlocation", "fixedassetsettings", "depreciationpolicy", "fixedasset",
          "assetcostcomponent", "depreciationrun", "depreciationline", "assetevent", "ipfairvalue", "reviewreminder")


def _seed():
    """A transaction=True test starts from a flushed database: the seeded reference data again."""
    from django.apps import apps
    from django.contrib.auth import get_user_model

    from core.access.models import Role, RolePermission, UserProfile
    from core.access.seed import apply_seed, apply_system_users
    from core.currency_seed import apply_currency_seed
    from core.models import Currency
    from invoicing import reference_seed

    apply_seed(Role, RolePermission)
    apply_currency_seed(Currency)
    apply_system_users(get_user_model(), UserProfile)
    reference_seed.apply_all(apps)


@postgres_only
@pytest.mark.django_db
def test_at34_sql_deletes_and_entity_changes_are_refused(w):
    from fixed_assets import jobs

    bill = t.bill(w, w.alm, [("1510", "480000.00"), ("1520", "60000.00")], on=date(2027, 1, 1))
    ip = t.capitalise(w, t.line_of(bill, 1), available=date(2027, 1, 1), category_code="IPB",
                      usage="investment_property", held_for="rental")
    car = t.capitalise(w, t.line_of(bill, 2), available=date(2027, 1, 1))
    run = t.run(w, w.alm, "2027-01")
    t.event(w, car, "write_off", date(2027, 2, 1))
    services.record_ip_fair_value(ip, user=w.a, valuation_date=date(2027, 1, 31), amount=D("490000.00"),
                                  valuer_name_ar="مقيم", evidence_ref="EV-1")
    jobs.raise_reminders(date(2027, 12, 31))
    for table in TABLES:
        name = f"fixed_assets_{table}"
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT id FROM {name} ORDER BY id LIMIT 1")
            pk = cursor.fetchone()[0]
        with pytest.raises(Exception) as exc, transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(f"DELETE FROM {name} WHERE id = %s", [pk])
        assert "R42" in str(exc.value), table
        with pytest.raises(Exception) as exc, transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(f"UPDATE {name} SET entity_id = %s WHERE id = %s", [w.sub.pk, pk])
        assert "R30" in str(exc.value) or "R42" in str(exc.value) or "R4" in str(exc.value), table
    line = run.lines.first()
    with pytest.raises(Exception) as exc, transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("UPDATE fixed_assets_depreciationline SET amount = 1 WHERE id = %s", [line.pk])
    assert "posted run" in str(exc.value)
    component = car.components.first()
    with pytest.raises(Exception), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("UPDATE fixed_assets_assetcostcomponent SET amount = 1 WHERE id = %s", [component.pk])


@postgres_only
@pytest.mark.django_db(transaction=True)
def test_r15_two_preparations_of_one_period_run_one_after_the_other():
    _seed()
    w = t.world(entities=("ALM",))
    bill = t.bill(w, w.alm, [("1520", "60000.00")], on=date(2027, 1, 1))
    t.capitalise(w, t.line_of(bill), available=date(2027, 1, 1))
    period = t.period(w.alm, "2027-01")
    result = {}

    def other():
        try:
            services.prepare_depreciation(w.alm, period, user=w.a2)
            result["ok"] = True
        except FixedAssetError as exc:
            result["code"] = exc.code
        finally:
            connections.close_all()

    with transaction.atomic():
        first = services.prepare_depreciation(w.alm, period, user=w.a)  # holds the period row
        thread = threading.Thread(target=other)
        thread.start()
        thread.join(timeout=1.5)
        assert thread.is_alive(), "the second preparation should wait for the period lock"
    thread.join(timeout=10)
    assert result == {"code": "run_exists"} and first.status == "draft"


@postgres_only
@pytest.mark.django_db(transaction=True)
def test_r40_a_register_load_waits_for_the_opening_entry_lock():
    """R40 parity with migration R25: the port locks the opening entry rows first, so a load and the posting of the
    opening entry never interleave; once it is posted the load is refused."""
    _seed()
    from core.access.testing import make_user, member
    from fixed_assets import migration_port
    from ledger import services as ledger
    from migration import services as migration
    from migration import testing as mt
    from migration.conftest import make_entity
    from migration.fixtures import dry_run as f

    from .test_migration import opening_tb

    h = make_entity(f.HOLDING, "parent")
    make_entity(f.SUBSIDIARY, "full", h, sharia_enforced=False)
    acc, apr = make_user("dry.acc"), make_user("dry.apr")
    member(acc, h, "accountant")
    member(apr, h, "approver")
    mt.approve_map(migration.create_account_map(h, user=acc, rows=f.map_rows(f.HOLDING)), approver=apr)
    f.setup_fixed_assets(h, acc)
    migration.load_opening_balances(h, opening_tb("36000.00"), user=acc, cutover=f.CUTOVER, apply=True)
    vehicles = t.category(h, "VEH").cost_account
    result = {}

    def load():
        try:
            migration_port.load_opening_asset(
                h, user=acc, legacy_code="FA-L-01", description_ar="سيارة", asset_class="vehicle", usage="own_use",
                location="المقر", cost_account=vehicles, acquisition_date=date(2025, 1, 1), cost=D("1000.00"),
                method="straight_line", remaining_life_months=24, cutover=f.CUTOVER)  # R40: a depreciable row
            result["ok"] = True
        except FixedAssetError as exc:
            result["code"] = exc.code
        finally:
            connections.close_all()

    with transaction.atomic():
        assert ledger.migration_opening_posted(h, lock=True) is False  # the posting side holds the opening rows
        thread = threading.Thread(target=load)
        thread.start()
        thread.join(timeout=1.5)
        assert thread.is_alive(), "the load should wait for the opening entry lock"
    thread.join(timeout=10)
    assert result == {"ok": True}


# --- review of ccb9cef, item 9: two links of one bill line ------------------------------------------------------------

@postgres_only
@pytest.mark.django_db(transaction=True)
def test_r4_two_links_of_one_line_run_one_after_the_other():
    _seed()
    w = t.world(entities=("ALM",))
    bill = t.bill(w, w.alm, [("1520", "60000.00")], on=date(2027, 1, 1))
    line = t.line_of(bill)
    result = {}

    def other():
        try:
            services.create_asset_from_line(line, user=w.a2, location=w.loc)
            result["ok"] = True
        except FixedAssetError as exc:
            result["code"] = exc.code
        finally:
            connections.close_all()

    with transaction.atomic():
        first = services.create_asset_from_line(line, user=w.a, location=w.loc)  # holds the line row
        thread = threading.Thread(target=other)
        thread.start()
        thread.join(timeout=1.5)
        assert thread.is_alive(), "the second link should wait for the line lock"
    thread.join(timeout=10)
    assert result == {"code": "line_fully_linked"} and first.cost == D("60000.00")


# --- review of ccb9cef, item 10: posted runs and events keep their values in SQL -------------------------------------

def _refused(sql, params, needle):
    with pytest.raises(Exception) as exc, transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql, params)
    assert needle in str(exc.value), sql


@postgres_only
@pytest.mark.django_db
def test_item10_a_posted_run_only_becomes_reversed(w):
    bill = t.bill(w, w.alm, [("1520", "60000.00")], on=date(2027, 1, 1))
    car = t.capitalise(w, t.line_of(bill), available=date(2027, 1, 1))
    run = t.run(w, w.alm, "2027-01")
    table = "fixed_assets_depreciationrun"
    for column, value in (("status", "draft"), ("status", "discarded"), ("total", 1), ("journal_entry_id", None),
                          ("fingerprint", "x"), ("number", "X-1"), ("posted_by_id", None)):
        _refused(f"UPDATE {table} SET {column} = %s WHERE id = %s", [value, run.pk], "R18")
    _refused("INSERT INTO fixed_assets_depreciationline (entity_id, run_id, asset_id, days, amount, catch_up, "
             "opening_nbv) VALUES (%s, %s, %s, 1, 1, true, 1)", [w.alm.pk, run.pk, car.pk], "R42")
    with connection.cursor() as cursor:  # the reversal request fields stay writable
        cursor.execute(f"UPDATE {table} SET reversal_reason_en = 'test' WHERE id = %s", [run.pk])
    services.request_reversal(run, user=w.a, reason_ar="خطأ")
    t.decide(run, w.c, permission="fixed_assets.reverse_document")  # posted -> reversed is allowed
    run.refresh_from_db()
    assert run.status == "reversed"
    _refused(f"UPDATE {table} SET status = 'posted' WHERE id = %s", [run.pk], "R18")
    _refused(f"UPDATE {table} SET reversal_entry_id = NULL WHERE id = %s", [run.pk], "R18")


@postgres_only
@pytest.mark.django_db
def test_item10_a_posted_or_approved_event_only_becomes_reversed(w):
    bill = t.bill(w, w.alm, [("1520", "60000.00"), ("1520", "1000.00")], on=date(2027, 1, 1))
    car = t.capitalise(w, t.line_of(bill, 1), available=date(2027, 1, 1))
    off = t.event(w, car, "write_off", date(2027, 1, 20))
    table = "fixed_assets_assetevent"
    for column, value in (("status", "draft"), ("amount", 1), ("cost_derecognised", 1),
                          ("depreciation_derecognised", 1), ("journal_entry_id", None), ("event_date", "2027-01-21"),
                          ("depreciation_until", "2027-01-31")):
        _refused(f"UPDATE {table} SET {column} = %s WHERE id = %s", [value, off.pk], "R18")
    other = t.capitalise(w, t.line_of(bill, 2), available=date(2027, 1, 1))
    e = t.event(w, other, "vat_use_change", date(2027, 1, 20), new_vat_use="exempt")  # approved, no entry
    _refused(f"UPDATE {table} SET new_vat_use = 'mixed' WHERE id = %s", [e.pk], "R18")
    _refused(f"UPDATE {table} SET status = 'submitted' WHERE id = %s", [e.pk], "R18")
    services.request_reversal(off, user=w.a, reason_ar="شطب خاطئ")
    t.decide(off, w.c, permission="fixed_assets.reverse_document")
    off.refresh_from_db()
    assert off.status == "reversed"
    _refused(f"UPDATE {table} SET status = 'posted' WHERE id = %s", [off.pk], "R18")


# --- follow-up review of acb5701: evidence and maker trail of posted runs and events --------------------------------

@postgres_only
@pytest.mark.django_db
def test_followup_a_posted_run_keeps_its_maker_trail(w):
    bill = t.bill(w, w.alm, [("1520", "60000.00")], on=date(2027, 1, 1))
    t.capitalise(w, t.line_of(bill), available=date(2027, 1, 1))
    run = t.run(w, w.alm, "2027-01")
    table = "fixed_assets_depreciationrun"
    for column, value in (("created_by_id", w.c.pk), ("created_at", "2020-01-01T00:00:00Z"),
                          ("submitted_by_id", None), ("submitted_at", None)):
        _refused(f"UPDATE {table} SET {column} = %s WHERE id = %s", [value, run.pk], "R18")


@postgres_only
@pytest.mark.django_db
def test_followup_a_posted_or_approved_event_keeps_its_evidence_and_maker_trail(w):
    bill = t.bill(w, w.alm, [("1520", "60000.00"), ("1520", "1000.00")], on=date(2027, 1, 1))
    car = t.capitalise(w, t.line_of(bill, 1), available=date(2027, 1, 1))
    off = t.event(w, car, "write_off", date(2027, 1, 20))
    journal_line = off.journal_entry.lines.first().pk
    table = "fixed_assets_assetevent"
    for column, value in (("cash_cost_price", 1), ("financing_total", 1), ("financing_ref", "MUR-X"),
                          ("recoverable_amount", 1), ("source_journal_line_id", journal_line), ("reason_ar", "x"),
                          ("reason_en", "x"), ("evidence_ref", "x"), ("old_vat_use", "exempt"),
                          ("created_by_id", w.c.pk), ("created_at", "2020-01-01T00:00:00Z"),
                          ("submitted_by_id", None), ("submitted_at", None)):
        _refused(f"UPDATE {table} SET {column} = %s WHERE id = %s", [value, off.pk], "R18")
    other = t.capitalise(w, t.line_of(bill, 2), available=date(2027, 1, 1))
    e = t.event(w, other, "vat_use_change", date(2027, 1, 20), new_vat_use="exempt")  # approved, no entry
    _refused(f"UPDATE {table} SET old_vat_use = 'exempt' WHERE id = %s", [e.pk], "R18")
    with connection.cursor() as cursor:  # previous_values is written once, while still empty
        cursor.execute(f"UPDATE {table} SET previous_values = %s WHERE id = %s", ['{"method": "x"}', e.pk])
    _refused(f"UPDATE {table} SET previous_values = '{{}}'::jsonb WHERE id = %s", [e.pk], "R18")
    _refused(f"UPDATE {table} SET previous_values = %s WHERE id = %s", ['{"method": "y"}', e.pk], "R18")
