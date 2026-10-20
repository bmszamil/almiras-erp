"""Payroll slice 2 guards in the database (PostgreSQL only; nothing on SQLite), mirroring payroll/models.py for writes
that bypass the ORM (CLAUDE.md rule 1; payroll §10.8):

- payroll runs and final settlements: a reversed payment moves them paid -> approved (status only).
- payments: never deleted; an approved payment changes only to receive its journal entry once, or approved ->
  reversed with the reversal entry, reverser, time and reason.
- loan installments: outstanding_after_exit -> written_off (status only); written_off is final.
- settlement corrections: never deleted; once submitted only submitted -> returned (reason), -> approved (number,
  approver, time, warnings) and the journal entry once; lines locked once the correction is submitted.
- loan write-offs and GOSI fines: never deleted; once approved only the journal entry is set (once).
- employee cost items: never deleted once approved and never changed.
- secondment cost reports: never deleted; once approved only approved -> superseded (status only); lines locked once
  the report is submitted.
"""
from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION payroll_run_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'payroll run % is never deleted (payroll R7, R13)', OLD.id;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' THEN
            RAISE EXCEPTION 'a payroll run is created as a draft (payroll R7)';
        END IF;
        RETURN NEW;
    END IF;
    changed := payroll_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF array_length(changed, 1) IS NULL THEN
        RETURN NEW;
    END IF;
    IF changed && ARRAY['id', 'entity_id', 'period', 'payroll_group', 'kind', 'created_by_id', 'created_at'] THEN
        RAISE EXCEPTION 'payroll run %: entity, period, group, kind and creator never change', OLD.id;
    END IF;
    IF OLD.status <> NEW.status AND (OLD.status, NEW.status) NOT IN (
            ('draft', 'calculated'), ('calculated', 'submitted'), ('submitted', 'returned'),
            ('returned', 'calculated'), ('submitted', 'approved'), ('approved', 'paid'), ('approved', 'reversed'),
            ('paid', 'approved')) THEN
        RAISE EXCEPTION 'payroll run % does not move from % to % (payroll R7)', OLD.id, OLD.status, NEW.status;
    END IF;
    IF OLD.status NOT IN ('approved', 'paid', 'reversed') THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'approved' AND NEW.status = 'approved' AND changed <@ ARRAY['journal_entry_id']
            AND OLD.journal_entry_id IS NULL THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'approved' AND NEW.status = 'paid' AND changed <@ ARRAY['status'] THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'paid' AND NEW.status = 'approved' AND changed <@ ARRAY['status']
            AND EXISTS (SELECT 1 FROM payroll_payrollpayment p WHERE p.run_id = OLD.id AND p.status = 'reversed') THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'approved' AND NEW.status = 'reversed' AND NEW.reversal_entry_id IS NOT NULL
            AND changed <@ ARRAY['status', 'reversal_entry_id', 'reversed_by_id', 'reversed_at'] THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'payroll run % is % and locked; corrections are later runs or a reversal (payroll R13)',
        OLD.id, OLD.status;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION payroll_settlement_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
    audit text[] := ARRAY['modified_by_id', 'modified_at'];
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'final settlement % is never deleted (payroll R34)', OLD.id;
    END IF;
    IF OLD.status NOT IN ('submitted', 'approved', 'paid') THEN
        RETURN NEW;
    END IF;
    changed := payroll_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF array_length(changed, 1) IS NULL THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'submitted' AND NEW.status = 'returned'
            AND changed <@ (ARRAY['status', 'returned_reason_ar'] || audit) THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'submitted' AND NEW.status = 'discarded' AND changed <@ (ARRAY['status'] || audit) THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'submitted' AND NEW.status = 'approved' AND OLD.number = '' AND NEW.number <> ''
            AND changed <@ ARRAY['status', 'number', 'approved_by_id', 'approved_at'] THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'approved' AND NEW.status = 'approved' AND changed <@ ARRAY['journal_entry_id']
            AND OLD.journal_entry_id IS NULL THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'approved' AND NEW.status = 'paid' AND changed <@ ARRAY['status'] THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'paid' AND NEW.status = 'approved' AND changed <@ ARRAY['status']
            AND EXISTS (SELECT 1 FROM payroll_payrollpayment p WHERE p.final_settlement_id = OLD.id
                        AND p.status = 'reversed') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'final settlement % is % and locked (payroll R34, re-review 3)', OLD.id, OLD.status;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION payroll_payment_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    changed := payroll_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF OLD.status = 'reversed' AND array_length(changed, 1) IS NOT NULL THEN
        RAISE EXCEPTION 'payroll payment % is reversed and locked (payroll R29)', OLD.id;
    END IF;
    IF OLD.status = 'approved' AND array_length(changed, 1) IS NOT NULL
            AND NOT (changed <@ ARRAY['journal_entry_id'] AND OLD.journal_entry_id IS NULL)
            AND NOT (NEW.status = 'reversed' AND NEW.reversal_entry_id IS NOT NULL AND OLD.reversal_entry_id IS NULL
                     AND changed <@ ARRAY['status', 'reversal_entry_id', 'reversed_by_id', 'reversed_at',
                                          'reversal_reason']) THEN
        RAISE EXCEPTION 'payroll payment % is approved and locked (payroll R29)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION payroll_installment_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status IN ('deducted', 'settled_in_final', 'outstanding_after_exit', 'written_off') THEN
            RAISE EXCEPTION '% %: a deducted installment is never deleted (payroll R18, R13)', TG_TABLE_NAME, OLD.id;
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status NOT IN ('deducted', 'settled_in_final', 'outstanding_after_exit', 'written_off') THEN
        RETURN NEW;
    END IF;
    changed := payroll_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF array_length(changed, 1) IS NULL THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'deducted' AND NEW.status = 'planned' AND NEW.deducted_amount = 0 AND NEW.run_id IS NULL
            AND changed <@ ARRAY['status', 'deducted_amount', 'run_id']
            AND EXISTS (SELECT 1 FROM payroll_payrollrun r WHERE r.id = OLD.run_id AND r.status = 'reversed') THEN
        RETURN NEW;
    END IF;
    IF TG_TABLE_NAME = 'payroll_loaninstallment' AND OLD.status = 'outstanding_after_exit'
            AND NEW.status = 'written_off' AND changed <@ ARRAY['status'] THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION '% %: a deducted installment changes only when its run is reversed (payroll R13, R18)',
        TG_TABLE_NAME, OLD.id;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION payroll_correction_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
    audit text[] := ARRAY['modified_by_id', 'modified_at'];
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'settlement correction % is never deleted', OLD.id;
    END IF;
    IF OLD.status NOT IN ('submitted', 'approved') THEN
        RETURN NEW;
    END IF;
    changed := payroll_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF array_length(changed, 1) IS NULL THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'submitted' AND NEW.status = 'returned'
            AND changed <@ (ARRAY['status', 'returned_reason_ar'] || audit) THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'submitted' AND NEW.status = 'approved' AND OLD.number = '' AND NEW.number <> ''
            AND changed <@ ARRAY['status', 'number', 'approved_by_id', 'approved_at', 'warnings'] THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'approved' AND NEW.status = 'approved' AND changed <@ ARRAY['journal_entry_id']
            AND OLD.journal_entry_id IS NULL THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'settlement correction % is % and locked', OLD.id, OLD.status;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION payroll_correction_line_lock() RETURNS trigger AS $$
