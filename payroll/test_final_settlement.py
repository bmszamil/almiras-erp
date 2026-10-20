"""Final settlement (compliance C-A11, review 14; payroll 3.8, R10, R28, R34, R49, R66; E5; AT-20, AT-40, AT-58).

Leavers were left out of the runs (R10) and nothing paid them. The hr termination approval now drafts a payroll
final settlement: salary to the last working day net of what the runs paid, leave encashment, EOSB per the hr
snapshot, notice pay, a separate settlement for a mutual agreement, GOSI of the leaving month on days of service
(Regs Art. 20(2)), loan recovery from all final dues without the monthly caps (P17), the Art. 88 deadline with
alerts, posting through the ledger registry and payment."""
from datetime import date, timedelta

import pytest
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, connection, transaction

from hr import services as hr
from hr.models import FinalSettlementInput, HrError
from payroll import services
from payroll.conftest import D, approved, calculated, entry_lines, saudi, slip
from payroll.models import (EmployeeLoan, EosbProvisionEntry, FinalSettlement, LoanInstallment, PayrollAlert,
                            PayrollError, PayrollRun)

LWD = date(2027, 3, 15)
WAGE = {"BASIC": "6000.00", "HOUSING": "1500.00", "TRANSPORT": "500.00"}  # actual wage 8,000.00, GOSI wage 7,500.00


def leaver(w, hire=date(2023, 6, 1), **fields):
    """A Saudi (open-ended contract, legacy cohort) on the E5 wage."""
    return saudi(w, components=dict(WAGE), hire_date=hire, **fields)


def terminate(w, emp, kind="employer_termination", lwd=LWD, **fields):
    if kind in ("employer_termination", "employee_notice_art75"):
        fields.setdefault("notice_given_on", lwd - timedelta(days=60))
    t = hr.record_termination(emp, user=w.h, kind=kind, last_working_day=lwd, reason_ar="سبب الإنهاء", **fields)
    return hr.approve_termination(hr.submit_termination(t, user=w.h), user=w.c)


def opening_provision(w, emp, amount):
    row = EosbProvisionEntry(entity=w.alm, employee=emp, date=date(2027, 1, 1), kind="opening", amount=D(amount))
    row._service_write = True
    row.save()


def go_live(w, period="2027-03"):
    """Verification 1: a final settlement waits for the entity's first approved regular run. Called after the
    termination, an approved March run (go-live in the leaving month) leaves the leaver out (R10) and pays another
    employee, so the leaver's figures do not change."""
    saudi(w)
    return approved(w, period)


def settle(w, t, **kw):
    fs = FinalSettlement.objects.get(termination=t)
    fs = services.prepare_final_settlement(fs, user=w.p, **kw)
    return services.submit_final_settlement(fs, user=w.p)


def lines(fs, line_type):
    return sum((l.amount for l in fs.lines.filter(line_type=line_type)), D("0"))


