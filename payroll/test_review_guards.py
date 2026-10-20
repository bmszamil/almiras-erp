"""Review fixes (2026-10-19): stored payslips are what approval posts; database guards (review items 5, 6, 15).

Ported from the reviewer's probes test_zz_probes.py (P6) and test_zz_pg_probes.py (PG2, PG3, PG4)."""
from datetime import date

import pytest
from django.db import DatabaseError, connection, transaction

from payroll import services
from payroll.conftest import D, approved, entry_lines, expat, saudi, slip, submitted
from payroll.models import EmployeeLoan, LoanInstallment, PayrollError, PayrollRun, PayslipLine, WpsFile

on_pg = pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL triggers")


def test_review6_tampered_stored_lines_are_never_posted(w):
    """Probe P6: the stored lines are changed by raw SQL after submission. On PostgreSQL the trigger refuses the
    write itself; on SQLite (no triggers) approval compares the stored-rows hash and refuses."""
    s1 = saudi(w)
    run = submitted(w, "2027-03")
    line = slip(run, s1).lines.get(line_type="earning", component__code="BASIC")
    sql = "UPDATE payroll_payslipline SET amount = 99999 WHERE id = %s"
    if connection.vendor == "postgresql":
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(sql, [line.pk])
    else:
        with connection.cursor() as cursor:
            cursor.execute(sql, [line.pk])
        with pytest.raises(PayrollError) as exc:
            services.approve_run(run, user=w.h)
        assert exc.value.code == "payslips_changed"
        stored = PayrollRun.objects.get(pk=run.pk)
        assert stored.status == "submitted" and stored.journal_entry_id is None
        run = services.calculate_run(services.return_run(run, user=w.h, reason_ar="تصحيح"), user=w.p)
        run = services.submit_run(run, user=w.p)
    run = services.approve_run(run, user=w.h)
    assert entry_lines(run)["2330"] == -slip(run, s1).net and entry_lines(run)["5100"] < D("99999")


def test_review6_base_manager_and_bulk_writes_are_refused(w):
    s1 = saudi(w)
    run = submitted(w, "2027-03")
    line = slip(run, s1).lines.first()
    with pytest.raises(PayrollError):
        PayslipLine._base_manager.filter(pk=line.pk).update(amount=D("1.00"))
    with pytest.raises(PayrollError):
        PayslipLine.objects.bulk_create([PayslipLine(entity=line.entity, payslip=line.payslip, line_type="earning",
                                                     amount=D("1.00"), currency=line.currency, amount_txn=D("1.00"),
                                                     account=line.account, description_en="x", description_ar="س")])
    with pytest.raises(PayrollError):
        LoanInstallment._base_manager.all().delete()
    assert PayslipLine._base_manager.get(pk=line.pk).amount == line.amount


def _refused(sql, params=()):
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql, params)


@on_pg
def test_review6_pg_payslips_and_lines_are_locked_once_the_run_is_submitted(w):
    """Probe PG2: raw SQL changed the lines of a submitted run and the approval posted them."""
    saudi(w)
    run = submitted(w, "2027-03")
    _refused("UPDATE payroll_payslipline SET amount = 99999 WHERE payslip_id IN (SELECT id FROM payroll_payslip "
             "WHERE run_id = %s)", [run.pk])
    _refused("UPDATE payroll_payslip SET net = net + 1 WHERE run_id = %s", [run.pk])
    _refused("DELETE FROM payroll_payslipline WHERE payslip_id IN (SELECT id FROM payroll_payslip WHERE run_id = %s)",
             [run.pk])
    services.return_run(run, user=w.h, reason_ar="تصحيح")
    assert services.calculate_run(run, user=w.p).status == "calculated"  # a returned run is recalculated


@on_pg
def test_review5_pg_a_deducted_installment_changes_only_through_the_reversal(w):
    """Probe PG4: raw SQL reset a deducted installment to planned."""
    n1 = expat(w)
    loan = services.create_loan(n1, user=w.p, kind="loan", principal="800.00", disbursement_date=date(2027, 2, 1),
                                purpose_ar="قرض", installments=[("2027-03", "800.00")])
    services.approve_loan(services.submit_loan(loan, user=w.p), user=w.mp)
    run = approved(w, "2027-03")
    inst = LoanInstallment.objects.get(loan=loan)
    _refused("UPDATE payroll_loaninstallment SET deducted_amount = 0, status = 'planned', run_id = NULL WHERE id = %s",
             [inst.pk])
    _refused("DELETE FROM payroll_loaninstallment WHERE id = %s", [inst.pk])
    _refused("UPDATE payroll_loaninstallment SET amount = 1 WHERE id = %s", [inst.pk])
    services.reverse_run(run, user=w.c, reason="error")
    inst = LoanInstallment.objects.get(pk=inst.pk)
    assert inst.status == "planned" and services._loan_outstanding_of(EmployeeLoan.objects.get(pk=loan.pk)) == D("800.00")


@on_pg
def test_review15_pg_wps_file_content_never_changes_and_the_bank_file_is_attached_once(w):
    """Probe PG3: raw SQL replaced the stored file and its hash."""
    saudi(w)
    run = approved(w, "2027-03")
    f = services.generate_wps_file(run, user=w.p, format_code="generic_csv")
    _refused("UPDATE payroll_wpsfile SET content = 'tampered', sha256 = %s WHERE id = %s", ["0" * 64, f.pk])
    services.attach_bank_processed_file(f, user=w.p, content="processed")
    _refused("UPDATE payroll_wpsfile SET bank_processed_sha256 = %s WHERE id = %s", ["1" * 64, f.pk])
    assert WpsFile.objects.get(pk=f.pk).content == f.content
