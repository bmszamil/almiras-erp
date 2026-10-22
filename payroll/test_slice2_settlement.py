"""Payroll slice 2 (§10.8 items 1, 2 and 4): corrections of an approved final settlement (difference lines only, a
second person approves, posted through the registry, the settlement never edited), the printable bilingual
settlement statement, the loan write-off on exit (OQ-7 default), GOSI late-payment fines (E14) and payment
reversals."""
from datetime import date, timedelta

import pytest
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, connection, transaction

from core.access.models import AccessLog
from core.access.testing import make_user, member
from payroll import services
from payroll.conftest import D, approved, entry_lines, saudi
from payroll.models import (EmployeeLoan, FinalSettlement, GosiFine, LoanInstallment, LoanWriteOff, PayrollError,
                            PayrollPayment, PayrollRun, SettlementCorrection)
from payroll.test_final_settlement import LWD, go_live, leaver, settle, terminate


def approved_settlement(w, **kw):
    n1 = leaver(w)
    t = terminate(w, n1, **kw)
    go_live(w)
    fs = services.approve_final_settlement(settle(w, t), user=w.c)
    return n1, t, fs


def correct(w, fs, lines, user=None, **kw):
    kw.setdefault("reason_ar", "خطأ في احتساب بدل الإجازة")
    return services.create_settlement_correction(fs, user=user or w.p, lines=lines, **kw)


def submit_c(w, c):
    return services.submit_settlement_correction(c, user=w.p)


def snapshot(fs):
    fs = FinalSettlement.objects.get(pk=fs.pk)
    return (fs.net, fs.gross, fs.status, sorted((l.line_type, l.amount) for l in fs.lines.all()),
            sorted((l.account_id, l.debit, l.credit) for l in fs.journal_entry.lines.all()))


# --- item 1: corrections ---------------------------------------------------------------------------------------

def test_correction_difference_lines_post_their_own_entry_and_never_edit_the_settlement(w):
    n1, t, fs = approved_settlement(w)
    before = snapshot(fs)
    c = correct(w, fs, [{"line_type": "leave_encashment", "direction": "increase", "amount": "266.67"},
                        {"line_type": "employer_gosi", "direction": "increase", "amount": "10.00",
                         "gosi_branch": "annuities"}])
    assert c.status == "draft" and c.net_change == D("266.67") and c.journal_entry is None
    c = submit_c(w, c)
    with pytest.raises(PermissionDenied):
        services.approve_settlement_correction(c, user=w.p)  # its preparer and submitter
    with pytest.raises(PermissionDenied):
        services.approve_settlement_correction(c, user=w.h)  # not the matrix row
    w.pclock.set(date(2027, 3, 25))
    c = services.approve_settlement_correction(c, user=w.c)
    assert c.number == "ALM-FSC-2027-000001" and c.journal_entry.date == date(2027, 3, 25)
    e = entry_lines(c)
    assert e == {"5100": D("266.67"), "5110": D("10.00"), "2340": D("-10.00"), "2330": D("-266.67")}
    assert c.journal_entry.document_type == "payroll.settlementcorrection" and c.journal_entry.document_id == c.pk
    assert all(l.description == c.number for l in c.journal_entry.lines.all())  # no names or pay (R36)
    assert snapshot(fs) == before  # the approved settlement and its entry are untouched (CLAUDE.md rule 1)
    assert AccessLog.objects.filter(event="permission_granted", details__action="settlement_correction_approved",
                                    actor=w.c).exists()
    # The settlement's open net includes the correction; GOSI of the month includes its 2340 credit.
    assert services._settlement_due(fs) == fs.net + D("266.67")
    assert services._correction_credits_2340(w.alm, "2027-03") == D("10.00")
    p = services.record_payment(w.alm, user=w.p, kind="final_settlement", final_settlement=fs,
                                payment_date=LWD + timedelta(days=5), amount=str(fs.net + D("266.67")))
    services.approve_payment(services.submit_payment(p, user=w.p), user=w.c)
    assert FinalSettlement.objects.get(pk=fs.pk).status == "paid"


