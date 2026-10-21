"""PostgreSQL only: the slice 3 guards (migration payroll.0021) refuse raw SQL that bypasses the services: Mudad
justifications move only along R80 and are never deleted; justification exports are never rewritten; liability
reports are never edited once sealed and their lines are append-only (R75, R80, R81)."""
from datetime import date, datetime

import pytest
from django.db import DatabaseError, connection, transaction

from hr.models import LeaveLedger, LeaveType
from payroll import services
from payroll.conftest import D, approved, expat, saudi
from payroll.models import MudadJustification

pytestmark = [pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL triggers"),
              pytest.mark.django_db]


def _refused(sql, params=()):
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql, params)


def _ok(sql, params=()):
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql, params)


def test_pg_mudad_justifications_and_exports_are_guarded(w):
    saudi(w)
    expat(w)  # no IBAN: a suggested justification
    run = approved(w)
    services.generate_wps_file(run, user=w.p, format_code="generic_csv")
    j = MudadJustification.objects.get(run=run)
    _refused("UPDATE payroll_mudadjustification SET status = 'approved' WHERE id = %s", [j.pk])  # skips draft
    _refused("UPDATE payroll_mudadjustification SET run_id = run_id + 1 WHERE id = %s", [j.pk])
    _refused("DELETE FROM payroll_mudadjustification WHERE id = %s", [j.pk])
    _ok("UPDATE payroll_mudadjustification SET description_en = 'edited' WHERE id = %s", [j.pk])
    j = services.withdraw_mudad_justification(j, user=w.p, reason_ar="اختبار")
    _refused("UPDATE payroll_mudadjustification SET description_ar = 'x' WHERE id = %s", [j.pk])  # final
    services.suggest_mudad_justifications(run, user=w.p)
    j2 = MudadJustification.objects.exclude(status="withdrawn").get(run=run)
    services.approve_mudad_justification(services.confirm_mudad_justification(j2, user=w.p), user=w.h)
    export = services.export_mudad_justifications(run, user=w.p)
    _refused("UPDATE payroll_mudadjustificationexport SET sha256 = 'x' WHERE id = %s", [export.pk])
    _refused("DELETE FROM payroll_mudadjustificationexport WHERE id = %s", [export.pk])
    _ok("UPDATE payroll_mudadjustificationexport SET nominated_for_burn_at = now() WHERE id = %s", [export.pk])


def test_pg_liability_reports_are_sealed_and_lines_append_only(w):
    s1 = saudi(w)
    lt = LeaveType.objects.get(entity=w.alm, code="ANNUAL")
    LeaveLedger.objects.create(entity=w.alm, employee=s1, leave_type=lt, date=date(2027, 3, 31), kind="accrual",
                               days=D("12.50"), period="2027-03", source_type="hr_accrue_leave")
    w.pclock.set(datetime(2027, 4, 5, 12, 0))
    first = services.leave_liability_report(w.alm, date(2027, 3, 31), user=w.a)
    line = first.lines.get()
    _refused("UPDATE payroll_payrollliabilityreport SET total_amount = 1 WHERE id = %s", [first.pk])
    _refused("DELETE FROM payroll_payrollliabilityreport WHERE id = %s", [first.pk])
    _refused("UPDATE payroll_payrollliabilityreportline SET amount = 1 WHERE id = %s", [line.pk])
    _refused("DELETE FROM payroll_payrollliabilityreportline WHERE id = %s", [line.pk])
    _refused("INSERT INTO payroll_payrollliabilityreportline (entity_id, report_id, employee_id, employee_code, "
             "employment_basis, jurisdiction, valuation, reason_en, reason_ar, amount) SELECT entity_id, report_id, "
             "employee_id, employee_code, employment_basis, jurisdiction, valuation, reason_en, reason_ar, amount FROM "
             "payroll_payrollliabilityreportline WHERE id = %s", [line.pk])
    second = services.leave_liability_report(w.alm, date(2027, 3, 31), user=w.a)  # supersedes through the service
    first.refresh_from_db()
    assert first.status == "superseded" and first.superseded_by_id == second.pk
    _refused("UPDATE payroll_payrollliabilityreport SET status = 'generated' WHERE id = %s", [first.pk])
