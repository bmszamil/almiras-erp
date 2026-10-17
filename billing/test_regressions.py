"""Regression tests for the billing code-review findings (reviewer probes P1 to P7 on 9d6189e, and the fixes
that followed). Each test names the finding and the rule it protects."""
from datetime import date
from decimal import Decimal

import pytest

from billing import services
from billing.conftest import approve_note, approved, clear, period, run, version
from billing.models import BillingAdjustment, BillingError, BillingPeriod
from invoicing.models import SalesDocument

D = Decimal


# --- P1 / finding 1: R42, OQ-24 --------------------------------------------------------------------------------

def test_p1_due_days_only_amendment_makes_no_correction_and_full_months_stay_whole(w):
    c = approved(w, lines=(("rent_commercial", "1000.05"),), start=date(2027, 4, 1))
    run(w, date(2027, 4, 1))
    clear(w, period(c, date(2027, 4, 1)).sales_document)
    v = services.approve_version(version(w, c, effective=date(2027, 4, 16), due_days=30), user=w.c)
    assert v.adjustments == [] and not BillingAdjustment.objects.filter(contract=c).exists()
    services.approve_version(version(w, c, effective=date(2027, 6, 16), due_days=45), user=w.c)
    for m in (5, 6):
        run(w, date(2027, m, 1))
    june = period(c, date(2027, 6, 1))
    assert june.invoice_lines[0]["net"] == "1000.05" and june.sales_document.total_net == D("1000.05")
    assert services.recompute_corrections(c, user=w.s) == []


# --- P2 / finding 2: access R29 (employee_ref masked for billing.view holders) ------------------------------------

def test_p2_viewer_never_reads_employee_ref_in_invoice_lines(w):
    from types import SimpleNamespace

    from django.contrib import admin

    from billing.test_secondments import secondment_contract

    c, a = secondment_contract(w, amount="31000.00")
    run(w, date(2027, 2, 1))
    p = services.periods(c, user=w.v).get(period_start=date(2027, 1, 1))  # w.v: viewer (billing.view only)
    assert p.status == "generated"
    assert all("employee_ref" not in row for row in p.invoice_lines)
    assert "EMP-T-0001" not in str(p.invoice_lines)
    assert p.invoice_lines[0]["secondment"] == a.pk  # invoicing resolves the person through its own rights
    assert services.view_secondment(a, user=w.v)["employee_ref"] == "••••"
    model_admin = admin.site._registry[BillingPeriod]
    request = SimpleNamespace(user=w.v)
    assert list(model_admin.get_queryset(request).filter(pk=p.pk)) == [p]
    assert "invoice_lines" in model_admin.exclude
    assert "invoice_lines" not in model_admin.get_readonly_fields(request, p)
    assert "invoice_lines" not in model_admin.get_fields(request, p)
    # corrections of a secondment line carry no employee_ref either
    w.clock.set(date(2027, 2, 3))
    services.end_secondment(w.alm, a, date(2027, 1, 20), w.s, source="manual")
    (adj,) = BillingAdjustment.objects.filter(contract=c)
    assert "EMP-T-0001" not in str(adj.lines) and all("secondment" not in row for row in adj.lines)


# --- P7 / finding 3: R46c (supersede locks the note and never drops an issued one) ---------------------------------

def test_p7_note_issued_after_the_last_read_back_is_kept_and_counted(w, monkeypatch):
    c = approved(w, start=date(2027, 3, 1))
    run(w, date(2027, 3, 1))
    march = period(c, date(2027, 3, 1))
    clear(w, march.sales_document)
    services.approve_version(version(w, c, effective=date(2027, 3, 1), amount="9000.00"), user=w.c)
    (adj,) = BillingAdjustment.objects.filter(contract=c)
    assert (adj.kind, adj.status) == ("credit", "drafted")
    # the race window: billing's read-back already ran when the CFO issued the note
    monkeypatch.setattr(services, "_sync_contract", lambda *a, **k: 0)
    approve_note(w, adj.document, day=date(2027, 3, 5))
    services.approve_version(version(w, c, effective=date(2027, 3, 1), amount="9500.00"), user=w.c)
    adj.refresh_from_db()
    assert adj.status == "final" and adj.document_id is not None  # not superseded, its note not dropped
    assert adj.document_id not in w.fake.discarded
    rows = list(BillingAdjustment.objects.filter(contract=c).order_by("pk").values_list("kind", "status"))
    assert rows == [("credit", "final"), ("debit", "drafted")]  # no second credit for the same days
    debit = BillingAdjustment.objects.filter(contract=c).order_by("pk").last()
    assert sum(D(x["delta"]) for x in debit.lines) == D("500.00")  # 9,500 required − (10,000 − 1,000) billed
    notes = SalesDocument.objects.filter(original_document=march.sales_document)
    assert sorted(notes.values_list("doc_type", "status")) == [("credit_note", "final"), ("debit_note", "draft")]