def test_correction_after_payment_reopens_the_balance_and_a_decrease_warns_of_overpayment(w):
    n1, t, fs = approved_settlement(w)
    p = services.record_payment(w.alm, user=w.p, kind="final_settlement", final_settlement=fs,
                                payment_date=LWD + timedelta(days=5), amount=str(fs.net))
    services.approve_payment(services.submit_payment(p, user=w.p), user=w.c)
    assert FinalSettlement.objects.get(pk=fs.pk).status == "paid"
    up = services.approve_settlement_correction(submit_c(w, correct(w, fs, [
        {"line_type": "notice_pay", "direction": "increase", "amount": "500.00"}])), user=w.c)
    p2 = services.record_payment(w.alm, user=w.p, kind="final_settlement", final_settlement=fs,
                                 payment_date=LWD + timedelta(days=9), amount="500.00")
    with pytest.raises(PayrollError) as exc:
        services.record_payment(w.alm, user=w.p, kind="final_settlement", final_settlement=fs,
                                payment_date=LWD + timedelta(days=9), amount="500.01")
    assert exc.value.code == "overpayment"
    services.approve_payment(services.submit_payment(p2, user=w.p), user=w.c)
    down = correct(w, fs, [{"line_type": "notice_pay", "direction": "decrease", "amount": "200.00"}])
    assert down.net_change == D("-200.00")
    assert any("Art. 92" in x and "200.00" in x for x in down.warnings)  # paid more than now due: recover by consent
    down = services.approve_settlement_correction(submit_c(w, down), user=w.c)
    assert entry_lines(down) == {"5100": D("-200.00"), "2330": D("200.00")}
    assert up.number < down.number


@pytest.mark.parametrize("bad, code", [
    ({"line_type": "loan_recovery", "direction": "decrease", "amount": "1.00"}, "correction_line_type"),
    ({"line_type": "notice_pay", "direction": "up", "amount": "1.00"}, "correction_line_type"),
    ({"line_type": "notice_pay", "direction": "increase", "amount": 1.5}, "float_rejected"),
    ({"line_type": "notice_pay", "direction": "increase", "amount": "1.005"}, "amount_invalid"),
    ({"line_type": "notice_pay", "direction": "increase", "amount": "0"}, "amount_invalid"),
    ({"line_type": "employee_gosi", "direction": "increase", "amount": "1.00"}, "correction_branch"),
    ({"line_type": "leave_encashment", "direction": "decrease", "amount": "999999.00"}, "correction_invalid"),
    ({"line_type": "separate_settlement", "direction": "increase", "amount": "100.00"}, "correction_invalid"),
])
def test_correction_line_refusals(w, bad, code):
    n1, t, fs = approved_settlement(w)
    with pytest.raises(PayrollError) as exc:
        correct(w, fs, [bad])
    assert exc.value.code == code


def test_correction_refused_before_approval_twice_open_without_reason_or_when_nothing_posts(w):
    n1 = leaver(w)
    t = terminate(w, n1)
    go_live(w)
    fs = settle(w, t)
    with pytest.raises(PayrollError) as exc:
        correct(w, fs, [{"line_type": "notice_pay", "direction": "increase", "amount": "1.00"}])
    assert exc.value.code == "settlement_status"  # before approval: recalculate instead
    fs = services.approve_final_settlement(fs, user=w.c)
    with pytest.raises(PayrollError) as exc:
        correct(w, fs, [{"line_type": "notice_pay", "direction": "increase", "amount": "1.00"}], reason_ar="")
    assert exc.value.code == "correction_invalid"
    with pytest.raises(PayrollError) as exc:  # same account up and down: nothing to post
        correct(w, fs, [{"line_type": "notice_pay", "direction": "increase", "amount": "50.00"},
                        {"line_type": "leave_encashment", "direction": "decrease", "amount": "50.00"}])
    assert exc.value.code == "correction_empty"
    correct(w, fs, [{"line_type": "notice_pay", "direction": "increase", "amount": "1.00"}])
    with pytest.raises(PayrollError) as exc:
        correct(w, fs, [{"line_type": "notice_pay", "direction": "increase", "amount": "1.00"}])
    assert exc.value.code == "correction_open"


def test_correction_decrease_limit_counts_earlier_approved_corrections(w):
    n1, t, fs = approved_settlement(w)
    booked = sum(l.amount for l in fs.lines.filter(line_type="leave_encashment"))
    services.approve_settlement_correction(submit_c(w, correct(w, fs, [
        {"line_type": "leave_encashment", "direction": "decrease", "amount": str(booked - D("10.00"))}])), user=w.c)
    with pytest.raises(PayrollError) as exc:
        correct(w, fs, [{"line_type": "leave_encashment", "direction": "decrease", "amount": "10.01"}])
    assert exc.value.code == "correction_invalid" and "more than the settlement booked" in exc.value.messages[0]


