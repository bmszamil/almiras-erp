"""Payroll re-review of the final settlement (2026-10-20), items 1 to 14; ported from the reviewer's probes
test_zz_reviewprobe.py (P1 to P11). Item 15 (PostgreSQL races) is in ``payroll/test_pg_concurrency.py``.

Every date is fixed (the hr clock 2027-06-01, the payroll clock 2027-03-20, conftest); nothing reads the real date."""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from django.contrib import admin
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.test import RequestFactory

from hr import services as hr
from hr.models import FinalSettlementInput, Termination
from ledger import services as ledger
from ledger.models import FiscalPeriod, LedgerError
from payroll import services
from payroll.conftest import D, approved, asset_bill, saudi, slip
from payroll.models import (DeferredSaleContract, EmployeeLoan, EosbProvisionEntry, FinalSettlement,
                            GosiCatchUpNotice, LoanInstallment, PayrollError, PayrollPayment, PayrollSettings)
from payroll.test_final_settlement import LWD, go_live, leaver, lines, opening_provision, settle, terminate
from payroll.test_review_misc import terms


def _approve(w, fs):
    return services.approve_final_settlement(fs, user=w.c)


def _pay_key(rows):
    """What the booked rows of a period add up to as contract pay (the netting key of R14 / review 2)."""
    total = D("0")
    for r in rows:
        k = services._net_key(r.line_type, r.component_id, r.source_ref)
        if k and k[0][0] == "pay":
            total += k[1] * r.amount
    return total


# --- 1. approved settlement lines are booked; runs leave a settled employee out (P1) -------------------------------

def test_rr1_p1_settlement_lines_are_booked_and_an_off_cycle_run_leaves_the_settled_employee_out(w):
    saudi(w)
    s7 = leaver(w, cohort="")  # no GOSI cohort: refused in February
    feb = services.calculate_run(services.create_run(w.alm, user=w.p, period="2027-02"), user=w.p)
    feb = services.approve_run(services.submit_run(feb, user=w.p, exclude_refused=True), user=w.h)
    assert not feb.payslips.filter(employee=s7, refused=False).exists()
    hr.update_employee(s7, user=w.h, gosi_cohort="legacy_m33")
    fs = settle(w, terminate(w, s7))
    retro = sum(l.amount for l in fs.lines.filter(origin_period="2027-02", line_type="retro"))
    assert retro == D("8000.00")  # February is paid by the settlement (earlier months re-examined)
    assert services._booked_lines(s7, "2027-02", services.PERIOD_TYPES) == []  # not approved yet: nothing booked
    fs = _approve(w, fs)
    booked = services._booked_lines(s7, "2027-02", services.PERIOD_TYPES)
    assert booked and all(getattr(r, "settlement_id", None) == fs.pk for r in booked)
    assert _pay_key(booked) == D("8000.00")
    gosi = services._booked_lines(s7, "2027-02", services.GOSI_TYPES)
    assert {r.line_type for r in gosi} >= {"employee_gosi", "employer_gosi"}
    assert all(r.origin_period == "2027-02" for r in gosi)
    off = services.create_run(w.alm, user=w.p, period="2027-02", kind="off_cycle", reason_ar="تصحيح", employees=[s7])
    off = services.calculate_run(off, user=w.p)  # verification 1: the settled employee is left out, with a warning
    assert not off.payslips.filter(employee=s7).exists()
    assert any("approved final settlement" in x for x in off.warnings)


def test_rr1_salary_recovered_beyond_the_last_day_counts_as_less_pay_for_the_month(w):
    n1 = leaver(w)
    mar = approved(w, "2027-03")  # the whole of March paid before the termination
    assert slip(mar, n1).gross == D("8000.00")
    fs = _approve(w, settle(w, terminate(w, n1)))
    assert sum(l.amount for l in fs.lines.filter(source_ref__startswith="overpaid:")) == D("4000.00")
    assert _pay_key(services._booked_lines(n1, "2027-03", services.PERIOD_TYPES)) == D("4000.00")  # 15/30 of 8,000