def test_c_a11_e5_settlement_salary_to_last_day_leave_eosb_gosi_by_days_loan_and_payment(w):
    n1 = leaver(w)
    loan = services.create_loan(n1, user=w.p, kind="loan", principal="1000.00", disbursement_date=date(2027, 2, 1),
                                purpose_ar="قرض حسن", installments=[("2027-06", "1000.00")])
    services.approve_loan(services.submit_loan(loan, user=w.p), user=w.mp)
    approved(w, "2027-02")
    opening_provision(w, n1, "15500.00")
    balance = sum(r.amount for r in EosbProvisionEntry.objects.filter(employee=n1))
    t = terminate(w, n1)
    si = FinalSettlementInput.objects.get(termination=t, status="draft")
    fs = FinalSettlement.objects.get(termination=t)
    assert fs.status == "draft" and fs.due_date == LWD + timedelta(days=7) == si.due_by  # employer ended: 7 days
    assert not calculated(w, "2027-03").payslips.filter(employee=n1).exists()  # R10: not in the run
    fs = settle(w, t)
    # March 1 to 15 of 8,000.00 = 15/30; GOSI of the leaving month on days of service: 7,500.00 x 15/31 = 3,629.03
    # (R66): employee 9% + 0.75% = 326.61 + 27.22, employer 9% + 0.75% + 2% = 326.61 + 27.22 + 72.58.
    assert lines(fs, "salary_to_last_day") == D("4000.00")
    assert lines(fs, "employee_gosi") == D("353.83") and lines(fs, "employer_gosi") == D("426.41")
    assert lines(fs, "leave_encashment") == si.leave_encashment
    award = t.eosb_calculations.get(superseded_by__isnull=True).award
    assert lines(fs, "eosb") == award and fs.eosb_provision_balance == balance
    assert lines(fs, "loan_recovery") == D("1000.00")  # all of it, no 10% / 50% cap (P17)
    assert fs.net == D("4000.00") + si.leave_encashment + award - D("1000.00") - D("353.83")
    with pytest.raises(PermissionDenied):
        services.approve_final_settlement(fs, user=w.p)  # its preparer
    with pytest.raises(PermissionDenied):
        services.approve_final_settlement(fs, user=w.h)  # not the matrix row
    fs = services.approve_final_settlement(fs, user=w.c)
    assert fs.number == "ALM-FS-2027-000001" and fs.journal_entry.date == LWD
    e = entry_lines(fs)
    assert e["2500"] == balance and e["5120"] == award - balance
    assert e["5100"] == D("4000.00") + si.leave_encashment and e["5110"] == D("426.41")
    assert e["2340"] == D("-780.24")
    assert e["1230"] == D("-1000.00") and e["2330"] == -fs.net
    assert sum(r.amount for r in EosbProvisionEntry.objects.filter(employee=n1)) == 0
    assert LoanInstallment.objects.get(loan=loan).status == "settled_in_final"
    assert EmployeeLoan.objects.get(pk=loan.pk).status == "settled"
    with pytest.raises(HrError):  # hr R40: the snapshot is final once the settlement is approved
        hr.recalculate_eosb(t, user=w.h)
    p = services.record_payment(w.alm, user=w.p, kind="final_settlement", final_settlement=fs,
                                payment_date=LWD + timedelta(days=5), amount=str(fs.net))
    with pytest.raises(PayrollError):
        services.record_payment(w.alm, user=w.p, kind="final_settlement", final_settlement=fs,
                                payment_date=LWD + timedelta(days=5), amount=str(fs.net + D("0.01")))
    p = services.approve_payment(services.submit_payment(p, user=w.p), user=w.c)
    assert {l.account.code for l in p.journal_entry.lines.select_related("account")} == {"2330", "1100"}
    assert FinalSettlement.objects.get(pk=fs.pk).status == "paid"


def test_c_a11_deadline_alerts_and_worker_initiated_14_days(w):
    n1 = leaver(w)
    t = terminate(w, n1)
    fs = FinalSettlement.objects.get(termination=t)
    for day, key in ((fs.due_date - timedelta(days=3), "before"), (fs.due_date, "due"),
                     (fs.due_date + timedelta(days=1), "late")):
        services.send_alerts(today=day, entity=w.alm)
        services.send_alerts(today=day, entity=w.alm)  # once each
        assert PayrollAlert.objects.filter(kind="final_settlement_due", key=f"fs:{fs.pk}:{key}", recipient=w.c).count() == 1
    n2 = leaver(w)
    t2 = terminate(w, n2, kind="art81_employee_leaves", article_paragraph="2")
    assert FinalSettlement.objects.get(termination=t2).due_date == LWD + timedelta(days=14)  # the worker ended it


def test_at58_mutual_agreement_separate_settlement_needs_written_consent(w):
    n1 = leaver(w)
    t = terminate(w, n1, kind="mutual_agreement", written_consent_ref="CONSENT-1")
    fs = FinalSettlement.objects.get(termination=t)
    with pytest.raises(PayrollError) as exc:
        services.prepare_final_settlement(fs, user=w.p, separate_settlement_amount="10000.00")
    assert exc.value.code == "separate_settlement"
    fs = services.prepare_final_settlement(fs, user=w.p, separate_settlement_amount="10000.00",
                                           written_consent_ref="SETTLEMENT-CONSENT-1")
    award = t.eosb_calculations.get(superseded_by__isnull=True)
    assert lines(fs, "eosb") == award.award == award.full_award  # the statutory floor, never below (P19(i))
    assert lines(fs, "separate_settlement") == D("10000.00") and lines(fs, "employer_gosi") == D("426.41")