def test_correction_separate_settlement_for_mutual_agreement_with_consent(w):
    n1, t, fs = approved_settlement(w, kind="mutual_agreement", written_consent_ref="CONSENT-1")
    c = correct(w, fs, [{"line_type": "separate_settlement", "direction": "increase", "amount": "1000.00"}],
                written_consent_ref="EXTRA-CONSENT-2")
    assert c.net_change == D("1000.00")


def test_correction_employee_and_second_person_rules_and_return(w):
    other = make_user("c3")
    member(other, w.alm, "approver")
    n1 = leaver(w, linked_user=other)  # an approver who is the leaver
    t = terminate(w, n1)
    go_live(w)
    fs = services.approve_final_settlement(settle(w, t), user=w.c)
    c = submit_c(w, correct(w, fs, [{"line_type": "other_earning", "direction": "increase", "amount": "75.00"}]))
    c = services.return_settlement_correction(c, user=w.c, reason_ar="أرفق المستند")
    assert c.status == "returned"
    c = submit_c(w, c)
    with pytest.raises(PermissionDenied):
        services.approve_settlement_correction(c, user=other)  # the employee never approves their own
    assert services.approve_settlement_correction(c, user=w.mp).status == "approved"  # MP fallback


def test_correction_stored_lines_tampered_are_refused(w):
    n1, t, fs = approved_settlement(w)
    c = submit_c(w, correct(w, fs, [{"line_type": "other_earning", "direction": "increase", "amount": "75.00"}]))
    if connection.vendor == "postgresql":
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("UPDATE payroll_settlementcorrectionline SET amount = 9999 WHERE correction_id = %s", [c.pk])
        return
    with connection.cursor() as cursor:
        cursor.execute("UPDATE payroll_settlementcorrectionline SET amount = 9999 WHERE correction_id = %s", [c.pk])
    with pytest.raises(PayrollError) as exc:
        services.approve_settlement_correction(c, user=w.c)
    assert exc.value.code == "payslips_changed"


def test_settlement_statement_is_bilingual_logged_and_shows_corrections_and_balance(w):
    n1, t, fs = approved_settlement(w)
    services.approve_settlement_correction(submit_c(w, correct(w, fs, [
        {"line_type": "notice_pay", "direction": "increase", "amount": "500.00"}])), user=w.c)
    html = services.render_settlement_statement(fs, user=w.p)
    assert 'lang="ar" dir="rtl"' in html and "The Arabic text prevails" in html and "النص العربي هو المعتمد" in html
    assert fs.number in html and "ALM-FSC-2027-000001" in html and n1.employee_code in html
    data = services.settlement_statement_data(fs, user=w.p)
    assert data["due"] == fs.net + D("500.00") and data["balance"] == data["due"] and data["final"]
    assert "interest" not in html.lower() or "qard hasan" in html
    assert AccessLog.objects.filter(event="personal_data_viewed", details__action="settlement_statement_viewed").exists()
    with pytest.raises(PermissionDenied):
        services.settlement_statement_data(fs, user=w.v)  # individual pay needs payroll.view_payslip
    with pytest.raises(PermissionDenied):
        services.settlement_statement_data(fs, user=w.ps)  # another entity's payroll officer


# --- item 2: loan write-off on exit ------------------------------------------------------------------------------

def _loan_left_after_exit(w, allowed=True):
    n1 = leaver(w, hire=date(2027, 1, 1))
    loan = services.create_loan(n1, user=w.p, kind="loan", principal="18000.00", disbursement_date=date(2027, 2, 1),
                                purpose_ar="قرض حسن", installments=[("2027-12", "18000.00")])
    loan = services.approve_loan(services.submit_loan(loan, user=w.p), user=w.mp)
    t = terminate(w, n1)
    go_live(w)
    fs = settle(w, t)
    return n1, loan, fs


