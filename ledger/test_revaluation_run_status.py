"""Revaluation run status (ledger-fx R44, R45; verification review probe E).

``RevaluationRun.status = "posted"`` used to be saved without
post_revaluation. A run is created as a draft; draft -> posted only through
post_revaluation, draft -> discarded only through discard_revaluation, each
with who and when.
"""
from datetime import date, timedelta

import pytest
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from ledger import services
from ledger.models import FiscalPeriod, LedgerError, RevaluationRun
from ledger.test_posting_hardening import w  # noqa: F401

pytestmark = pytest.mark.django_db


def _run(w):
    run = RevaluationRun(entity=w.alm, period=FiscalPeriod.objects.get(entity=w.alm, name="2027-01"),
                         rate_date=date(2027, 1, 31), run_by=w.clerk)
    run.save()
    return run


def test_probe_e_run_status_changes_only_through_services(w):
    run = _run(w)
    for target in ("posted", "discarded"):
        run.status = target
        with pytest.raises(LedgerError):
            run.save()
    run = RevaluationRun.objects.get(pk=run.pk)
    run.status, run.posted_by, run.posted_at = "posted", w.cfo, timezone.now()  # with plausible fields too
    with pytest.raises(LedgerError):
        run.save()
    run = RevaluationRun.objects.get(pk=run.pk)
    run.discarded_by, run.discarded_at = w.clerk, timezone.now()  # a draft never carries a discarder
    with pytest.raises(LedgerError):
        run.save()
    assert RevaluationRun.objects.get(pk=run.pk).status == "draft"
    with pytest.raises(LedgerError):
        RevaluationRun.objects.filter(pk=run.pk).update(status="posted")


def test_runs_are_created_as_drafts(w):
    period = FiscalPeriod.objects.get(entity=w.alm, name="2027-02")
    for status_ in ("posted", "discarded"):
        with pytest.raises(LedgerError):
            RevaluationRun(entity=w.alm, period=period, rate_date=date(2027, 2, 28), run_by=w.clerk, status=status_).save()
        with pytest.raises(LedgerError):
            RevaluationRun.objects.bulk_create(
                [RevaluationRun(entity=w.alm, period=period, rate_date=date(2027, 2, 28), run_by=w.clerk, status=status_)]
            )
    assert not RevaluationRun.objects.exists()


def test_discard_service_records_who_and_when(w):
    run = _run(w)
    discarded = services.discard_revaluation(run, user=w.clerk)
    assert discarded.status == "discarded" and discarded.discarded_by == w.clerk and discarded.discarded_at is not None
    assert timezone.now() - discarded.discarded_at < timedelta(seconds=10)


# --- the same rules in the database (PostgreSQL only; spec-writer follow-up L1) ----------------------

def _raw(sql, params=()):
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql, params)


postgres_only = pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL trigger")


@postgres_only
def test_l1_database_refuses_run_status_changes_outside_the_services(w):
    run = _run(w)
    for sql in (
        "UPDATE ledger_revaluationrun SET status = 'posted' WHERE id = %s",  # no poster, time or reversal
        "UPDATE ledger_revaluationrun SET status = 'posted', posted_by_id = run_by_id, posted_at = now() WHERE id = %s",
        "UPDATE ledger_revaluationrun SET status = 'discarded' WHERE id = %s",  # no discarder
        "UPDATE ledger_revaluationrun SET discarded_by_id = run_by_id, discarded_at = now() WHERE id = %s",  # a draft
        "UPDATE ledger_revaluationrun SET status = 'discarded', discarded_by_id = run_by_id, discarded_at = now(), "
        "rate_date = '2027-01-30' WHERE id = %s",  # also changes another column
        "DELETE FROM ledger_revaluationrun WHERE id = %s",
    ):
        with pytest.raises(DatabaseError):
            _raw(sql, [run.pk])
    assert RevaluationRun.objects.get(pk=run.pk).status == "draft"
    with pytest.raises(DatabaseError, match="created as drafts"):
        _raw("INSERT INTO ledger_revaluationrun (entity_id, period_id, rate_date, status, run_by_id, run_at, "
             "posted_by_id, posted_at) SELECT entity_id, period_id, rate_date, 'posted', run_by_id, now(), run_by_id, "
             "now() FROM ledger_revaluationrun WHERE id = %s", [run.pk])
    services.discard_revaluation(run, user=w.clerk)  # the service's own transition passes the trigger
    with pytest.raises(DatabaseError, match="never changes"):
        _raw("UPDATE ledger_revaluationrun SET status = 'draft', discarded_by_id = NULL, discarded_at = NULL "
             "WHERE id = %s", [run.pk])
