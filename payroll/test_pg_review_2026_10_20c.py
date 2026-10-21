"""PostgreSQL only: review fixes of 2026-10-20, third round (payroll §10.9 items 17 to 22).

Finding 3: ``payroll_rebate_lock`` (payroll.0018 as tightened by payroll.0019) against raw SQL, every transition.
Finding 2: the ``payroll_alerts`` command runs the bai' ajel refresh (``select_for_update``) in a transaction per
entity: run here with committed transactions (``transaction=True``), as the daily job runs it."""
import pytest
from django.core.management import call_command
from django.db import DatabaseError, connection, transaction

from payroll import services
from payroll.conftest import D
from payroll.models import DeferredSaleRebate, PayrollAlert
from payroll.test_review_fixes_2026_10_20b import planned, rebate, sold

pg_only = pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL triggers and row locks")


@pytest.fixture
def seeded_db(transactional_db):
    """Transactional tests flush the database: put back the migration-seeded rows the ``w`` fixture and a bai' ajel
    sale rely on (roles, currencies, system users, statutory parameters, invoicing reference data such as VAT
    rates), as payroll's and invoicing's concurrency tests do."""
    from importlib import import_module

    from django.apps import apps
    from django.contrib.auth import get_user_model

    from core.access.models import Role, RolePermission, UserProfile
    from core.access.seed import apply_seed, apply_system_users
    from core.currency_seed import apply_currency_seed
    from core.models import Currency
    from hr.models import StatutoryParameter
    from invoicing import reference_seed
    from payroll.test_pg_concurrency import SEED_MIGRATIONS

    apply_seed(Role, RolePermission)
    apply_currency_seed(Currency)
    apply_system_users(get_user_model(), UserProfile)
    if not StatutoryParameter.objects.exists():
        for module, name in SEED_MIGRATIONS:
            getattr(import_module(module), name)(apps, None)
    reference_seed.apply_all(apps)


def _refused(sql, params=()):
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql, params)


def _ok(sql, params=()):
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql, params)


def _set(pk, sql, ok=True):
    (_ok if ok else _refused)(f"UPDATE payroll_deferredsalerebate SET {sql} WHERE id = %s", [pk])


def _row(pk):
    return DeferredSaleRebate.objects.get(pk=pk)


def _approved(w):
    c = sold(w)
    r = services.approve_deferred_sale_rebate(rebate(w, c, amount="120.00"), user=w.mp)
    assert (r.status, r.credit_note_status, r.approval_token) == ("approved", "issued", "")
    return c, r


@pg_only
@pytest.mark.django_db
def test_f3_a_draft_changes_freely_and_no_rebate_is_ever_deleted(w):
    c = sold(w)
    r = rebate(w, c)
    _set(r.pk, "amount = 61.00, reason_ar = 'سبب', approval_token = 'x'")
    _refused("DELETE FROM payroll_deferredsalerebate WHERE id = %s", [r.pk])
    _, a = _approved(w)
    _refused("DELETE FROM payroll_deferredsalerebate WHERE id = %s", [a.pk])


@pg_only
@pytest.mark.django_db
def test_f3_an_approved_rebate_only_records_its_note_final_or_rejected(w):
    c, r = _approved(w)
    for sql in ("amount = 1.00", "vat_amount = 0", "credit_note_ref = 'OTHER'", "installment_cuts = '[]'",
                "approved_by_id = NULL", "approval_token = 'x'", "status = 'discarded'", "status = 'draft'",
                "credit_note_status = 'rejected'",  # rejected without the status
                "status = 'rejected', credit_note_status = 'issued'",  # the status without the note's state
                "status = 'rejected', credit_note_status = 'rejected', amount = 1.00",
                "credit_note_status = 'final', amount = 1.00"):
        _set(r.pk, sql, ok=False)
    _set(r.pk, "status = 'rejected', credit_note_status = 'rejected', modified_at = now()")  # from issued
    assert _row(r.pk).status == "rejected"


@pg_only
@pytest.mark.django_db
def test_f3_a_final_note_is_immutable_and_never_becomes_rejected(w):
    c, r = _approved(w)
    _set(r.pk, "credit_note_status = 'final'")
    for sql in ("status = 'rejected', credit_note_status = 'rejected'", "credit_note_status = 'issued'",
                "credit_note_status = 'rejected'", "amount = 1.00", "credit_note_ref = 'OTHER'",
                "status = 'discarded'", "approval_token = 'x'", "modified_at = '2030-01-01'"):
        _set(r.pk, sql, ok=False)
    assert (_row(r.pk).status, _row(r.pk).credit_note_status) == ("approved", "final")