def test_oq7_write_off_waits_for_the_settlement_then_mp_posts_dr_expense_cr_1230(w):
    n1, loan, fs = _loan_left_after_exit(w)
    with pytest.raises(PayrollError) as exc:  # settlement submitted, not approved: the dues come first
        services.prepare_loan_write_off(loan, user=w.p, reason_ar="لا يمكن التحصيل")
    assert exc.value.code == "write_off_before_settlement"
    fs = services.approve_final_settlement(fs, user=w.c)
    left = services._loan_outstanding_of(EmployeeLoan.objects.get(pk=loan.pk))
    assert left > 0
    with pytest.raises(PayrollError):
        services.prepare_loan_write_off(loan, user=w.p, reason_ar="")
    wo = services.prepare_loan_write_off(loan, user=w.p, reason_ar="تعذر التحصيل بعد انتهاء الخدمة")
    assert wo.amount == left and wo.expense_account.code == "5100" and wo.journal_entry is None
    with pytest.raises(PayrollError) as exc:
        services.prepare_loan_write_off(loan, user=w.p, reason_ar="مكرر")
    assert exc.value.code == "write_off_exists"
    wo = services.submit_loan_write_off(wo, user=w.p)
    with pytest.raises(PermissionDenied):
        services.approve_loan_write_off(wo, user=w.c)  # the CFO does not hold it (ALM: the MP, OQ-7 default)
    with pytest.raises(PermissionDenied):
        services.approve_loan_write_off(wo, user=w.p)
    w.pclock.set(date(2027, 3, 28))
    wo = services.approve_loan_write_off(wo, user=w.mp)
    assert wo.number == "ALM-LWO-2027-000001" and wo.journal_entry.date == date(2027, 3, 28)
    assert entry_lines(wo) == {"5100": left, "1230": -left}
    loan = EmployeeLoan.objects.get(pk=loan.pk)
    assert loan.status == "written_off" and services._loan_outstanding_of(loan) == 0
    assert set(LoanInstallment.objects.filter(loan=loan).values_list("status", flat=True)) == {
        "settled_in_final", "written_off"}
    log = AccessLog.objects.get(details__action="loan_written_off")
    assert log.actor == w.mp and D(log.details["amount"]) == left and log.details["employee_code"] == n1.employee_code


def test_oq7_write_off_outside_the_group_parent_needs_the_ceo(w):
    """P20(a), access A26: outside Almiras the top authority is the CEO (seed row ``ceo``; the role comes with the
    access build, A28); the MP's group role has no membership in SUB (no cascade, P20(c))."""
    from payroll import approvals
    assert approvals.allowed_roles(w.alm, "payroll.loan_write_off") == ("managing_partner",)
    assert approvals.allowed_roles(w.sub, "payroll.loan_write_off") == ("ceo",)
    from types import SimpleNamespace

    from core.access.models import Membership
    grant = SimpleNamespace(user=w.mp, permission="payroll.approve_loan_write_off",
                            membership_ids=list(Membership.objects.filter(user=w.mp).values_list("pk", flat=True)))
    approvals.check(grant, w.alm, "payroll.loan_write_off")  # Almiras: the MP
    with pytest.raises(PermissionDenied) as exc:
        approvals.check(grant, w.sub, "payroll.loan_write_off")  # SUB: its CEO, never the MP
    assert "ceo" in str(exc.value)


def test_oq7_write_off_account_is_data_and_refused_if_not_an_expense(w):
    n1, loan, fs = _loan_left_after_exit(w)
    services.approve_final_settlement(fs, user=w.c)
    with pytest.raises(PayrollError) as exc:
        services.prepare_loan_write_off(loan, user=w.p, reason_ar="سبب", expense_account="1100")
    assert exc.value.code == "write_off_account"
    with pytest.raises(PayrollError) as exc:
        services.prepare_loan_write_off(loan, user=w.p, reason_ar="سبب", expense_account="5610")  # Sharia (R37)
    assert exc.value.code == "write_off_account"
    wo = services.prepare_loan_write_off(loan, user=w.p, reason_ar="سبب", expense_account="5900")
    wo = services.reject_loan_write_off(wo, user=w.mp, reason_ar="يُحصّل من الموظف")
    assert wo.status == "rejected"
    assert services.prepare_loan_write_off(loan, user=w.p, reason_ar="سبب ثان").status == "draft"


def test_oq7_write_off_amount_rechecked_at_approval(w):
    n1, loan, fs = _loan_left_after_exit(w)
    services.approve_final_settlement(fs, user=w.c)
    wo = services.submit_loan_write_off(services.prepare_loan_write_off(loan, user=w.p, reason_ar="سبب"), user=w.p)
    row = LoanWriteOff._base_manager.get(pk=wo.pk)
    with connection.cursor() as cursor:  # a different amount reaching the approval (not a service path)
        cursor.execute("UPDATE payroll_loanwriteoff SET amount = amount - 1 WHERE id = %s", [row.pk])
    with pytest.raises(PayrollError) as exc:
        services.approve_loan_write_off(wo, user=w.mp)
    assert exc.value.code == "write_off_amount"