# --- 2. run reversal after a settlement (P10) ----------------------------------------------------------------

def test_rr2_p10_run_reversal_refused_once_a_settlement_netted_against_it(w):
    n1 = leaver(w)
    saudi(w)
    mar = approved(w, "2027-03")
    fs = settle(w, terminate(w, n1))  # submitted
    with pytest.raises(PayrollError) as exc:
        services.reverse_run(mar, user=w.c, reason="error")
    assert exc.value.code == "settlement_netted"
    _approve(w, fs)
    with pytest.raises(PayrollError) as exc:
        services.reverse_run(mar, user=w.c, reason="error")
    assert exc.value.code == "settlement_netted"
    assert services.get_run(w.c, mar.pk).status == "approved"


def test_rr2_a_calculated_settlement_goes_back_to_draft_when_the_run_is_reversed(w):
    n1 = leaver(w)
    mar = approved(w, "2027-03")
    fs = FinalSettlement.objects.get(termination=terminate(w, n1))
    fs = services.prepare_final_settlement(fs, user=w.p)
    assert fs.status == "calculated" and fs.lines.exists()
    assert services.reverse_run(mar, user=w.c, reason="error").status == "reversed"
    fs = FinalSettlement.objects.get(pk=fs.pk)
    assert fs.status == "draft" and not fs.lines.exists() and fs.stored_hash == ""
    with pytest.raises(PayrollError):
        services.submit_final_settlement(fs, user=w.p)
    fs = services.prepare_final_settlement(fs, user=w.p)  # recalculated without the reversed run
    assert lines(fs, "salary_to_last_day") == D("4000.00")


# --- 3. header tamper (P3) -------------------------------------------------------------------------------------

def test_rr3_p3_tampered_header_refuses_the_approval(w):
    n1 = leaver(w)
    opening_provision(w, n1, "15500.00")
    fs = FinalSettlement.objects.get(termination=terminate(w, n1))
    fs = services.prepare_final_settlement(fs, user=w.p)
    with connection.cursor() as c:  # calculated: not locked in the database, caught by the stored hash
        c.execute("UPDATE payroll_finalsettlement SET eosb_provision_balance = 0 WHERE id = %s", [fs.pk])
    fs = services.submit_final_settlement(FinalSettlement.objects.get(pk=fs.pk), user=w.p)
    with pytest.raises(PayrollError) as exc:
        _approve(w, fs)
    assert exc.value.code == "payslips_changed"
    assert sum(r.amount for r in EosbProvisionEntry.objects.filter(employee=n1)) == D("15500.00")
    assert FinalSettlement.objects.get(pk=fs.pk).status == "submitted"


def test_rr3_header_fields_are_in_the_stored_hash(w):
    n1 = leaver(w)
    fs = services.prepare_final_settlement(FinalSettlement.objects.get(termination=terminate(w, n1)), user=w.p)
    before = services._settlement_stored_hash(fs)
    for column, value in (("net", "1"), ("gross", "1"), ("total_deductions", "1"), ("eosb_award", "1"),
                          ("separate_settlement_amount", "1"), ("eosb_calculation_id", None)):
        with connection.cursor() as c:
            c.execute(f"SELECT {column} FROM payroll_finalsettlement WHERE id = %s", [fs.pk])
            old = c.fetchone()[0]
            c.execute(f"UPDATE payroll_finalsettlement SET {column} = %s WHERE id = %s", [value, fs.pk])
            assert services._settlement_stored_hash(fs) != before, column
            c.execute(f"UPDATE payroll_finalsettlement SET {column} = %s WHERE id = %s", [old, fs.pk])
    assert services._settlement_stored_hash(fs) == before


@pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL triggers")
def test_rr3_pg_header_locked_once_submitted_except_the_legal_moves(w):
    n1 = leaver(w)
    opening_provision(w, n1, "15500.00")
    t = terminate(w, n1)
    go_live(w)
    fs = settle(w, t)
    for sql in ("UPDATE payroll_finalsettlement SET eosb_provision_balance = 0 WHERE id = %s",
                "UPDATE payroll_finalsettlement SET net = 1 WHERE id = %s",
                "UPDATE payroll_finalsettlement SET settlement_input_id = NULL WHERE id = %s",
                "UPDATE payroll_finalsettlement SET status = 'approved' WHERE id = %s"):
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(sql, [fs.pk])
    fs = services.return_final_settlement(fs, user=w.c, reason_ar="مراجعة")  # legal move
    fs = services.submit_final_settlement(services.prepare_final_settlement(fs, user=w.p), user=w.p)
    fs = _approve(w, fs)  # legal move, then the journal entry once
    assert fs.status == "approved" and fs.journal_entry_id
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("UPDATE payroll_finalsettlement SET eosb_award = 0 WHERE id = %s", [fs.pk])


# --- 4. settlement GOSI in the GOSI totals (P2) ---------------------------------------------------------------

def test_rr4_p2_settlement_gosi_is_payable_reported_and_counted_by_the_reversal_guard(w):
    saudi(w)
    n1 = leaver(w)
    t = terminate(w, n1)  # before the run: the leaver is left out of March (R10)
    mar = approved(w, "2027-03")
    run_gosi = services._gosi_booked(w.alm, "2027-03")
    fs = _approve(w, settle(w, t))
    fs_gosi = lines(fs, "employee_gosi") + lines(fs, "employer_gosi")
    assert fs_gosi == D("780.24")
    assert services._gosi_booked(w.alm, "2027-03") == run_gosi + fs_gosi
    assert services._gosi_open(w.alm, "2027-03") == run_gosi + fs_gosi
    report = services.gosi_report(w.alm, "2027-03", user=w.c)
    assert report["total"] == run_gosi + fs_gosi
    row = next(r for r in report["rows"] if r["employee_code"] == n1.employee_code)
    assert row["employee"] == D("353.83") and row["employer"] == D("426.41") and row["gosi_wage"] == D("3629.03")
    p = services.record_payment(w.alm, user=w.p, kind="gosi", gosi_period="2027-03", payment_date=date(2027, 4, 10),
                                amount=str(fs_gosi))
    services.approve_payment(services.submit_payment(p, user=w.p), user=w.c)
    # The payment covers the settlement's contributions only: the run may still be reversed.
    assert services._gosi_booked(w.alm, "2027-03", exclude_run=mar) == fs_gosi
    assert services.reverse_run(mar, user=w.c, reason="error").status == "reversed"
    assert services._gosi_open(w.alm, "2027-03") == 0


# --- 5. catch-up notice of a settlement (P9) ------------------------------------------------------------------

def test_rr5_p9_gosi_share_recovered_by_the_settlement_has_its_notice(w):
    n = leaver(w, hire=date(2027, 1, 1), cohort="new_m273", gosi_first_coverage_date=date(2025, 1, 1))
    req = hr.request_leave(n, "UNPAID", user=w.h, start_date=date(2027, 2, 1), end_date=date(2027, 2, 28),
                           reason_ar="سبب")
    hr.approve_leave(req, user=w.h2)
    approved(w, "2027-02")  # a month of unpaid leave: the employee share is advanced (Dr 1230)
    fs = _approve(w, settle(w, terminate(w, n)))
    recovered = lines(fs, "gosi_recovery")
    assert recovered > 0
    notice = GosiCatchUpNotice.objects.get(employee=n)
    assert notice.final_settlement_id == fs.pk and notice.run_id is None
    assert notice.period_of_origin == "2027-02" and notice.amount == recovered
    assert "2027-02" in notice.text_en and notice.text_ar
    notice = services.record_catch_up_notification(notice, user=w.p, notified_on=date(2027, 3, 16), channel="letter")
    assert notice.channel == "letter"
    # The recovery counts for February (item 1): a later calculation sees the advanced share as recovered.
    assert any(getattr(r, "settlement_id", None) == fs.pk and r.line_type == "gosi_catch_up"
               for r in services._booked_lines(n, "2027-02", services.GOSI_TYPES))


