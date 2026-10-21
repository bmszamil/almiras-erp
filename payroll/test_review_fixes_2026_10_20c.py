"""Payroll review fixes of 2026-10-20, third round (payroll §10.9 items 17 to 22; invoicing R128 as amended), with
payroll's fake invoicing port (the real port and ledger balances: invoicing/test_deferred_sale_review3.py; the
PostgreSQL trigger and the alerts command in a transaction: payroll/test_pg_review_2026_10_20c.py). Anonymised
fixtures; the payroll clock is fixed; nothing external is called.

Finding 1: a rejection read after the employee's final settlement restores the amount as outstanding after exit.
Finding 2: the restore, status and alerts of a rejection commit together. Finding 4: the rebating marker needs the
one-time token on the rebate row, a valid grant under the authority matrix and four eyes. Finding 6: backfilled WHT
rows are flagged for review in the return. Item 7: WHT with a payment approved before payroll.0018, a zero-net
payslip and a KSA resident named by a payment."""
import importlib
from datetime import date
from types import SimpleNamespace

import pytest
from django.apps import apps as django_apps
from django.core.exceptions import PermissionDenied
from django.db import connection, models

from core import access
from payroll import approvals, invoicing_port, services
from payroll.conftest import D, approved, rate, saudi, slip
from payroll.models import (DeferredSaleInstallment, DeferredSaleRebate, FinalSettlement, PayrollAlert, PayrollError,
                            PayrollPayment)
from payroll.test_final_settlement import go_live, leaver, settle, terminate
from payroll.test_review_fixes_2026_10_20 import bilingual, category, remuneration
from payroll.test_review_fixes_2026_10_20b import _editor, approval_marker, planned, rebate, sold

backfill = importlib.import_module("payroll.migrations.0018_review_fixes_rebate_wht").backfill_wht_parts


def after_exit(c):
    return [(i.period, i.amount, i.carried_from_id is not None)
            for i in DeferredSaleInstallment.objects.filter(contract=c, status="outstanding_after_exit")
            .order_by("period", "pk")]


def settled_leaver_with_pending_rebate(w):
    """The reproduction: N1 (final-dues undertaking) has a pending rebate (60.00, gross 69.00 off the last instalment)
    when the final settlement recovers everything else and is approved."""
    n1 = leaver(w)
    c = sold(w, n1, first="2027-03", undertaking=True)
    t = terminate(w, n1)
    go_live(w)
    r = services.approve_deferred_sale_rebate(rebate(w, c, amount="60.00"), user=w.mp)
    fs = services.approve_final_settlement(FinalSettlement.objects.get(pk=settle(w, t).pk), user=w.c)
    assert planned(c) == [] and fs.status == "approved"
    return n1, c, r


# --- finding 1: a rejection read after the final settlement -----------------------------------------------------------

def test_f1_rejection_read_after_the_final_settlement_restores_the_amount_outstanding_after_exit(w):
    n1, c, r = settled_leaver_with_pending_rebate(w)
    last = DeferredSaleInstallment.objects.filter(contract=c, period="2028-02").get()
    assert (last.status, last.amount) == ("settled_in_final", D("57.50"))  # 126.50 less the 69.00 cut
    w.invoicing.rebate_status = "rejected"  # ZATCA rejects after the settlement was approved
    services._refresh_deferred_sales(w.alm)
    assert planned(c) == []  # never a planned instalment for someone who has left
    assert after_exit(c) == [("2028-02", D("69.00"), True)]
    restored = DeferredSaleInstallment.objects.get(contract=c, status="outstanding_after_exit")
    assert restored.carried_from_id == last.pk  # carried from the instalment the approval had reduced
    r = DeferredSaleRebate.objects.get(pk=r.pk)
    assert (r.status, r.credit_note_status) == ("rejected", "rejected")
    alerts = PayrollAlert.objects.filter(kind="bai_ajel_rebate")
    assert {a.recipient for a in alerts} >= {w.p, w.mp}
    text = alerts.first()
    assert "outstanding after exit on 1240" in text.text_en and "discard the rebate" in text.text_en
    assert "قائم بعد انتهاء الخدمة" in text.text_ar and "ألغِ الإبراء" in text.text_ar
    assert "were restored" not in text.text_en and r.credit_note_ref in text.text_en and c.number in text.text_ar


