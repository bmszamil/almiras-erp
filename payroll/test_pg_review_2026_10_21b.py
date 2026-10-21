"""PostgreSQL only: payroll review fixes of 2026-10-21, fifth review (payroll §10.9 items 30 to 38).

Item 8 (payroll.0024): a bai' ajel installment outstanding after exit becomes ``written_off`` or
``recovered_after_exit`` only when an approved closure of its contract lists it (at that amount for a recovery);
rejected closures never change, approved ones only receive their entry once. Item 9: two approvals of the same closure
at once post once. The migration reverses to 0023 and re-applies."""
import threading

import pytest
from django.db import DatabaseError, connection, connections, transaction

from ledger.models import JournalEntry
from payroll import services
from payroll.models import DeferredSaleClosure, DeferredSaleContract
from payroll.test_pg_review_2026_10_20c import seeded_db  # noqa: F401  (a fixture)
from payroll.test_pg_review_2026_10_21 import _columns, _prosrc, _triggers
from payroll.test_review_fixes_2026_10_21 import _after_exit_balance, _recovery, _write_off

pytestmark = pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL triggers and row locks")

UPD = "UPDATE payroll_deferredsaleinstallment SET {} WHERE id = %s"


def _refused(sql, params=()):
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql, params)


def _exec(sql, params=()):
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql, params)


# --- item 8: the installments move only with an approved closure that lists them ----------------------------------

@pytest.mark.django_db
def test_item8_no_installment_is_written_off_or_recovered_without_an_approved_closure(w):
    n1, c = _after_exit_balance(w)
    inst = c.installments.get(status="outstanding_after_exit")
    _refused(UPD.format("status = 'written_off'"), [inst.pk])
    _refused(UPD.format("status = 'recovered_after_exit', amount = 10.00, deducted_amount = 10.00"), [inst.pk])
    # a submitted closure listing it is not enough
    x = _write_off(w, c)
    _exec("UPDATE payroll_deferredsaleclosure SET installment_parts = %s::jsonb WHERE id = %s",
          [f'[{{"installment_id": {inst.pk}, "amount": "69.00"}}]', x.pk])
    _refused(UPD.format("status = 'written_off'"), [inst.pk])
    inst.refresh_from_db()
    assert inst.status == "outstanding_after_exit"


@pytest.mark.django_db
def test_item8_an_approved_write_off_moves_only_the_installments_it_lists(w):
    n1, c = _after_exit_balance(w)
    x = services.approve_deferred_sale_closure(_write_off(w, c), user=w.mp)  # the service passes the guard
    assert set(c.installments.exclude(status="settled_in_final").values_list("status", flat=True)) == {"written_off"}
    # an installment the write-off does not list (collected by the final settlement) never moves
    other = c.installments.filter(status="settled_in_final").first()
    assert other is not None
    _refused(UPD.format("status = 'written_off'"), [other.pk])
    assert x.installment_parts


@pytest.mark.django_db
def test_item8_an_approved_recovery_moves_its_installment_only_at_the_amount_it_lists(w):
    n1, c = _after_exit_balance(w)
    x = services.approve_deferred_sale_closure(_recovery(w, c, "30.00"), user=w.mp)
    rest = c.installments.get(status="outstanding_after_exit")
    assert rest.amount == 39
    for sql in ("status = 'recovered_after_exit', amount = 39.00, deducted_amount = 39.00",  # not listed
                "status = 'written_off'"):  # a recovery is not a write-off
        _refused(UPD.format(sql), [rest.pk])
    # listing the remainder on the approved recovery is itself refused (an approved closure is locked)
    _refused("UPDATE payroll_deferredsaleclosure SET installment_parts = installment_parts || %s::jsonb WHERE id = %s",
             [f'[{{"installment_id": {rest.pk}, "amount": "39.00"}}]', x.pk])


