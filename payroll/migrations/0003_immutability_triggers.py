"""Payroll guards in the database (PostgreSQL only), mirroring payroll/models.py for writes that bypass the ORM
(R7, R13, R24, R31). On SQLite this migration does nothing.

- payroll runs: never deleted; entity, period, group, kind and creator never change; status moves only along R7
  (draft -> calculated -> submitted -> returned / approved -> paid / reversed); an approved, paid or reversed run is
  locked except: its journal entry is set once, approved -> paid (status only), approved -> reversed with the
  reversal entry and reverser.
- payslips and payslip lines: no insert, update or delete while their run is approved, paid or reversed.
- EOSB provision entries: append-only.
- loans: never deleted; principal, employee and entity never change once submitted.
- payments: never deleted; an approved payment changes only to receive its journal entry once.
- WPS files: never deleted (kept indefinitely, P19(m)).
"""
from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION payroll_changed_keys(old_row jsonb, new_row jsonb) RETURNS text[] AS $$
    SELECT coalesce(array_agg(n.key ORDER BY n.key), ARRAY[]::text[])
      FROM jsonb_each(new_row) n
     WHERE n.value IS DISTINCT FROM (old_row -> n.key);
$$ LANGUAGE sql IMMUTABLE;

CREATE OR REPLACE FUNCTION payroll_run_locked(run_id bigint) RETURNS boolean AS $$
    SELECT EXISTS (SELECT 1 FROM payroll_payrollrun WHERE id = run_id AND status IN ('approved', 'paid', 'reversed'));
$$ LANGUAGE sql STABLE;

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
            ('returned', 'calculated'), ('submitted', 'approved'), ('approved', 'paid'), ('approved', 'reversed')) THEN
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
    IF OLD.status = 'approved' AND NEW.status = 'reversed' AND NEW.reversal_entry_id IS NOT NULL
            AND changed <@ ARRAY['status', 'reversal_entry_id', 'reversed_by_id', 'reversed_at'] THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'payroll run % is % and locked; corrections are later runs or a reversal (payroll R13)',
        OLD.id, OLD.status;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION payroll_payslip_lock() RETURNS trigger AS $$
DECLARE
    rid bigint;
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') AND payroll_run_locked(OLD.run_id) THEN
        RAISE EXCEPTION 'payslip %: its run is locked (payroll R13)', OLD.id;
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') AND payroll_run_locked(NEW.run_id) THEN
        RAISE EXCEPTION 'payslip: its run is locked (payroll R13)';
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION payroll_line_lock() RETURNS trigger AS $$
DECLARE
    sid bigint;
BEGIN
    sid := CASE WHEN TG_OP = 'DELETE' THEN OLD.payslip_id ELSE NEW.payslip_id END;
    IF EXISTS (SELECT 1 FROM payroll_payslip p WHERE p.id = sid AND payroll_run_locked(p.run_id))
            OR (TG_OP = 'UPDATE' AND EXISTS (SELECT 1 FROM payroll_payslip p WHERE p.id = OLD.payslip_id
                                                                         AND payroll_run_locked(p.run_id))) THEN
        RAISE EXCEPTION 'payslip line: its run is locked (payroll R13)';
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION payroll_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only (payroll R24)', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION payroll_never_deleted() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% rows are never deleted (payroll R13, R31)', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION payroll_loan_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    changed := payroll_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF OLD.status <> 'draft' AND changed && ARRAY['entity_id', 'employee_id', 'principal', 'kind', 'interest_free',
                                                  'created_by_id', 'disbursement_account_id'] THEN
        RAISE EXCEPTION 'loan %: principal, employee and kind never change once submitted (payroll R26)', OLD.id;
    END IF;
    IF OLD.journal_entry_id IS NOT NULL AND changed && ARRAY['journal_entry_id', 'disbursement_date'] THEN
        RAISE EXCEPTION 'loan %: the disbursement is posted and never changes', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

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

DROP TRIGGER IF EXISTS payroll_run_lock ON payroll_payrollrun;
CREATE TRIGGER payroll_run_lock BEFORE INSERT OR UPDATE OR DELETE ON payroll_payrollrun
    FOR EACH ROW EXECUTE FUNCTION payroll_run_lock();