# --- item 4: GOSI fines (E14) and payment reversal --------------------------------------------------------------

def test_e14_gosi_fine_estimate_record_and_post_on_5930(w):
    saudi(w)
    run = approved(w, "2027-03")
    booked = services._gosi_booked(w.alm, "2027-03")
    est, months = services._gosi_fine_estimate(w.alm, "2027-03", date(2027, 4, 20))
    assert months == 1 and est == (booked * D("0.02")).quantize(D("0.01"))
    assert services._gosi_fine_estimate(w.alm, "2027-03", date(2027, 4, 15)) == (D("0"), 0)
    assert services._gosi_fine_estimate(w.alm, "2027-03", date(2027, 5, 16))[1] == 2  # a part of a month counts
    with pytest.raises(PayrollError):
        services.record_gosi_fine(w.alm, user=w.p, period="2027-03", amount="10.00", fine_date=date(2027, 4, 30),
                                  gosi_invoice_ref="")
    fine = services.record_gosi_fine(w.alm, user=w.p, period="2027-03", amount="60.00", fine_date=date(2027, 4, 30),
                                     gosi_invoice_ref="GOSI-INV-2027-04", paid_on=date(2027, 4, 20))
    assert fine.estimate == est and "governs" in fine.note_en
    fine = services.submit_gosi_fine(fine, user=w.p)
    with pytest.raises(PermissionDenied):
        services.approve_gosi_fine(fine, user=w.p)  # its recorder
    fine = services.approve_gosi_fine(fine, user=w.c)
    assert entry_lines(fine) == {"5930": D("60.00"), "2340": D("-60.00")}
    assert fine.journal_entry.date == date(2027, 4, 30) and fine.number.startswith("ALM-GFN-2027-")
    assert services._gosi_open(w.alm, "2027-03") == booked + D("60.00")  # paid to GOSI with the period
    report = services.gosi_report(w.alm, "2027-03", user=w.c)
    assert report["fines"] == D("60.00") and report["total_due"] == report["total"] + D("60.00")
    p = services.record_payment(w.alm, user=w.p, kind="gosi", gosi_period="2027-03", payment_date=date(2027, 5, 1),
                                amount=str(booked + D("60.00")))
    services.approve_payment(services.submit_payment(p, user=w.p), user=w.c)
    assert services._gosi_open(w.alm, "2027-03") == 0
    assert run.pk


def test_payment_reversal_reopens_the_run_and_is_a_mirror_entry(w):
    s1 = saudi(w)
    run = approved(w, "2027-03")
    net = run.payslips.get(employee=s1).net
    p = services.record_payment(w.alm, user=w.p, kind="net_salaries", run=run, payment_date=date(2027, 3, 31),
                                amount=str(net))
    p = services.approve_payment(services.submit_payment(p, user=w.p), user=w.c)
    assert PayrollRun.objects.get(pk=run.pk).status == "paid"
    with pytest.raises(PermissionDenied):
        services.reverse_payment(p, user=w.c, reason="bank rejected")  # its approver
    with pytest.raises(PermissionDenied):
        services.reverse_payment(p, user=w.p, reason="bank rejected")  # its recorder (no right either)
    c3 = make_user("c3")
    member(c3, w.alm, "approver")
    with pytest.raises(PayrollError):
        services.reverse_payment(p, user=c3, reason="")
    p = services.reverse_payment(p, user=c3, reason="The bank rejected the transfer")
    assert p.status == "reversed" and p.reversal_entry is not None
    assert entry_lines(p) == {"2330": net, "1100": -net}
    rev = {l.account.code: l.debit - l.credit for l in p.reversal_entry.lines.select_related("account")}
    assert rev == {"2330": -net, "1100": net}
    assert PayrollRun.objects.get(pk=run.pk).status == "approved"
    with pytest.raises(PayrollError):
        services.reverse_payment(p, user=c3, reason="again")
    p2 = services.record_payment(w.alm, user=w.p, kind="net_salaries", run=run, payment_date=date(2027, 4, 2),
                                 amount=str(net))
    services.approve_payment(services.submit_payment(p2, user=w.p), user=w.c)
    assert PayrollRun.objects.get(pk=run.pk).status == "paid"


