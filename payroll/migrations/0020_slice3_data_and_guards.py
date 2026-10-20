"""Payroll slice 3 (docs/specs/payroll.md §11): seed rows, the copy of recorded Mudad justifications and the
PostgreSQL guards of the new tables.

- Parameters (11.3.4, all ``unverified``): ``wps.mudad.justification_codes`` (SA, empty: [verify: Mudad code list])
  and ``leave.encashment_basis`` (EG, empty: OQ-55).
- ``MudadSubmission.justifications`` is read-only from slice 3: its entries are copied into ``MudadJustification`` rows
  (``source=import``, status ``submitted``) when the employee has a payslip in the submission's run; nothing else is
  changed. An entry that names no payslip of the run is left in the JSON (reported by ``manage.py migrate`` output
  only).
- PostgreSQL only (SQLite: nothing):
  - Mudad justifications are never deleted; entity, payslip, run, employee code and creator never change; the status
    moves only along R80 (suggested -> draft -> approved -> submitted -> accepted / rejected; withdrawn before
    submission); an accepted, rejected or withdrawn row never changes.
  - justification exports are never deleted; only the burn nomination is set, once.
  - liability reports are never deleted; once sealed (sha256 set) only ``generated -> superseded`` and
    ``superseded_by`` (once) change; their lines are append-only and none is added to a sealed report.
"""
from datetime import timedelta

from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION payroll_mudad_justification_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Mudad justification % is never deleted (payroll R80)', OLD.id;
    END IF;
    changed := payroll_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF array_length(changed, 1) IS NULL THEN
        RETURN NEW;
    END IF;
    IF OLD.status IN ('accepted', 'rejected', 'withdrawn') THEN
        RAISE EXCEPTION 'Mudad justification % is % and final (payroll R80)', OLD.id, OLD.status;
    END IF;
    IF changed && ARRAY['id', 'entity_id', 'payslip_id', 'run_id', 'employee_code', 'created_by_id', 'created_at'] THEN
        RAISE EXCEPTION 'Mudad justification %: entity, payslip, run, employee and creator never change', OLD.id;
    END IF;
    IF OLD.status <> NEW.status AND (OLD.status, NEW.status) NOT IN (
            ('suggested', 'draft'), ('draft', 'approved'), ('approved', 'submitted'), ('submitted', 'accepted'),
            ('submitted', 'rejected'), ('suggested', 'withdrawn'), ('draft', 'withdrawn'), ('approved', 'withdrawn')) THEN
        RAISE EXCEPTION 'Mudad justification % does not move from % to % (payroll R80)', OLD.id, OLD.status, NEW.status;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION payroll_justification_export_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'justification export % is kept (payroll R31, R81)', OLD.id;
    END IF;
    changed := payroll_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF array_length(changed, 1) IS NULL THEN
        RETURN NEW;
    END IF;
    IF changed <@ ARRAY['nominated_for_burn_at'] AND OLD.nominated_for_burn_at IS NULL THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'justification export % is never rewritten (payroll R81)', OLD.id;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION payroll_liability_report_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'liability report % is kept; a new one supersedes it (payroll R75)', OLD.id;
    END IF;
    IF OLD.sha256 = '' THEN
        RETURN NEW;
    END IF;
    changed := payroll_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF array_length(changed, 1) IS NULL THEN
        RETURN NEW;
    END IF;
    IF changed <@ ARRAY['status', 'superseded_by_id'] AND NEW.status = 'superseded'
            AND (NOT 'status' = ANY(changed) OR OLD.status = 'generated')
            AND (NOT 'superseded_by_id' = ANY(changed) OR OLD.superseded_by_id IS NULL) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'liability report % is never edited; a new report supersedes it (payroll R75)', OLD.id;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION payroll_liability_line_lock() RETURNS trigger AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION '% is append-only (payroll R75)', TG_TABLE_NAME;
    END IF;
    IF EXISTS (SELECT 1 FROM payroll_payrollliabilityreport r WHERE r.id = NEW.report_id AND r.sha256 <> '') THEN
        RAISE EXCEPTION 'liability report % is sealed: no line is added (payroll R75)', NEW.report_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS payroll_mudad_justification_lock ON payroll_mudadjustification;
CREATE TRIGGER payroll_mudad_justification_lock BEFORE UPDATE OR DELETE ON payroll_mudadjustification
    FOR EACH ROW EXECUTE FUNCTION payroll_mudad_justification_lock();
DROP TRIGGER IF EXISTS payroll_justification_export_lock ON payroll_mudadjustificationexport;
CREATE TRIGGER payroll_justification_export_lock BEFORE UPDATE OR DELETE ON payroll_mudadjustificationexport
    FOR EACH ROW EXECUTE FUNCTION payroll_justification_export_lock();
