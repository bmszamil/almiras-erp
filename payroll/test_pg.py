"""PostgreSQL only: the payroll immutability triggers (migration 0003) refuse raw SQL that bypasses the ORM (R7,
R13, R24, R29, R31; AT-4)."""
from datetime import date

import pytest
from django.db import DatabaseError, connection, transaction

from payroll import services
from payroll.conftest import approved, run_for, saudi

pytestmark = [pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL triggers"),
              pytest.mark.django_db]


def _refused(sql, params=()):
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql, params)


def _ok(sql, params=()):
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql, params)


def test_at4_raw_sql_cannot_skip_the_lifecycle_or_change_an_approved_run(w):
    saudi(w)
    draft = run_for(w, "2027-04")
    _refused("UPDATE payroll_payrollrun SET status = 'approved' WHERE id = %s", [draft.pk])
    _ok("UPDATE payroll_payrollrun SET pay_date = pay_date WHERE id = %s", [draft.pk])
    run = approved(w)
    _refused("UPDATE payroll_payrollrun SET inputs_hash = 'x' WHERE id = %s", [run.pk])
    _refused("UPDATE payroll_payrollrun SET status = 'draft' WHERE id = %s", [run.pk])
    _refused("UPDATE payroll_payrollrun SET journal_entry_id = NULL WHERE id = %s", [run.pk])
    _refused("DELETE FROM payroll_payrollrun WHERE id = %s", [run.pk])
    _refused("UPDATE payroll_payslip SET net = 1 WHERE run_id = %s", [run.pk])
    _refused("UPDATE payroll_payslipline SET amount = 1 WHERE payslip_id IN (SELECT id FROM payroll_payslip WHERE "
             "run_id = %s)", [run.pk])
    _refused("DELETE FROM payroll_payslipline WHERE payslip_id IN (SELECT id FROM payroll_payslip WHERE run_id = %s)",
             [run.pk])
    _refused("UPDATE payroll_eosbprovisionentry SET amount = 0")
    _refused("DELETE FROM payroll_eosbprovisionentry")


def test_r29_r31_approved_payments_and_wps_files_are_locked(w):
    saudi(w)
    run = approved(w)
    p = services.record_payment(w.alm, user=w.p, kind="net_salaries", run=run, payment_date=date(2027, 3, 31),
                                amount=run.payslips.get().net)
    p = services.approve_payment(services.submit_payment(p, user=w.p), user=w.c)
    _refused("UPDATE payroll_payrollpayment SET amount = 1 WHERE id = %s", [p.pk])
    _refused("DELETE FROM payroll_payrollpayment WHERE id = %s", [p.pk])
    f = services.generate_wps_file(run, user=w.p, format_code="generic_csv")
    _refused("DELETE FROM payroll_wpsfile WHERE id = %s", [f.pk])


def test_r26_raw_sql_cannot_change_a_submitted_loan(w):
    s1 = saudi(w)
    loan = services.create_loan(s1, user=w.p, kind="loan", principal="1000.00", disbursement_date=date(2027, 2, 1),
                                purpose_ar="قرض", installments=[("2027-03", "1000.00")])
    services.submit_loan(loan, user=w.p)
    _refused("UPDATE payroll_employeeloan SET principal = 2000 WHERE id = %s", [loan.pk])
    _refused("UPDATE payroll_employeeloan SET interest_free = false WHERE id = %s", [loan.pk])