# --- P3 / finding 4: R33 (an unexpected error fails one period, the run goes on) -------------------------------

def test_p3_unexpected_error_in_one_period_fails_it_and_the_run_continues(w, monkeypatch):
    c1 = approved(w)
    c2 = approved(w)
    original = w.fake.create_from_billing
    calls = {"n": 0}

    def boom(p, user):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("unexpected (test)")
        return original(p, user)

    monkeypatch.setattr(w.fake, "create_from_billing", boom)
    r = run(w, date(2027, 1, 1))
    assert (r.status, r.generated_count, r.failed_count) == ("completed_with_errors", 1, 1)
    statuses = {c.pk: period(c, date(2027, 1, 1)) for c in (c1, c2)}
    failed = [p for p in statuses.values() if p.status == "failed"]
    assert len(failed) == 1 and sorted(p.status for p in statuses.values()) == ["failed", "generated"]
    p = failed[0]
    assert (p.error_code, p.sales_document, p.invoice_lines) == ("unexpected", None, None)  # rolled back
    assert "RuntimeError" in p.failure_reason_en and "غير متوقع" in p.failure_reason_ar
    item = r.items.get(billing_period=p)
    assert (item.outcome, item.error_code) == ("failed", "unexpected")
    nxt = run(w, date(2027, 1, 2))  # retried automatically
    assert nxt.generated_count == 1 and period(BillingPeriod.objects.get(pk=p.pk).contract,
                                               date(2027, 1, 1)).status == "generated"


# --- P4 / finding 9: R57, R58 (hr_event only for linked assignments) -----------------------------------------------

def test_p4_hr_event_refused_for_an_unlinked_assignment(w):
    from core.access.models import AccessLog

    from billing.test_secondments import secondment_contract

    c, a = secondment_contract(w)
    run(w, date(2027, 2, 1))
    with pytest.raises(BillingError) as exc:  # w.h holds hr.end_assignment in ALM, but nothing is linked
        services.end_secondment(w.alm, a, date(2027, 1, 20), w.h, source="hr_event")
    assert exc.value.code == "not_linked" and "الموارد البشرية" in str(exc.value)
    a.refresh_from_db()
    assert (a.end_date, a.end_source, a.last_hr_end_date) == (None, "", None)
    assert not BillingAdjustment.objects.filter(contract=c).exists()
    assert AccessLog.objects.filter(event="permission_denied", actor=w.h,
                                    details__reason="not_linked_to_hr").exists()
    services.end_secondment(w.alm, a, date(2027, 1, 20), w.s, source="manual")  # D6: accounting staff record it
    a.refresh_from_db()
    assert (a.end_date, a.end_source) == (date(2027, 1, 20), "manual")


def test_r57_end_during_a_run_is_refused_as_retryable_and_changes_nothing(w, monkeypatch):
    """A run in progress holds the entity's billing lock (the real try-lock is tested in test_db_guards AT-27): the
    end raises a retryable ``run_in_progress`` and billing is unchanged (R41, R57)."""
    from billing.test_secondments import secondment_contract

    c, a = secondment_contract(w)
    run(w, date(2027, 2, 1))

    def busy(entity):
        raise services.error(*services._msg("run_in_progress", entity=entity.code), code="run_in_progress",
                             retryable=True)

    monkeypatch.setattr(services, "_entity_lock_now", busy)
    with pytest.raises(BillingError) as exc:
        services.end_secondment(w.alm, a, date(2027, 1, 20), w.s, source="manual")
    assert exc.value.code == "run_in_progress" and exc.value.retryable
    a.refresh_from_db()
    assert a.end_date is None and not BillingAdjustment.objects.filter(contract=c).exists()
