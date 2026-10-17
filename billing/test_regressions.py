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