@pytest.mark.parametrize("allowed", [True, False])
def test_at40_loan_larger_than_the_dues(w, settings, allowed):
    settings.PAYROLL_EOSB_DEDUCTION_ALLOWED = allowed
    n1 = leaver(w, hire=date(2027, 1, 1))
    loan = services.create_loan(n1, user=w.p, kind="loan", principal="18000.00", disbursement_date=date(2027, 2, 1),
                                purpose_ar="قرض حسن", installments=[("2027-12", "18000.00")])
    services.approve_loan(services.submit_loan(loan, user=w.p), user=w.mp)
    t = terminate(w, n1)
    go_live(w)
    fs = settle(w, t)
    eosb = lines(fs, "eosb")
    dues = fs.gross - lines(fs, "employee_gosi")  # what is left after the GOSI share
    recovered = lines(fs, "loan_recovery")
    assert recovered == (dues if allowed else dues - eosb)
    assert fs.net == (0 if allowed else eosb)
    assert any("write-off" in x for x in fs.warnings)
    fs = services.approve_final_settlement(fs, user=w.c)
    assert services._loan_outstanding_of(EmployeeLoan.objects.get(pk=loan.pk)) == D("18000.00") - recovered


def test_review14_leaving_month_already_paid_by_the_run_is_recovered_from_the_dues(w):
    n1 = leaver(w)
    mar = approved(w, "2027-03")  # paid the whole of March before the termination was approved
    assert slip(mar, n1).gross == D("8000.00")
    t = terminate(w, n1)
    fs = settle(w, t)
    assert lines(fs, "salary_to_last_day") == 0
    assert sum(l.amount for l in fs.lines.filter(line_type="other_deduction", source_ref__startswith="overpaid:")) \
        == D("4000.00")  # March 16 to 30 were paid; recovered from the final dues (Art. 88)
    assert any("GOSI" in x and "reconciliation" in x for x in fs.warnings)  # full month booked, days due (R66)


def test_c_a11_withdrawal_discards_the_draft_and_remuneration_has_none(w):
    n1 = leaver(w)
    t = terminate(w, n1, lwd=date(2027, 7, 31))  # last working day after the hr clock (2027-06-01)
    fs = FinalSettlement.objects.get(termination=t)
    hr.withdraw_termination(t, user=w.c, reason_ar="سحب")
    assert FinalSettlement.objects.get(pk=fs.pk).status == "discarded"
    from payroll.conftest import hired
    r1 = hired(w, basis="remuneration")
    t2 = hr.record_termination(r1, user=w.h, kind="end_of_remuneration", last_working_day=date(2027, 3, 31),
                               reason_ar="انتهاء")
    t2 = hr.approve_termination(hr.submit_termination(t2, user=w.h), user=w.c)
    assert not FinalSettlement.objects.filter(termination=t2).exists()  # the run pays the last month (R10, H17)
    assert PayrollRun.objects.count() == 0


@pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL triggers")
def test_c_a11_pg_settlement_and_lines_locked_in_the_database(w):
    n1 = leaver(w)
    t = terminate(w, n1)
    go_live(w)
    fs = settle(w, t)
    for sql in ("UPDATE payroll_finalsettlementline SET amount = 1 WHERE settlement_id = %s",
                "DELETE FROM payroll_finalsettlementline WHERE settlement_id = %s"):
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(sql, [fs.pk])  # submitted: lines locked
    fs = services.approve_final_settlement(fs, user=w.c)
    for sql in ("UPDATE payroll_finalsettlement SET net = 1 WHERE id = %s",
                "DELETE FROM payroll_finalsettlement WHERE id = %s"):
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(sql, [fs.pk])