@pytest.mark.django_db
def test_item8_a_rejected_closure_never_changes_and_is_never_deleted(w):
    n1, c = _after_exit_balance(w)
    x = _write_off(w, c)
    services.reject_deferred_sale_closure(x, user=w.mp, reason_ar="لا")
    for sql in ("status = 'submitted'", "status = 'approved'", "amount = 1.00", "rejected_reason_ar = 'x'",
                """installment_parts = '[{"installment_id": 1, "amount": "1.00"}]'::jsonb""", "vat_share = 0"):
        _refused(f"UPDATE payroll_deferredsaleclosure SET {sql} WHERE id = %s", [x.pk])
    _refused("DELETE FROM payroll_deferredsaleclosure WHERE id = %s", [x.pk])
    # a draft or submitted closure is the service's to change (status rules in the model)
    y = services.prepare_deferred_sale_closure(DeferredSaleContract.objects.get(pk=c.pk), user=w.p, kind="write_off",
                                               reason_ar="سبب")
    _exec("UPDATE payroll_deferredsaleclosure SET reason_en = 'x' WHERE id = %s", [y.pk])
    _refused("DELETE FROM payroll_deferredsaleclosure WHERE id = %s", [y.pk])


@pytest.mark.django_db
def test_item8_an_approved_closure_receives_its_entry_once_only(w):
    n1, c = _after_exit_balance(w)
    x = services.approve_deferred_sale_closure(_write_off(w, c), user=w.mp)
    for sql in ("journal_entry_id = NULL", "vat_share = 0", "warnings = '[]'::jsonb", "status = 'rejected'"):
        _refused(f"UPDATE payroll_deferredsaleclosure SET {sql} WHERE id = %s", [x.pk])


# --- item 9: two approvals of the same closure at once ------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_item9_concurrent_approvals_of_the_same_closure_post_once(seeded_db, w):
    n1, c = _after_exit_balance(w)
    x = _write_off(w, c)
    start = threading.Barrier(2)
    res = []

    def approve():
        try:
            start.wait(10)
            services.approve_deferred_sale_closure(DeferredSaleClosure.objects.get(pk=x.pk), user=w.mp)
            res.append("ok")
        except Exception as exc:  # noqa: BLE001 - the failure is the result under test
            res.append(getattr(exc, "code", type(exc).__name__))
        finally:
            connections.close_all()

    threads = [threading.Thread(target=approve) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert sorted(res) == ["closure_status", "ok"]
    x = DeferredSaleClosure.objects.get(pk=x.pk)
    assert x.status == "approved" and x.journal_entry_id
    assert JournalEntry.objects.filter(document_type="payroll.deferredsaleclosure").count() == 1
    assert set(c.installments.exclude(status="settled_in_final").values_list("status", flat=True)) == {"written_off"}


# --- payroll.0024 reverses and re-applies -------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_migration_0024_reverses_and_reapplies_on_postgresql():
    from django.db.migrations.executor import MigrationExecutor

    def migrate(targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)

    leaf = MigrationExecutor(connection).loader.graph.leaf_nodes()
    assert "payroll_deferredsaleclosure" in _prosrc("payroll_installment_lock")
    assert "rejected" in _prosrc("payroll_sale_closure_lock")
    try:
        migrate([("payroll", "0023_slice3_review_fixes")])
        assert "vat_share" not in _columns("payroll_deferredsaleclosure")
        assert "payroll_deferredsaleclosure" not in _prosrc("payroll_installment_lock")
        assert "recovered_after_exit" in _prosrc("payroll_installment_lock")  # 0020's body again
        assert _prosrc("payroll_sale_closure_lock") is None
        assert "payroll_sale_closure_lock" in _triggers("payroll_deferredsaleclosure")  # 0020's trigger
    finally:
        migrate(leaf)
    assert {"vat_share", "warnings"} <= _columns("payroll_deferredsaleclosure")
    assert "payroll_deferredsaleclosure" in _prosrc("payroll_installment_lock")
    assert "payroll_sale_closure_lock" in _triggers("payroll_deferredsaleclosure")