DROP TRIGGER IF EXISTS payroll_liability_report_lock ON payroll_payrollliabilityreport;
CREATE TRIGGER payroll_liability_report_lock BEFORE UPDATE OR DELETE ON payroll_payrollliabilityreport
    FOR EACH ROW EXECUTE FUNCTION payroll_liability_report_lock();
DROP TRIGGER IF EXISTS payroll_liability_line_lock ON payroll_payrollliabilityreportline;
CREATE TRIGGER payroll_liability_line_lock BEFORE INSERT OR UPDATE OR DELETE ON payroll_payrollliabilityreportline
    FOR EACH ROW EXECUTE FUNCTION payroll_liability_line_lock();
"""

REVERSE = r"""
DROP TRIGGER IF EXISTS payroll_mudad_justification_lock ON payroll_mudadjustification;
DROP TRIGGER IF EXISTS payroll_justification_export_lock ON payroll_mudadjustificationexport;
DROP TRIGGER IF EXISTS payroll_liability_report_lock ON payroll_payrollliabilityreport;
DROP TRIGGER IF EXISTS payroll_liability_line_lock ON payroll_payrollliabilityreportline;
DROP FUNCTION IF EXISTS payroll_mudad_justification_lock();
DROP FUNCTION IF EXISTS payroll_justification_export_lock();
DROP FUNCTION IF EXISTS payroll_liability_report_lock();
DROP FUNCTION IF EXISTS payroll_liability_line_lock();
"""

KINDS = ("non_payment", "partial_payment", "amount_difference", "late_payment")
REASONS = ("unpaid_leave", "sick_leave_reduced", "maternity_gosi", "joiner_leaver_proration", "final_settlement",
           "contract_suspended", "court_order_deduction", "bank_rejected", "iban_exception", "paid_off_cycle",
           "payroll_correction", "other")


def _sql(schema_editor, sql):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(sql)  # no parameters: the "%" in RAISE messages is not a placeholder


def seed_parameters(apps, schema_editor):
    from payroll import statutory_seed

    statutory_seed.apply(apps.get_model("hr", "StatutoryParameter"))


def copy_justifications(apps, schema_editor):
    """11.3.2: the entries recorded in ``MudadSubmission.justifications`` become rows (``source=import``,
    ``submitted``). Historical models have no service guard; amounts default to the payslip's when absent."""
    Submission = apps.get_model("payroll", "MudadSubmission")
    Justification = apps.get_model("payroll", "MudadJustification")
    Payslip = apps.get_model("payroll", "Payslip")
    for sub in Submission.objects.exclude(justifications=[]).order_by("pk"):
        for entry in sub.justifications or []:
            if not isinstance(entry, dict):
                continue
            code = str(entry.get("employee_code") or "")
            ps = Payslip.objects.filter(run_id=sub.run_id, employee__employee_code=code).first()
            if ps is None:
                continue
            reason = entry.get("reason") if entry.get("reason") in REASONS else "other"
            kind = entry.get("kind") if entry.get("kind") in KINDS else "non_payment"
            if Justification.objects.filter(payslip=ps, reason=reason).exclude(status="withdrawn").exists():
                continue
            text_ar = str(entry.get("description_ar") or entry.get("text_ar") or entry.get("reason_ar")
                          or "مبرر مستورد من سجل الرفع على مدد")[:500]
            Justification.objects.create(
                entity_id=sub.entity_id, payslip=ps, run_id=sub.run_id, wps_file_id=sub.wps_file_id,
                employee_code=code, kind=kind, reason=reason, mudad_code=(entry.get("mudad_code") or None),
                expected_amount=entry.get("expected_amount") or ps.gross,
                paid_amount=entry.get("paid_amount") or (0 if kind == "non_payment" else ps.gross),
                description_ar=text_ar,
                description_en=str(entry.get("description_en") or entry.get("text_en") or "")[:500],
                evidence_ref=entry.get("evidence_ref") or None, status="submitted", source="import",
                submitted_on=sub.submitted_on, worker_response_due=sub.submitted_on + timedelta(days=3),
                mudad_reference=sub.reference, created_by_id=sub.recorded_by_id)


def forwards_guard(apps, schema_editor):
    _sql(schema_editor, FORWARD)


def backwards_guard(apps, schema_editor):
    _sql(schema_editor, REVERSE)


class Migration(migrations.Migration):
    dependencies = [("payroll", "0019_slice3")]

    operations = [
        migrations.RunPython(seed_parameters, migrations.RunPython.noop),
        migrations.RunPython(copy_justifications, migrations.RunPython.noop),
        migrations.RunPython(forwards_guard, backwards_guard),
    ]