def test_f1_after_exit_the_rebate_is_not_approved_again_and_is_discarded_with_nothing_changed(w):
    n1, c, r = settled_leaver_with_pending_rebate(w)
    w.invoicing.rebate_status = "rejected"
    services._refresh_deferred_sales(w.alm)
    before = after_exit(c)
    with pytest.raises(PayrollError) as exc:  # no instalment is left for an ibra' to reduce
        services.approve_deferred_sale_rebate(DeferredSaleRebate.objects.get(pk=r.pk), user=w.mp)
    assert exc.value.code == "rebate_after_exit" and bilingual(exc.value)
    assert len(w.invoicing.rebates) == 1 and after_exit(c) == before  # no second credit note, nothing cut
    services.discard_deferred_sale_rebate(DeferredSaleRebate.objects.get(pk=r.pk), user=w.p)
    assert DeferredSaleRebate.objects.get(pk=r.pk).status == "discarded" and after_exit(c) == before
    services._refresh_deferred_sales(w.alm)  # read again: nothing more restored
    assert after_exit(c) == before


def test_f1_before_the_final_settlement_is_approved_a_rejection_still_restores_planned_instalments(w):
    n1 = leaver(w)
    c = sold(w, n1, first="2027-03", undertaking=True)
    t = terminate(w, n1)
    go_live(w)
    full = planned(c)
    services.approve_deferred_sale_rebate(rebate(w, c, amount="60.00"), user=w.mp)
    settle(w, t)  # submitted, not approved
    w.invoicing.rebate_status = "rejected"
    services._refresh_deferred_sales(w.alm)
    assert planned(c) == full and after_exit(c) == []


# --- finding 2: restore, status and alerts commit together -----------------------------------------------------------

def test_f2_a_failing_alert_rolls_back_the_restore_and_the_status_of_that_rebate(w, monkeypatch):
    c = sold(w)
    r = services.approve_deferred_sale_rebate(rebate(w, c, amount="120.00"), user=w.mp)
    cut = planned(c)
    w.invoicing.rebate_status = "rejected"

    def broken(*args, **kwargs):
        raise RuntimeError("alert store down (test)")

    monkeypatch.setattr(services, "_send_alerts", broken)
    with pytest.raises(RuntimeError):
        services._refresh_rebates(w.alm)
    assert planned(c) == cut and DeferredSaleRebate.objects.get(pk=r.pk).status == "approved"
    monkeypatch.undo()
    assert [x.pk for x in services._refresh_rebates(w.alm)] == [r.pk]
    assert DeferredSaleRebate.objects.get(pk=r.pk).status == "rejected" and len(planned(c)) == 12


def test_f2_the_alerts_job_reads_the_rejection_per_entity(w):
    c = sold(w)
    r = services.approve_deferred_sale_rebate(rebate(w, c, amount="120.00"), user=w.mp)
    w.invoicing.rebate_status = "rejected"
    services.send_alerts(today=date(2027, 3, 20))
    assert DeferredSaleRebate.objects.get(pk=r.pk).status == "rejected"
    assert PayrollAlert.objects.filter(kind="bai_ajel_rebate", recipient=w.mp).exists()


# --- finding 4: the marker is bound to the approval -------------------------------------------------------------------

def test_f4_a_forged_marker_issues_nothing(w):
    """The reproduction (x5): any code could enter the marker with an object that has the rebate's pk."""
    c = sold(w)
    r = rebate(w, c)
    ref = f"payroll.deferredsalerebate:{r.pk}"
    with pytest.raises(TypeError):  # the token and grant are required
        with invoicing_port.rebating(SimpleNamespace(pk=r.pk), w.mp):
            pass
    good = access.require(w.mp, w.alm, "payroll.approve_deferred_sale", document=c)
    for token in ("", "x" * 64):  # no token, or one that is not on the row
        with invoicing_port.rebating(SimpleNamespace(pk=r.pk), w.mp, token=token, grant=good):
            assert invoicing_port.rebate_problems(c, source_ref=ref, amount=r.amount, approver=w.mp)
    with pytest.raises(PayrollError) as exc:
        with invoicing_port.rebating(SimpleNamespace(pk=r.pk), w.mp, token="x" * 64, grant=good):
            w.invoicing.create_bai_ajel_rebate(c, user=w.mp, source_ref=ref, amount=r.amount, reason_ar="x")
    assert exc.value.code == "rebate_not_from_payroll" and not w.invoicing.rebates
    assert DeferredSaleRebate.objects.get(pk=r.pk).approval_token == ""