# --- 6. earlier months closed before the settlement; no EOSB accrual after it (P5) ---------------------------

def test_rr6_p5_settlement_refused_while_an_earlier_month_has_no_approved_run(w):
    n1 = leaver(w, hire=date(2026, 1, 1))
    approved(w, "2027-01")  # payroll live from January
    fs = settle(w, terminate(w, n1))
    with pytest.raises(PayrollError) as exc:
        _approve(w, fs)
    assert exc.value.code == "settlement_runs_open" and "2027-02" in str(exc.value)
    approved(w, "2027-02")  # February accrues the provision for the leaver (settlement not approved yet)
    with pytest.raises(PayrollError) as exc:
        _approve(w, fs)
    assert exc.value.code == "recalculate"  # the provision balance changed
    fs = services.return_final_settlement(fs, user=w.c, reason_ar="أعد الاحتساب")
    fs = _approve(w, services.submit_final_settlement(services.prepare_final_settlement(fs, user=w.p), user=w.p))
    assert sum(r.amount for r in EosbProvisionEntry.objects.filter(employee=n1)) == 0
    # A run never accrues the provision again for a settled employee.
    s = services._settings_of(w.alm)
    ctx = services._settlement_ctx(fs, w.p, s)
    sl = services.Slip(employee=n1, basis="ksa_employee", nationality_class="saudi", cohort="legacy_m33",
                       currency=w.alm.base_currency)
    services._eosb(ctx, sl, n1, services._employment(n1, "2027-03", s))
    assert sl.eosb is None and not sl.lines and any("final settlement is approved" in x for x in sl.warnings)


def test_rr6_settlement_refused_while_a_run_is_submitted(w):
    n1 = leaver(w, hire=date(2026, 1, 1))
    approved(w, "2027-01")
    t = terminate(w, n1)
    feb = services.submit_run(services.calculate_run(services.create_run(w.alm, user=w.p, period="2027-02"),
                                                     user=w.p), user=w.p)
    fs = settle(w, t)
    with pytest.raises(PayrollError) as exc:
        _approve(w, fs)
    assert exc.value.code == "settlement_runs_open" and "2027-02 is not approved yet" in str(exc.value)
    services.approve_run(feb, user=w.h)
    with pytest.raises(PayrollError) as exc:  # February's EOSB accrual changed the provision balance
        _approve(w, fs)
    assert exc.value.code == "recalculate"


# --- 7. one purchase bill, one bai' ajel contract (P11) ---------------------------------------------------------

def test_rr7_p11_one_purchase_bill_backs_one_contract(w):
    a, b = saudi(w), saudi(w)
    bill = asset_bill(w, "30000.00")
    c1 = services.record_asset_acquisition(terms(w, a), user=w.p, purchase_bill=bill, possession_date=date(2027, 2, 16),
                                           possession_evidence_ref="DELIVERY-1")
    assert c1.status == "asset_acquired"
    c2 = terms(w, b)
    with pytest.raises(PayrollError) as exc:
        services.record_asset_acquisition(c2, user=w.p, purchase_bill=bill, possession_date=date(2027, 2, 16),
                                          possession_evidence_ref="DELIVERY-1")
    assert exc.value.code == "acquisition_invalid" and "another bai' ajel contract" in str(exc.value)
    c2 = DeferredSaleContract.objects.get(pk=c2.pk)
    c2.purchase_bill = bill
    c2._service_write = True
    with pytest.raises(IntegrityError), transaction.atomic():
        c2.save()  # the database refuses it too (payroll_sale_one_purchase_bill)


# --- 8. partial loan recovery (P4) -----------------------------------------------------------------------------

