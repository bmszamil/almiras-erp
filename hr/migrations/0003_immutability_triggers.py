"""HR rules R7, R15, R29, R40, R24/R25, R41 in the database (PostgreSQL only), mirroring the model guards for
writes that bypass the ORM. On SQLite this migration does nothing; the model and queryset guards apply.

- hr_statutoryparameter: never deleted; once used by an approval only status, verifier and use time change (R7).
- hr_employmentcontract: created as drafts; only drafts deleted; an approved version changes only its
  ``ended_on`` (termination R38 / withdrawal R41); rejected and withdrawn versions never change (R15).
- hr_contractcomponent: written only while its contract version is a draft (R15).
- hr_leaveledger, hr_sickleaveday, hr_eosbcalculation: append-only (R29, R32, R40).
- hr_assignmentevent: never deleted; only the delivery fields change, and a delivered event never changes (R25).
- hr_termination: only drafts deleted; an approved termination changes only to ``withdrawn`` (R41).
- hr_employee: only drafts deleted (R14).
"""
from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION hr_changed_keys(old_row jsonb, new_row jsonb) RETURNS text[] AS $$
    SELECT coalesce(array_agg(n.key ORDER BY n.key), ARRAY[]::text[])
      FROM jsonb_each(new_row) n
     WHERE n.value IS DISTINCT FROM (old_row -> n.key);
$$ LANGUAGE sql IMMUTABLE;

CREATE OR REPLACE FUNCTION hr_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only (hr R29, R32, R40)', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION hr_parameter_lock() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'statutory parameters are never deleted (hr R7)';
    END IF;
    IF OLD.used_at IS NOT NULL AND NOT hr_changed_keys(to_jsonb(OLD), to_jsonb(NEW))
            <@ ARRAY['status', 'verified_by_id', 'verified_at', 'used_at'] THEN
        RAISE EXCEPTION 'statutory parameter % was used by an approval and is never edited (hr R7)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION hr_contract_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION 'contract version % is %; only drafts are deleted (hr R15)', OLD.id, OLD.status;
        END IF;
        RETURN OLD;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' OR NEW.approved_by_id IS NOT NULL THEN
            RAISE EXCEPTION 'contract versions are created as drafts (hr R15)';
        END IF;
        RETURN NEW;
    END IF;
    changed := hr_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF changed && ARRAY['id', 'entity_id', 'employee_id', 'version_no', 'created_by_id', 'created_at'] THEN
        RAISE EXCEPTION 'contract version %: entity, employee and version never change (hr R20)', OLD.id;
    END IF;
    IF OLD.status = 'approved' THEN
        IF NOT changed <@ ARRAY['ended_on'] THEN
            RAISE EXCEPTION 'approved contract version % is immutable (hr R15)', OLD.id;
        END IF;
    ELSIF OLD.status IN ('rejected', 'withdrawn') THEN
        IF array_length(changed, 1) IS NOT NULL THEN
            RAISE EXCEPTION 'contract version % is %; it never changes', OLD.id, OLD.status;
        END IF;
    ELSIF NEW.status = 'approved' AND (NEW.approved_by_id IS NULL OR OLD.status <> 'submitted') THEN
        RAISE EXCEPTION 'contract version % is approved only from submitted, with an approver (hr R15)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION hr_component_lock() RETURNS trigger AS $$
DECLARE
    st text;
BEGIN
    SELECT status INTO st FROM hr_employmentcontract
     WHERE id = CASE WHEN TG_OP = 'DELETE' THEN OLD.contract_id ELSE NEW.contract_id END FOR SHARE;
    IF st IS DISTINCT FROM 'draft' THEN
        RAISE EXCEPTION 'contract components change only while the version is a draft (hr R15)';
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.contract_id <> NEW.contract_id THEN
        RAISE EXCEPTION 'a contract component never moves to another version';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION hr_event_lock() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'assignment events are never deleted (hr R24)';
    END IF;
    IF OLD.delivery_status = 'delivered' OR NOT hr_changed_keys(to_jsonb(OLD), to_jsonb(NEW))
            <@ ARRAY['delivery_status', 'attempts', 'last_error_en', 'last_error_ar', 'billing_assignment_id',
                     'delivered_at'] THEN
        RAISE EXCEPTION 'assignment event %: only its delivery changes, and never after delivery (hr R25)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION hr_termination_lock() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION 'termination % is %; only drafts are deleted (hr R41)', OLD.id, OLD.status;
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status = 'withdrawn' THEN
        RAISE EXCEPTION 'withdrawn termination % never changes', OLD.id;
    END IF;
    IF OLD.status = 'approved' AND (NEW.status <> 'withdrawn' OR NOT hr_changed_keys(to_jsonb(OLD), to_jsonb(NEW))
            <@ ARRAY['status', 'withdrawn_by_id', 'withdrawn_at', 'modified_by_id', 'modified_at']) THEN
        RAISE EXCEPTION 'approved termination % changes only by withdrawal (hr R41)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION hr_employee_delete_lock() RETURNS trigger AS $$