DECLARE
    cid bigint;
BEGIN
    cid := CASE WHEN TG_OP = 'DELETE' THEN OLD.correction_id ELSE NEW.correction_id END;
    IF EXISTS (SELECT 1 FROM payroll_settlementcorrection c WHERE c.id = cid AND c.status IN ('submitted', 'approved'))
            OR (TG_OP = 'UPDATE' AND EXISTS (SELECT 1 FROM payroll_settlementcorrection c WHERE c.id = OLD.correction_id
                                             AND c.status IN ('submitted', 'approved'))) THEN
        RAISE EXCEPTION 'settlement correction line: its correction is locked';
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION payroll_posted_document_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION '% % is never deleted (payroll slice 2)', TG_TABLE_NAME, OLD.id;
    END IF;
    IF OLD.status <> 'approved' THEN
        RETURN NEW;
    END IF;
    changed := payroll_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF array_length(changed, 1) IS NULL OR (changed <@ ARRAY['journal_entry_id'] AND OLD.journal_entry_id IS NULL) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION '% % is approved and locked (payroll slice 2)', TG_TABLE_NAME, OLD.id;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION payroll_cost_item_lock() RETURNS trigger AS $$
BEGIN
    IF OLD.status = 'approved' THEN
        RAISE EXCEPTION 'employee cost item % is approved and locked (payroll R63)', OLD.id;
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION payroll_cost_report_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'secondment cost report % is never deleted (payroll R35)', OLD.id;
    END IF;
    IF OLD.status NOT IN ('approved', 'superseded') THEN
        RETURN NEW;
    END IF;
    changed := payroll_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF array_length(changed, 1) IS NULL
            OR (OLD.status = 'approved' AND NEW.status = 'superseded' AND changed <@ ARRAY['status']) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'secondment cost report % is % and locked (payroll R35)', OLD.id, OLD.status;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION payroll_cost_line_lock() RETURNS trigger AS $$
DECLARE
    rid bigint;