def test_rr8_p4_partial_loan_recovery_settles_only_what_was_recovered(w):
    n1 = leaver(w, hire=date(2027, 1, 1))
    loan = services.create_loan(n1, user=w.p, kind="loan", principal="18000.00", disbursement_date=date(2027, 2, 1),
                                purpose_ar="قرض حسن", installments=[(f"2027-{m:02d}", "3000.00") for m in range(7, 13)])
    services.approve_loan(services.submit_loan(loan, user=w.p), user=w.mp)
    t = terminate(w, n1)
    go_live(w)
    fs = _approve(w, settle(w, t))
    recovered = lines(fs, "loan_recovery")
    assert D("0") < recovered < D("18000.00") and recovered % 3000 != 0  # one installment is recovered in part
    rows = list(LoanInstallment.objects.filter(loan=loan).order_by("period", "pk"))
    settled = [i for i in rows if i.status == "settled_in_final"]
    left = [i for i in rows if i.status == "outstanding_after_exit"]
    assert settled and all(i.deducted_amount == i.amount > 0 for i in settled)
    assert sum(i.deducted_amount for i in settled) == recovered
    assert sum(i.amount for i in left) == D("18000.00") - recovered
    assert not [i for i in rows if i.status == "planned"]
    split = [i for i in left if i.carried_from_id]
    assert len(split) == 1 and split[0].carried_from.status == "settled_in_final"
    assert split[0].amount + split[0].carried_from.amount == D("3000.00")
    loan = EmployeeLoan.objects.get(pk=loan.pk)
    assert loan.status == "active" and services._loan_outstanding_of(loan) == D("18000.00") - recovered


# --- 9. notice pay rounded once (P7) -------------------------------------------------------------------------

def test_rr9_p7_notice_pay_is_wage_times_days_over_the_divisor_rounded_once(w):
    n1 = leaver(w)  # actual wage 8,000.00: daily 266.67 (rounded), 30 x 266.67 = 8,000.10
    t = terminate(w, n1, notice_given_on=LWD - timedelta(days=30))
    si = FinalSettlementInput.objects.get(termination=t, status="draft")
    assert si.notice_shortfall_days == 30 and si.daily_wage == D("266.67")
    assert si.notice_shortfall_wage == D("8000.00")
    fs = settle(w, t)
    assert lines(fs, "notice_pay") == D("8000.00")


# --- 10. separate settlement in SAR, 2 decimals ------------------------------------------------------------------

def test_rr10_separate_settlement_has_at_most_two_decimals(w):
    n1 = leaver(w)
    t = terminate(w, n1, kind="mutual_agreement", written_consent_ref="CONSENT-1")
    fs = FinalSettlement.objects.get(termination=t)
    with pytest.raises(PayrollError) as exc:
        services.prepare_final_settlement(fs, user=w.p, separate_settlement_amount="10000.005",
                                          written_consent_ref="SETTLEMENT-CONSENT-1")
    assert exc.value.code == "separate_settlement"
    fs = services.prepare_final_settlement(fs, user=w.p, separate_settlement_amount="10000.50",
                                           written_consent_ref="SETTLEMENT-CONSENT-1")
    assert lines(fs, "separate_settlement") == D("10000.50")


# --- 11. closed leaving month ------------------------------------------------------------------------------------

def test_rr11_settlement_of_a_closed_month_posts_in_the_next_open_period(w):
    n1 = leaver(w)
    t = terminate(w, n1)
    go_live(w)
    fs = services.prepare_final_settlement(FinalSettlement.objects.get(termination=t), user=w.p)
    march = FiscalPeriod.objects.get(entity=w.alm, start_date=date(2027, 3, 1))
    ledger.close_period(march, user=w.c)  # a calculated settlement does not hold the month open
    fs = _approve(w, services.submit_final_settlement(fs, user=w.p))
    assert fs.journal_entry.date == date(2027, 4, 1) and fs.last_working_day == LWD
    n2 = leaver(w)
    fs2 = settle(w, terminate(w, n2, lwd=date(2027, 4, 15)))  # submitted settlements still block the close
    april = FiscalPeriod.objects.get(entity=w.alm, start_date=date(2027, 4, 1))
    with pytest.raises(LedgerError) as exc:
        ledger.close_period(april, user=w.c)
    assert "final settlement" in str(exc.value)
    assert fs2.status == "submitted"