def test_f4_the_token_must_match_the_row_and_is_cleared_by_the_approval(w):
    c = sold(w)
    r = rebate(w, c)
    ref = f"payroll.deferredsalerebate:{r.pk}"
    with approval_marker(w, r, w.mp):
        assert invoicing_port.rebate_problems(c, source_ref=ref, amount=r.amount, approver=w.mp) == []
        row = DeferredSaleRebate.objects.get(pk=r.pk)
        row.approval_token = "u" * 64  # another approval took over the row
        services._save(row, update_fields=["approval_token"])
        assert invoicing_port.rebate_problems(c, source_ref=ref, amount=r.amount, approver=w.mp)
    r = services.approve_deferred_sale_rebate(DeferredSaleRebate.objects.get(pk=r.pk), user=w.mp)
    assert r.status == "approved" and DeferredSaleRebate.objects.get(pk=r.pk).approval_token == ""


def test_f4_the_grant_must_be_the_approvers_for_this_contract_and_valid(w):
    c = sold(w)
    other = sold(w, first="2027-05")
    r = rebate(w, c)
    ref = f"payroll.deferredsalerebate:{r.pk}"
    elsewhere = access.require(w.mp, w.alm, "payroll.approve_deferred_sale", document=other)
    for grant in (None, SimpleNamespace(user=w.mp), elsewhere):  # none, forged, another contract's
        with approval_marker(w, r, w.mp, grant=grant):
            assert invoicing_port.rebate_problems(c, source_ref=ref, amount=r.amount, approver=w.mp)
    used = access.require(w.mp, w.alm, "payroll.approve_deferred_sale", document=c)
    access.use_grant(used, target=c)
    with approval_marker(w, r, w.mp, grant=used):  # single use
        assert invoicing_port.rebate_problems(c, source_ref=ref, amount=r.amount, approver=w.mp)


def test_f4_the_authority_matrix_is_checked_again(w, monkeypatch):
    c = sold(w)
    r = rebate(w, c)
    ref = f"payroll.deferredsalerebate:{r.pk}"
    with approval_marker(w, r, w.mp):
        monkeypatch.setitem(approvals.SEED_ROWS, "payroll.deferred_sale", ("nobody",))  # test: matrix without MP
        assert invoicing_port.rebate_problems(c, source_ref=ref, amount=r.amount, approver=w.mp)
        monkeypatch.undo()
        assert invoicing_port.rebate_problems(c, source_ref=ref, amount=r.amount, approver=w.mp) == []


def test_f4_four_eyes_against_the_last_modifier_as_the_approval(w):
    c = sold(w)
    r = rebate(w, c)
    ref = f"payroll.deferredsalerebate:{r.pk}"
    row = DeferredSaleRebate.objects.get(pk=r.pk)
    row.modified_by = w.mp  # test data: the sale approver last edited the draft
    services._save(row)
    with approval_marker(w, row, w.mp):
        assert invoicing_port.rebate_problems(c, source_ref=ref, amount=r.amount, approver=w.mp)
    with pytest.raises(PermissionDenied):
        services.approve_deferred_sale_rebate(row, user=w.mp)
    assert not w.invoicing.rebates


# --- finding 6 and item 7: WHT ----------------------------------------------------------------------------------------