BEGIN
    rid := CASE WHEN TG_OP = 'DELETE' THEN OLD.report_id ELSE NEW.report_id END;
    IF EXISTS (SELECT 1 FROM payroll_secondmentcostreport r WHERE r.id = rid
               AND r.status IN ('submitted', 'approved', 'superseded'))
            OR (TG_OP = 'UPDATE' AND EXISTS (SELECT 1 FROM payroll_secondmentcostreport r WHERE r.id = OLD.report_id
                                             AND r.status IN ('submitted', 'approved', 'superseded'))) THEN
        RAISE EXCEPTION 'secondment cost line: its report is locked (payroll R35)';
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS payroll_correction_lock ON payroll_settlementcorrection;
CREATE TRIGGER payroll_correction_lock BEFORE UPDATE OR DELETE ON payroll_settlementcorrection
    FOR EACH ROW EXECUTE FUNCTION payroll_correction_lock();
DROP TRIGGER IF EXISTS payroll_correction_line_lock ON payroll_settlementcorrectionline;
CREATE TRIGGER payroll_correction_line_lock BEFORE INSERT OR UPDATE OR DELETE ON payroll_settlementcorrectionline
    FOR EACH ROW EXECUTE FUNCTION payroll_correction_line_lock();
DROP TRIGGER IF EXISTS payroll_write_off_lock ON payroll_loanwriteoff;
CREATE TRIGGER payroll_write_off_lock BEFORE UPDATE OR DELETE ON payroll_loanwriteoff
    FOR EACH ROW EXECUTE FUNCTION payroll_posted_document_lock();
DROP TRIGGER IF EXISTS payroll_gosi_fine_lock ON payroll_gosifine;
CREATE TRIGGER payroll_gosi_fine_lock BEFORE UPDATE OR DELETE ON payroll_gosifine
    FOR EACH ROW EXECUTE FUNCTION payroll_posted_document_lock();
DROP TRIGGER IF EXISTS payroll_cost_item_lock ON payroll_employeecostitem;
CREATE TRIGGER payroll_cost_item_lock BEFORE UPDATE OR DELETE ON payroll_employeecostitem
    FOR EACH ROW EXECUTE FUNCTION payroll_cost_item_lock();
DROP TRIGGER IF EXISTS payroll_cost_report_lock ON payroll_secondmentcostreport;
CREATE TRIGGER payroll_cost_report_lock BEFORE UPDATE OR DELETE ON payroll_secondmentcostreport
    FOR EACH ROW EXECUTE FUNCTION payroll_cost_report_lock();
DROP TRIGGER IF EXISTS payroll_cost_line_lock ON payroll_secondmentcostline;
CREATE TRIGGER payroll_cost_line_lock BEFORE INSERT OR UPDATE OR DELETE ON payroll_secondmentcostline
    FOR EACH ROW EXECUTE FUNCTION payroll_cost_line_lock();
"""

# The run, settlement, payment and installment functions as before (0003, 0011, 0010) and no slice 2 triggers.
REVERSE = r"""
DROP TRIGGER IF EXISTS payroll_correction_lock ON payroll_settlementcorrection;
DROP TRIGGER IF EXISTS payroll_correction_line_lock ON payroll_settlementcorrectionline;
DROP TRIGGER IF EXISTS payroll_write_off_lock ON payroll_loanwriteoff;
DROP TRIGGER IF EXISTS payroll_gosi_fine_lock ON payroll_gosifine;
DROP TRIGGER IF EXISTS payroll_cost_item_lock ON payroll_employeecostitem;
DROP TRIGGER IF EXISTS payroll_cost_report_lock ON payroll_secondmentcostreport;
DROP TRIGGER IF EXISTS payroll_cost_line_lock ON payroll_secondmentcostline;
DROP FUNCTION IF EXISTS payroll_correction_lock();
DROP FUNCTION IF EXISTS payroll_correction_line_lock();
DROP FUNCTION IF EXISTS payroll_posted_document_lock();
DROP FUNCTION IF EXISTS payroll_cost_item_lock();
DROP FUNCTION IF EXISTS payroll_cost_report_lock();
DROP FUNCTION IF EXISTS payroll_cost_line_lock();

CREATE OR REPLACE FUNCTION payroll_payment_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    changed := payroll_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF OLD.status = 'approved' AND NOT (changed <@ ARRAY['journal_entry_id'] AND OLD.journal_entry_id IS NULL) THEN
        RAISE EXCEPTION 'payroll payment % is approved and locked (payroll R29)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def _run(schema_editor, sql):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(sql)  # no parameters: the % in the RAISE messages are PL/pgSQL placeholders


def forwards(apps, schema_editor):
    _run(schema_editor, FORWARD)


def backwards(apps, schema_editor):
    # The run, settlement and installment functions keep their slice 2 bodies on the way back (they only allow more
    # moves for rows that slice 2 created); the payment function and the new triggers are restored / dropped.
    _run(schema_editor, REVERSE)


class Migration(migrations.Migration):
    dependencies = [("payroll", "0013_slice2_documents")]

    operations = [migrations.RunPython(forwards, backwards)]