# --- 12. KSA employees only; payroll must be set up --------------------------------------------------------------

def test_rr12_p6_no_settlement_draft_for_a_local_entity_employee(w):
    # A local entity employee cannot be hired with the seeded data (EG parameters unverified; a Saudi entity holds no
    # local entity employees, hr R49): the gate is checked on the hand-off itself.
    si = SimpleNamespace(includes_statutory_lines=True, entity=w.alm,
                         employee=SimpleNamespace(employment_basis="local_entity_employee"))
    assert services._settlement_draft(si, w.c) is None
    assert FinalSettlement.objects.count() == 0


def test_rr12_termination_refused_while_payroll_is_not_set_up(w):
    n1 = leaver(w)
    s = PayrollSettings.objects.get(entity=w.alm)
    s._service_delete = True
    s.delete()
    t = hr.record_termination(n1, user=w.h, kind="employer_termination", last_working_day=LWD, reason_ar="سبب",
                              notice_given_on=LWD - timedelta(days=60))
    t = hr.submit_termination(t, user=w.h)
    with pytest.raises(PayrollError) as exc:
        hr.approve_termination(t, user=w.c)
    assert exc.value.code == "payroll_not_set_up" and " / " in str(exc.value)  # English / Arabic
    assert Termination.objects.get(pk=t.pk).status == "submitted"
    assert not FinalSettlement.objects.exists()


# --- 13. admin --------------------------------------------------------------------------------------------------

def test_rr13_settlement_payments_hidden_in_the_admin_without_view_payslip(w):
    n1 = leaver(w)
    t = terminate(w, n1)
    go_live(w)
    fs = _approve(w, settle(w, t))
    p = services.record_payment(w.alm, user=w.p, kind="final_settlement", final_settlement=fs,
                                payment_date=LWD + timedelta(days=5), amount=str(fs.net))
    g = services.record_payment(w.alm, user=w.p, kind="gosi", gosi_period="2027-03", payment_date=date(2027, 4, 10),
                                amount="100.00")
    ma = admin.site._registry[PayrollPayment]
    for user, sees in ((w.v, False), (w.c, True)):  # viewer: payroll.view only; approver: payroll.view_payslip too
        request = RequestFactory().get("/admin/payroll/payrollpayment/")
        request.user = user
        pks = set(ma.get_queryset(request).values_list("pk", flat=True))
        assert g.pk in pks and (p.pk in pks) == sees
        assert ma.has_view_permission(request, p) == sees and ma.has_view_permission(request, g)


# --- 14. CSV cells ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", [" =1+1", "  +SUM(A1)", " -2", " @cmd", " \t=x"])
def test_rr14_formula_after_leading_whitespace_is_neutralised(value):
    assert services._cell(value) == "'" + value


def test_rr14_plain_cells_unchanged():
    assert services._cell(" Riyadh") == " Riyadh" and services._cell("8000.00") == "8000.00"


# --- P8: two payments of the full net ------------------------------------------------------------------------------

def test_rr_p8_two_payments_of_the_full_net_never_both_pass(w):
    n1 = leaver(w)
    t = terminate(w, n1)
    go_live(w)
    fs = _approve(w, settle(w, t))
    ps = [services.submit_payment(services.record_payment(w.alm, user=w.p, kind="final_settlement",
                                                          final_settlement=fs, payment_date=date(2027, 3, 20),
                                                          amount=str(fs.net)), user=w.p) for _ in range(2)]
    services.approve_payment(ps[0], user=w.c)
    with pytest.raises(PayrollError) as exc:
        services.approve_payment(ps[1], user=w.c)
    assert exc.value.code == "overpayment"
    assert FinalSettlement.objects.get(pk=fs.pk).status == "paid"