def _set(p, **fields):
    """Test data: an approved payment as it was stored before payroll.0017 / 0018 (approved rows are locked on
    PostgreSQL, so the lock is lifted for this one test update, as payroll.0018 itself does)."""
    with connection.cursor() as cursor:
        if connection.vendor == "postgresql":
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            cursor.execute("ALTER TABLE payroll_payrollpayment DISABLE TRIGGER payroll_payment_lock")
        models.QuerySet.update(PayrollPayment.objects.filter(pk=p.pk), **fields)
        if connection.vendor == "postgresql":
            cursor.execute("ALTER TABLE payroll_payrollpayment ENABLE TRIGGER payroll_payment_lock")
    p.refresh_from_db()
    return p


def _resident(emp):
    from hr.models import Employee

    # Test data: hr refuses a KSA residence for remuneration staff (R52); payroll checks it again (B3).
    models.QuerySet.update(Employee.objects.filter(pk=emp.pk), country_of_residence="SA")


def _usd_run(w, *emps):
    rate("USD", "SAR", date(2027, 3, 31), "3.7500")
    rate("USD", "SAR", date(2027, 4, 8), "3.7500")
    return approved(w, payroll_group="out_of_country")


def _pay(w, run, amount, payslips=None, currency="USD", wht=True):
    p = services.record_payment(w.alm, user=w.p, kind="net_salaries", run=run, payment_date=date(2027, 4, 8),
                                amount=amount, currency=currency, wht_category=_category() if wht else None,
                                payslips=payslips)
    return services.approve_payment(services.submit_payment(p, user=w.p), user=w.c)


def _category():
    from invoicing.models import WhtCategory

    return WhtCategory.objects.filter(code="test-remuneration").first() or category()


def _payroll_rows(w, month=date(2027, 4, 1)):
    from invoicing import services as inv

    data = inv.wht_return_data(w.alm, month, user=w.a)
    return data, [r for r in data["rows"] if r.get("source") == "payroll"]


def test_f6_backfilled_rows_are_flagged_for_review_and_name_a_resident_payee(w):
    r1, r2 = remuneration(w), remuneration(w)
    _resident(r2)
    run = _usd_run(w)
    p = _pay(w, run, "10000.00")
    assert [x["employee_code"] for x in p.wht_parts] == [r1.employee_code]  # r2 resident: no WHT (B3)
    booked = (p.wht_base_sar, p.wht_amount_sar)
    _set(p, wht_parts=[])  # before payroll.0017
    backfill(django_apps, _editor())
    p.refresh_from_db()
    # The backfill splits the booked WHT by net over both remuneration payees, the resident included.
    assert {x["employee_code"] for x in p.wht_parts} == {r1.employee_code, r2.employee_code}
    data, rows = _payroll_rows(w)
    assert data["difference"] == 0  # the rows still tie to 2220 as booked
    assert (sum(r["base_sar"] for r in rows), sum(r["wht_sar"] for r in rows)) == booked
    assert all(r["review_required"] and r["backfilled"] and r["backfilled_payments"] == [p.pk] for r in rows)
    by = {r["vendor"]: r for r in rows}
    assert by[r2.employee_code]["review_reasons"] == ["resident"]
    assert "resident in Saudi Arabia" in by[r2.employee_code]["review_en"]
    assert "مقيمًا في المملكة" in by[r2.employee_code]["review_ar"]
    assert by[r1.employee_code]["review_reasons"] == []
    assert "payroll.0018" in by[r1.employee_code]["review_en"] and "payroll.0018" in by[r1.employee_code]["review_ar"]
    assert "resident" not in by[r1.employee_code]["review_en"]


def test_f6_a_backfilled_part_naming_a_salaried_payee_is_flagged_and_kept_apart_from_live_rows(w):
    s1 = saudi(w)
    run = approved(w)  # in-country: no WHT on salaries
    p = _pay(w, run, str(slip(run, s1).net), currency=None, wht=False)
    part = {"employee_id": s1.pk, "employee_code": s1.employee_code, "country": "", "base_sar": "100.00",
            "wht_sar": "5.00", "backfilled": True}
    # Test data: a pre-0017 WHT payment on this run, as payroll.0018 rebuilt it (fallback: all payslips by net).
    _set(p, wht_category=_category(), wht_parts=[part], wht_amount=D("5.00"), wht_amount_sar=D("5.00"),
         wht_base_sar=D("100.00"), payment_date=date(2027, 4, 8))
    _, rows = _payroll_rows(w)
    assert len(rows) == 1 and rows[0]["review_reasons"] == ["not_remuneration"]
    assert "not paid as remuneration staff" in rows[0]["review_en"] and "ليس من موظفي المكافآت" in rows[0]["review_ar"]