DROP TRIGGER IF EXISTS payroll_payslip_lock ON payroll_payslip;
CREATE TRIGGER payroll_payslip_lock BEFORE INSERT OR UPDATE OR DELETE ON payroll_payslip
    FOR EACH ROW EXECUTE FUNCTION payroll_payslip_lock();
DROP TRIGGER IF EXISTS payroll_line_lock ON payroll_payslipline;
CREATE TRIGGER payroll_line_lock BEFORE INSERT OR UPDATE OR DELETE ON payroll_payslipline
    FOR EACH ROW EXECUTE FUNCTION payroll_line_lock();
DROP TRIGGER IF EXISTS payroll_eosb_append_only ON payroll_eosbprovisionentry;
CREATE TRIGGER payroll_eosb_append_only BEFORE UPDATE OR DELETE ON payroll_eosbprovisionentry
    FOR EACH ROW EXECUTE FUNCTION payroll_append_only();
DROP TRIGGER IF EXISTS payroll_loan_lock ON payroll_employeeloan;
CREATE TRIGGER payroll_loan_lock BEFORE UPDATE ON payroll_employeeloan
    FOR EACH ROW EXECUTE FUNCTION payroll_loan_lock();
DROP TRIGGER IF EXISTS payroll_loan_never_deleted ON payroll_employeeloan;
CREATE TRIGGER payroll_loan_never_deleted BEFORE DELETE ON payroll_employeeloan
    FOR EACH ROW EXECUTE FUNCTION payroll_never_deleted();
DROP TRIGGER IF EXISTS payroll_payment_lock ON payroll_payrollpayment;
CREATE TRIGGER payroll_payment_lock BEFORE UPDATE ON payroll_payrollpayment
    FOR EACH ROW EXECUTE FUNCTION payroll_payment_lock();
DROP TRIGGER IF EXISTS payroll_payment_never_deleted ON payroll_payrollpayment;
CREATE TRIGGER payroll_payment_never_deleted BEFORE DELETE ON payroll_payrollpayment
    FOR EACH ROW EXECUTE FUNCTION payroll_never_deleted();
DROP TRIGGER IF EXISTS payroll_wps_never_deleted ON payroll_wpsfile;
CREATE TRIGGER payroll_wps_never_deleted BEFORE DELETE ON payroll_wpsfile
    FOR EACH ROW EXECUTE FUNCTION payroll_never_deleted();
"""

REVERSE = r"""
DROP TRIGGER IF EXISTS payroll_run_lock ON payroll_payrollrun;
DROP TRIGGER IF EXISTS payroll_payslip_lock ON payroll_payslip;
DROP TRIGGER IF EXISTS payroll_line_lock ON payroll_payslipline;
DROP TRIGGER IF EXISTS payroll_eosb_append_only ON payroll_eosbprovisionentry;
DROP TRIGGER IF EXISTS payroll_loan_lock ON payroll_employeeloan;
DROP TRIGGER IF EXISTS payroll_loan_never_deleted ON payroll_employeeloan;
DROP TRIGGER IF EXISTS payroll_payment_lock ON payroll_payrollpayment;
DROP TRIGGER IF EXISTS payroll_payment_never_deleted ON payroll_payrollpayment;
DROP TRIGGER IF EXISTS payroll_wps_never_deleted ON payroll_wpsfile;
DROP FUNCTION IF EXISTS payroll_run_lock();
DROP FUNCTION IF EXISTS payroll_payslip_lock();
DROP FUNCTION IF EXISTS payroll_line_lock();
DROP FUNCTION IF EXISTS payroll_append_only();
DROP FUNCTION IF EXISTS payroll_never_deleted();
DROP FUNCTION IF EXISTS payroll_loan_lock();
DROP FUNCTION IF EXISTS payroll_payment_lock();
DROP FUNCTION IF EXISTS payroll_run_locked(bigint);
DROP FUNCTION IF EXISTS payroll_changed_keys(jsonb, jsonb);
"""


def forwards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(FORWARD)


def backwards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(REVERSE)


class Migration(migrations.Migration):
    dependencies = [("payroll", "0002_seed_parameters_accounts")]

    operations = [migrations.RunPython(forwards, backwards)]