BEGIN
    IF OLD.status <> 'draft' THEN
        RAISE EXCEPTION 'employee % is %; only drafts are deleted (hr R14)', OLD.id, OLD.status;
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER hr_parameter_lock BEFORE UPDATE OR DELETE ON hr_statutoryparameter
    FOR EACH ROW EXECUTE FUNCTION hr_parameter_lock();
CREATE TRIGGER hr_contract_lock BEFORE INSERT OR UPDATE OR DELETE ON hr_employmentcontract
    FOR EACH ROW EXECUTE FUNCTION hr_contract_lock();
CREATE TRIGGER hr_component_lock BEFORE INSERT OR UPDATE OR DELETE ON hr_contractcomponent
    FOR EACH ROW EXECUTE FUNCTION hr_component_lock();
CREATE TRIGGER hr_leaveledger_append_only BEFORE UPDATE OR DELETE ON hr_leaveledger
    FOR EACH ROW EXECUTE FUNCTION hr_append_only();
CREATE TRIGGER hr_sickleaveday_append_only BEFORE UPDATE OR DELETE ON hr_sickleaveday
    FOR EACH ROW EXECUTE FUNCTION hr_append_only();
CREATE TRIGGER hr_eosbcalculation_append_only BEFORE UPDATE OR DELETE ON hr_eosbcalculation
    FOR EACH ROW EXECUTE FUNCTION hr_append_only();
CREATE TRIGGER hr_event_lock BEFORE UPDATE OR DELETE ON hr_assignmentevent
    FOR EACH ROW EXECUTE FUNCTION hr_event_lock();
CREATE TRIGGER hr_termination_lock BEFORE UPDATE OR DELETE ON hr_termination
    FOR EACH ROW EXECUTE FUNCTION hr_termination_lock();
CREATE TRIGGER hr_employee_delete_lock BEFORE DELETE ON hr_employee
    FOR EACH ROW EXECUTE FUNCTION hr_employee_delete_lock();
"""

REVERSE = r"""
DROP TRIGGER IF EXISTS hr_parameter_lock ON hr_statutoryparameter;
DROP TRIGGER IF EXISTS hr_contract_lock ON hr_employmentcontract;
DROP TRIGGER IF EXISTS hr_component_lock ON hr_contractcomponent;
DROP TRIGGER IF EXISTS hr_leaveledger_append_only ON hr_leaveledger;
DROP TRIGGER IF EXISTS hr_sickleaveday_append_only ON hr_sickleaveday;
DROP TRIGGER IF EXISTS hr_eosbcalculation_append_only ON hr_eosbcalculation;
DROP TRIGGER IF EXISTS hr_event_lock ON hr_assignmentevent;
DROP TRIGGER IF EXISTS hr_termination_lock ON hr_termination;
DROP TRIGGER IF EXISTS hr_employee_delete_lock ON hr_employee;
DROP FUNCTION IF EXISTS hr_parameter_lock();
DROP FUNCTION IF EXISTS hr_contract_lock();
DROP FUNCTION IF EXISTS hr_component_lock();
DROP FUNCTION IF EXISTS hr_append_only();
DROP FUNCTION IF EXISTS hr_event_lock();
DROP FUNCTION IF EXISTS hr_termination_lock();
DROP FUNCTION IF EXISTS hr_employee_delete_lock();
DROP FUNCTION IF EXISTS hr_changed_keys(jsonb, jsonb);
"""


def _run(schema_editor, sql):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(sql)


class Migration(migrations.Migration):
    dependencies = [("hr", "0002_seed_statutory_parameters")]

    operations = [migrations.RunPython(lambda a, s: _run(s, FORWARD), lambda a, s: _run(s, REVERSE))]
