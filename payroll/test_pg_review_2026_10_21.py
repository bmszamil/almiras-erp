"""PostgreSQL only: review fixes of 2026-10-21, fourth review (payroll §10.9 items 23 to 29). Ported from the
reviewer's test_zz_rv4_pg.py (D1).

Item 3: ``_refresh_rebates`` locks the contract before its rebate, as the rebate approval does, so a refresh and an
approval of the same rebate never deadlock. Item 5: ``payroll_installment_lock`` (payroll.0020) lets a bai' ajel
installment outstanding after exit move only to ``written_off`` or ``recovered_after_exit``; closures are locked once
approved. Item 7: payroll.0019 (and 0020) reverse and re-apply on PostgreSQL."""
import threading

import pytest
from django.db import DatabaseError, connection, connections, transaction

from payroll import services
from payroll.models import DeferredSaleContract, DeferredSaleRebate
from payroll.test_pg_review_2026_10_20c import seeded_db  # noqa: F401  (a fixture)
from payroll.test_review_fixes_2026_10_20b import planned, rebate, sold

pytestmark = pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL triggers and row locks")


# --- item 3: lock order --------------------------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_item3_refresh_and_approval_of_the_same_rebate_never_deadlock(seeded_db, w, monkeypatch):
    """The reviewer's D1: thread A (the refresh) is held just before it locks the contract until thread B (the
    approval of the same approved rebate, whose note ZATCA rejected) holds the contract. Before the fix A held the
    rebate row already (``select_for_update`` on the rebates first) and B waited for it while A waited for the
    contract: PostgreSQL broke the deadlock by failing one of them."""
    c = sold(w)
    r = services.approve_deferred_sale_rebate(rebate(w, c, amount="120.00"), user=w.mp)
    w.invoicing.rebate_status = "rejected"
    real = services._lock
    a_waits, b_holds = threading.Event(), threading.Event()
    who = threading.local()

    def lock(model, pk):
        if getattr(who, "name", "") == "A" and model is DeferredSaleContract:
            a_waits.set()
            b_holds.wait(10)
            return real(model, pk)
        out = real(model, pk)
        if getattr(who, "name", "") == "B" and model is DeferredSaleContract:
            b_holds.set()
        return out

    monkeypatch.setattr(services, "_lock", lock)
    res = {}

    def a():
        who.name = "A"
        try:
            services._refresh_rebates(w.alm)
            res["A"] = "ok"
        except Exception as exc:  # noqa: BLE001 - the failure is the result under test
            res["A"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        finally:
            connections.close_all()

    def b():
        who.name = "B"
        a_waits.wait(10)
        try:
            services.approve_deferred_sale_rebate(DeferredSaleRebate.objects.get(pk=r.pk), user=w.mp)
            res["B"] = "ok"
        except Exception as exc:  # noqa: BLE001
            res["B"] = f"{getattr(exc, 'code', '')} {type(exc).__name__}: {str(exc)[:120]}"
        finally:
            connections.close_all()

    threads = [threading.Thread(target=a), threading.Thread(target=b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert res == {"A": "ok", "B": "ok"}
    # B restored the cuts (rejected note read under its locks) and approved again with a new note; A then read the
    # rebate again under the contract lock and found the new note (rejected by the test port too) and restored it
    row = DeferredSaleRebate.objects.get(pk=r.pk)
    assert row.status == "rejected" and len(w.invoicing.rebates) == 2
    assert len(planned(c)) == 12  # everything given back exactly once per approval


# --- item 5: installment and closure guards ------------------------------------------------------------------------

def _refused(sql, params=()):
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql, params)


def _ok(sql, params=()):
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql, params)


@pytest.mark.django_db
def test_item5_after_exit_installments_move_only_to_written_off_or_recovered(w):
    from payroll.test_review_fixes_2026_10_21 import _after_exit_balance

    n1, c = _after_exit_balance(w)
    inst = c.installments.get(status="outstanding_after_exit")
    upd = "UPDATE payroll_deferredsaleinstallment SET {} WHERE id = %s"
    for sql in ("status = 'planned'", "amount = 1.00", "status = 'deducted'",
                "status = 'written_off', amount = 1.00",
                "status = 'recovered_after_exit', amount = 10.00, deducted_amount = 9.00",
                "status = 'recovered_after_exit', amount = 70.00, deducted_amount = 70.00",  # more than owed
                "status = 'recovered_after_exit', amount = 0, deducted_amount = 0",
                "status = 'recovered_after_exit', amount = 10.00, deducted_amount = 10.00, period = '2030-01'"):
        _refused(upd.format(sql), [inst.pk])
    _refused("DELETE FROM payroll_deferredsaleinstallment WHERE id = %s", [inst.pk])
    # review 5 (item 8, payroll.0024): only an approved recovery listing it at that amount moves it
    _refused(upd.format("status = 'recovered_after_exit', amount = 10.00, deducted_amount = 10.00"), [inst.pk])
    from payroll.test_review_fixes_2026_10_21 import _recovery
    services.approve_deferred_sale_closure(_recovery(w, c, "10.00"), user=w.mp)
    inst.refresh_from_db()
    assert (inst.status, inst.amount) == ("recovered_after_exit", 10)
    for sql in ("status = 'outstanding_after_exit'", "amount = 9.00, deducted_amount = 9.00", "status = 'written_off'"):
        _refused(upd.format(sql), [inst.pk])  # recovered is final
    _refused("DELETE FROM payroll_deferredsaleinstallment WHERE id = %s", [inst.pk])


@pytest.mark.django_db
def test_item5_a_written_off_installment_is_final_and_an_approved_closure_is_locked(w):
    from payroll.test_review_fixes_2026_10_21 import _after_exit_balance, _write_off

    n1, c = _after_exit_balance(w)
    x = services.approve_deferred_sale_closure(_write_off(w, c), user=w.mp)
    inst = c.installments.get(status="written_off")
    for sql in ("status = 'outstanding_after_exit'", "amount = 1.00", "status = 'recovered_after_exit'"):
        _refused(f"UPDATE payroll_deferredsaleinstallment SET {sql} WHERE id = %s", [inst.pk])
    for sql in ("amount = 1.00", "profit_share = 0", "status = 'rejected'", "journal_entry_id = NULL",
                "installment_parts = '[]'"):
        _refused(f"UPDATE payroll_deferredsaleclosure SET {sql} WHERE id = %s", [x.pk])
    _refused("DELETE FROM payroll_deferredsaleclosure WHERE id = %s", [x.pk])


# --- item 7: payroll.0019 (and 0020) reverse on PostgreSQL ----------------------------------------------------------

def _prosrc(name):
    with connection.cursor() as cursor:
        cursor.execute("SELECT prosrc FROM pg_proc WHERE proname = %s", [name])
        row = cursor.fetchone()
    return row[0] if row else None


def _columns(table):
    with connection.cursor() as cursor:
        return {c.name for c in connection.introspection.get_table_description(cursor, table)}


def _triggers(table):
    with connection.cursor() as cursor:
        cursor.execute("SELECT tgname FROM pg_trigger WHERE tgrelid = %s::regclass AND NOT tgisinternal", [table])
        return {r[0] for r in cursor.fetchall()}


@pytest.mark.django_db(transaction=True)
def test_item7_migrations_0019_and_0020_reverse_and_reapply_on_postgresql():
    from django.db.migrations.executor import MigrationExecutor

    def migrate(targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)

    leaf = MigrationExecutor(connection).loader.graph.leaf_nodes()
    assert "approval_token" in _columns("payroll_deferredsalerebate")
    assert "OLD.status = 'rejected' AND NEW.status = 'rejected'" in _prosrc("payroll_rebate_lock")
    assert "recovered_after_exit" in _prosrc("payroll_installment_lock")
    try:
        migrate([("payroll", "0018_review_fixes_rebate_wht")])
        assert "approval_token" not in _columns("payroll_deferredsalerebate")
        assert "wht_uncovered" not in _columns("payroll_payrollpayment")
        assert "payroll_deferredsaleclosure" not in connection.introspection.table_names()
        rebate_lock = _prosrc("payroll_rebate_lock")
        assert rebate_lock and "approval_token" not in rebate_lock  # 0018's body again
        installment_lock = _prosrc("payroll_installment_lock")
        assert "recovered_after_exit" not in installment_lock and "payroll_loaninstallment" in installment_lock
        assert "payroll_rebate_lock" in _triggers("payroll_deferredsalerebate")
    finally:
        migrate(leaf)
    assert "approval_token" in _columns("payroll_deferredsalerebate")
    assert "wht_uncovered" in _columns("payroll_payrollpayment")
    assert "approval_token" in _prosrc("payroll_rebate_lock")
    assert "recovered_after_exit" in _prosrc("payroll_installment_lock")
    assert "payroll_sale_closure_lock" in _triggers("payroll_deferredsaleclosure")