def test_payment_reversal_of_a_final_settlement_and_a_gosi_payment(w):
    n1, t, fs = approved_settlement(w)
    p = services.record_payment(w.alm, user=w.p, kind="final_settlement", final_settlement=fs,
                                payment_date=LWD + timedelta(days=5), amount=str(fs.net))
    p = services.approve_payment(services.submit_payment(p, user=w.p), user=w.c)
    c3 = make_user("c3")
    member(c3, w.alm, "approver")
    services.reverse_payment(p, user=c3, reason="returned by the bank")
    fs = FinalSettlement.objects.get(pk=fs.pk)
    assert fs.status == "approved"
    gp = services.record_payment(w.alm, user=w.p, kind="gosi", gosi_period="2027-03", payment_date=date(2027, 4, 10),
                                 amount=str(services._gosi_open(w.alm, "2027-03")))
    gp = services.approve_payment(services.submit_payment(gp, user=w.p), user=w.c)
    assert services._gosi_open(w.alm, "2027-03") == 0
    services.reverse_payment(gp, user=c3, reason="paid twice")
    assert services._gosi_open(w.alm, "2027-03") > 0
    assert PayrollPayment.objects.filter(status="reversed").count() == 2


@pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL triggers")
def test_slice2_pg_documents_locked_in_the_database(w):
    n1, loan, fs = _loan_left_after_exit(w)
    fs = services.approve_final_settlement(fs, user=w.c)
    c = services.approve_settlement_correction(submit_c(w, correct(w, fs, [
        {"line_type": "notice_pay", "direction": "increase", "amount": "5.00"}])), user=w.c)
    wo = services.approve_loan_write_off(services.submit_loan_write_off(services.prepare_loan_write_off(
        loan, user=w.p, reason_ar="سبب"), user=w.p), user=w.mp)
    fine = services.approve_gosi_fine(services.submit_gosi_fine(services.record_gosi_fine(
        w.alm, user=w.p, period="2027-03", amount="5.00", fine_date=date(2027, 4, 30), gosi_invoice_ref="G-1"),
        user=w.p), user=w.c)
    for sql, pk in (("UPDATE payroll_settlementcorrection SET net_change = 1 WHERE id = %s", c.pk),
                    ("DELETE FROM payroll_settlementcorrection WHERE id = %s", c.pk),
                    ("UPDATE payroll_settlementcorrectionline SET amount = 1 WHERE correction_id = %s", c.pk),
                    ("UPDATE payroll_loanwriteoff SET amount = 1 WHERE id = %s", wo.pk),
                    ("DELETE FROM payroll_loanwriteoff WHERE id = %s", wo.pk),
                    ("UPDATE payroll_gosifine SET amount = 1 WHERE id = %s", fine.pk),
                    ("UPDATE payroll_loaninstallment SET status = 'planned' WHERE loan_id = %s AND status = "
                     "'written_off'", loan.pk)):
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(sql, [pk])
    p = services.record_payment(w.alm, user=w.p, kind="final_settlement", final_settlement=fs,
                                payment_date=LWD + timedelta(days=5), amount=str(services._settlement_due(fs)))
    p = services.approve_payment(services.submit_payment(p, user=w.p), user=w.c)
    c3 = make_user("c3")
    member(c3, w.alm, "approver")
    p = services.reverse_payment(p, user=c3, reason="bank")
    for sql in ("UPDATE payroll_payrollpayment SET status = 'approved' WHERE id = %s",
                "UPDATE payroll_payrollpayment SET amount = 1 WHERE id = %s",
                "DELETE FROM payroll_payrollpayment WHERE id = %s"):
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(sql, [p.pk])
    assert GosiFine.objects.get(pk=fine.pk).status == "approved"
    assert SettlementCorrection.objects.get(pk=c.pk).status == "approved"


def test_oq7_write_off_account_setting_is_data_checked_as_an_expense(w):
    with pytest.raises(PayrollError) as exc:
        services.configure_settings(w.alm, user=w.c, loan_write_off_account="1100")
    assert exc.value.code == "settings_invalid"
    with pytest.raises(PayrollError):
        services.configure_settings(w.alm, user=w.c, loan_write_off_account="5610")  # Sharia-prohibited in ALM
    services.configure_settings(w.alm, user=w.c, loan_write_off_account="5900")
    n1, loan, fs = _loan_left_after_exit(w)
    services.approve_final_settlement(fs, user=w.c)
    assert services.prepare_loan_write_off(loan, user=w.p, reason_ar="سبب").expense_account.code == "5900"
