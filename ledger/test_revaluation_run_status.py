"""Revaluation run status (ledger-fx R44, R45; verification review probe E).

``RevaluationRun.status = "posted"`` used to be saved without
post_revaluation. A run is created as a draft; draft -> posted only through
post_revaluation, draft -> discarded only through discard_revaluation, each
with who and when.
"""
from datetime import date, timedelta

import pytest
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