@pg_only
@pytest.mark.django_db
def test_f3_a_rejected_rebate_is_approved_again_only_with_a_new_issued_or_final_note(w):
    c, r = _approved(w)
    old_ref = r.credit_note_ref
    _set(r.pk, "status = 'rejected', credit_note_status = 'rejected'")
    again = "status = 'approved', approved_at = now(), installment_cuts = '[]'"
    for sql in (f"{again}, credit_note_status = 'issued'",  # the same note
                f"{again}, credit_note_status = 'final'",
                f"{again}, credit_note_status = 'rejected', credit_note_ref = 'NEW-1'",  # not a live note
                f"{again}, credit_note_status = '', credit_note_ref = 'NEW-1'",
                f"{again}, credit_note_status = 'issued', credit_note_ref = ''",
                f"{again}, credit_note_status = 'issued', credit_note_ref = 'NEW-1', amount = 1.00",  # amount locked
                f"{again}, credit_note_status = 'issued', credit_note_ref = 'NEW-1', approval_token = 'x'",
                f"{again}, credit_note_status = 'issued', credit_note_ref = 'NEW-1', approved_by_id = NULL",
                "status = 'rejected', amount = 1.00", "status = 'rejected', credit_note_ref = 'NEW-1'",
                "approval_token = 'x', modified_at = '2030-01-01'", "status = 'draft'"):
        _set(r.pk, sql, ok=False)
    _set(r.pk, "approval_token = 'x'")  # the approval in progress writes its one-time token
    _set(r.pk, f"{again}, credit_note_status = 'issued', credit_note_ref = 'NEW-1', approval_token = ''")
    assert (_row(r.pk).status, _row(r.pk).credit_note_ref) == ("approved", "NEW-1") and old_ref != "NEW-1"
    _set(r.pk, "status = 'rejected', credit_note_status = 'rejected'")
    _set(r.pk, f"{again}, credit_note_status = 'final', credit_note_ref = 'NEW-2'")  # reported at once: final
    assert _row(r.pk).credit_note_status == "final"


@pg_only
@pytest.mark.django_db
def test_f3_a_rejected_rebate_is_discarded_and_a_discarded_one_never_changes(w):
    c, r = _approved(w)
    _set(r.pk, "status = 'rejected', credit_note_status = 'rejected'")
    _set(r.pk, "status = 'discarded', amount = 1.00", ok=False)
    _set(r.pk, "status = 'discarded', modified_at = now()")
    for sql in ("status = 'rejected'", "status = 'approved'", "status = 'draft'", "amount = 1.00",
                "approval_token = 'x'", "modified_at = '2030-01-01'"):
        _set(r.pk, sql, ok=False)


@pg_only
@pytest.mark.django_db
def test_f3_the_services_pass_the_trigger_reject_then_approve_again_then_final(w):
    c, r = _approved(w)
    w.invoicing.rebate_status = "rejected"
    services._refresh_deferred_sales(w.alm)
    w.invoicing.rebate_status = "issued"
    r = services.approve_deferred_sale_rebate(_row(r.pk), user=w.mp)  # token written then cleared under the trigger
    assert (r.status, r.approval_token) == ("approved", "")
    w.invoicing.rebate_status = "final"
    services._refresh_deferred_sales(w.alm)
    assert _row(r.pk).credit_note_status == "final"


@pg_only
@pytest.mark.django_db(transaction=True)
def test_f2_the_payroll_alerts_command_reads_a_rejection_inside_a_transaction(seeded_db, w):
    c = sold(w)
    r = services.approve_deferred_sale_rebate(rebate(w, c, amount="120.00"), user=w.mp)
    cut = planned(c)
    w.invoicing.rebate_status = "rejected"
    assert not connection.in_atomic_block  # as the daily job: autocommit, no request transaction
    call_command("payroll_alerts")  # before the fix: TransactionManagementError (select_for_update in autocommit)
    assert _row(r.pk).status == "rejected" and planned(c) != cut
    assert sum(a for _, a in planned(c)) == sum(a for _, a in cut) + D("138.00")  # 120.00 + VAT 18.00
    assert PayrollAlert.objects.filter(kind="bai_ajel_rebate", recipient=w.mp).count() == 1
    call_command("payroll_alerts")  # idempotent
    assert PayrollAlert.objects.filter(kind="bai_ajel_rebate", recipient=w.mp).count() == 1
