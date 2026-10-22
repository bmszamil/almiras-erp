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
                cutover=f.CUTOVER)
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