def test_f6_live_rows_carry_no_review_flag(w):
    remuneration(w)
    run = _usd_run(w)
    _pay(w, run, "5000.00")
    _, rows = _payroll_rows(w)
    assert rows and not any(r.get("review_required") for r in rows)


def test_item7_a_payment_approved_before_0018_counts_pro_rata_in_what_is_paid_per_payslip(w):
    r1, r2 = remuneration(w), remuneration(w)
    run = _usd_run(w)
    s1, s2 = slip(run, r1), slip(run, r2)
    p1 = _pay(w, run, "5000.00", payslips=[s1])
    _set(p1, payslip_parts=[], payslip_ids=[])  # before payroll.0018: no parts
    slips, paid, covered = services._payslip_paid(run, "USD")
    assert (paid[s1.pk], paid[s2.pk]) == (D("2500"), D("2500")) and covered == {s1.pk, s2.pk}  # pro rata to net
    with pytest.raises(PayrollError) as exc:  # on that reading s1 has 2,500.00 open, not 5,000.00
        _pay(w, run, "5000.00", payslips=[s1])
    assert exc.value.code == "overpayment"
    p2 = _pay(w, run, "5000.00")  # the rest: half each
    assert sorted(x["amount"] for x in p2.payslip_parts) == ["2500.00", "2500.00"]
    assert sorted(x["base_sar"] for x in p2.wht_parts) == ["9375.00", "9375.00"]  # 2,500 x 3.75 each, own gross
    with pytest.raises(PayrollError) as exc:
        _pay(w, run, "0.01")
    assert exc.value.code == "overpayment"


def test_item7_a_zero_net_payslip_carries_no_wht_taken_from_another_payee(w):
    r1, r2 = remuneration(w), remuneration(w)
    inp = services.create_input(r1, user=w.p, kind="one_off_deduction", period="2027-03", amount="5000.00",
                                deduction_basis="written_consent", consent_ref="SIGNED-R1", reason_ar="استقطاع")
    services.approve_input(inp, user=w.p2)
    run = _usd_run(w)
    s1, s2 = slip(run, r1), slip(run, r2)
    assert (s1.net_txn, s1.gross_txn, s2.net_txn) == (D("0.000"), D("5000.000"), D("5000.000"))
    p = _pay(w, run, "5000.00")
    assert {x["payslip_id"]: x["amount"] for x in p.payslip_parts} == {s1.pk: "0.00", s2.pk: "5000.00"}
    # r2's own WHT only (5% of 5,000.00): r1 is paid nothing by this payment, so nothing is withheld for r1 from it
    assert p.wht_amount == D("250.000") and [x["employee_code"] for x in p.wht_parts] == [r2.employee_code]
    warning = " ".join(p.warnings or [])
    assert r1.employee_code in warning and "no net pay" in warning and "لا صافي" in warning


def test_item7_a_ksa_resident_named_by_a_payment_bears_no_wht(w):
    r1, r2 = remuneration(w), remuneration(w)
    _resident(r2)
    run = _usd_run(w)
    p = _pay(w, run, "5000.00", payslips=[slip(run, r2)])
    assert p.payslip_parts == [{"payslip_id": slip(run, r2).pk, "amount": "5000.00"}]
    assert (p.wht_amount, p.wht_amount_sar, p.wht_base_sar, p.wht_parts) == (D("0.000"), D("0.00"), D("0.00"), [])
    assert any(r2.employee_code in x and "resident in Saudi Arabia" in x for x in p.warnings)
    p2 = _pay(w, run, "5000.00", payslips=[slip(run, r1)])
    assert [x["employee_code"] for x in p2.wht_parts] == [r1.employee_code] and p2.wht_amount == D("250.000")
    _, rows = _payroll_rows(w)
    assert [r["vendor"] for r in rows] == [r1.employee_code]
