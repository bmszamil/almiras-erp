"""Billing rules R10, R11, R38, R41, R42 in the database (PostgreSQL only), mirroring the model guards for writes
that bypass the ORM. On SQLite this migration does nothing; the model and queryset guards apply.

- billing_contractversion: created as drafts; only drafts deleted; approved only from draft or submitted, with an
  approver; approved, rejected and withdrawn versions never change (R38).
- billing_contractline, billing_contractpricestep: written or deleted only while their version is a draft (R38).
- billing_billingperiod: never deleted; created ``planned``; a ``generated`` period keeps its dates, lines,
  billed version and document; its only change is to ``failed`` with no document after a ZATCA rejection (R12).
- billing_billingadjustment: never deleted; its computed fields never change; final, discarded, superseded and
  failed corrections never change (R42, R46).
- billing_billingrun: never deleted; changes only while running. billing_billingrunitem, billing_contractevent:
  append-only.
"""
from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION billing_changed_keys(old_row jsonb, new_row jsonb) RETURNS text[] AS $$
    SELECT coalesce(array_agg(n.key ORDER BY n.key), ARRAY[]::text[])
      FROM jsonb_each(new_row) n
     WHERE n.key <> 'updated_at' AND n.value IS DISTINCT FROM (old_row -> n.key);
$$ LANGUAGE sql IMMUTABLE;

CREATE OR REPLACE FUNCTION billing_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only (billing 3.7, 3.8)', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION billing_version_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION 'contract version % is %; only drafts are deleted (billing R38)', OLD.id, OLD.status;
        END IF;
        RETURN OLD;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' OR NEW.approved_by_id IS NOT NULL THEN
            RAISE EXCEPTION 'contract versions are created as drafts (billing R38)';
        END IF;
        RETURN NEW;
    END IF;
    changed := billing_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF changed && ARRAY['id', 'entity_id', 'contract_id', 'version_no', 'created_by_id', 'created_at'] THEN
        RAISE EXCEPTION 'contract version %: entity, contract and number never change', OLD.id;
    END IF;
    IF OLD.status IN ('approved', 'rejected', 'withdrawn') THEN
        IF array_length(changed, 1) IS NOT NULL THEN
            RAISE EXCEPTION 'contract version % is %; it never changes (billing R38)', OLD.id, OLD.status;
        END IF;
    ELSIF NEW.status = 'approved' AND (NEW.approved_by_id IS NULL OR NEW.approved_at IS NULL) THEN
        RAISE EXCEPTION 'contract version % is approved only with an approver (billing R39)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION billing_version_child_lock() RETURNS trigger AS $$
-- TG_ARGV[0] = 'line' | 'step'
DECLARE
    old_status text;
    new_status text;
BEGIN
    IF TG_ARGV[0] = 'line' THEN
        IF TG_OP <> 'INSERT' THEN
            SELECT status INTO old_status FROM billing_contractversion WHERE id = OLD.version_id FOR SHARE;
        END IF;
        IF TG_OP <> 'DELETE' THEN
            SELECT status INTO new_status FROM billing_contractversion WHERE id = NEW.version_id FOR SHARE;
        END IF;
    ELSE
        IF TG_OP <> 'INSERT' THEN
            SELECT v.status INTO old_status FROM billing_contractversion v
              JOIN billing_contractline l ON l.version_id = v.id WHERE l.id = OLD.line_id;
        END IF;
        IF TG_OP <> 'DELETE' THEN
            SELECT v.status INTO new_status FROM billing_contractversion v
              JOIN billing_contractline l ON l.version_id = v.id WHERE l.id = NEW.line_id;
        END IF;
    END IF;
    IF (TG_OP <> 'INSERT' AND old_status IS DISTINCT FROM 'draft')
       OR (TG_OP <> 'DELETE' AND new_status IS DISTINCT FROM 'draft') THEN
        RAISE EXCEPTION 'contract % rows change only while their version is a draft (billing R38)', TG_ARGV[0];
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION billing_period_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'billing periods are never deleted; they are cancelled (billing R10)';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'planned' OR NEW.sales_document_id IS NOT NULL OR NEW.invoice_lines IS NOT NULL THEN
            RAISE EXCEPTION 'billing periods are created planned (billing R10)';
        END IF;
        RETURN NEW;
    END IF;
    changed := billing_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF changed && ARRAY['id', 'entity_id', 'contract_id'] THEN
        RAISE EXCEPTION 'billing period %: entity and contract never change', OLD.id;
    END IF;
    IF OLD.status = 'generated' THEN
        IF changed && ARRAY['period_start', 'period_end', 'full_period_days', 'bill_date', 'proration_factor',
                            'billed_version_id', 'invoice_lines', 'generated_at'] THEN
            RAISE EXCEPTION 'billing period % is generated; its dates, lines and version never change (billing R10, R41)', OLD.id;
        END IF;
        IF NEW.status = 'generated' THEN
            IF NEW.sales_document_id IS DISTINCT FROM OLD.sales_document_id THEN
                RAISE EXCEPTION 'billing period % is billed once (billing R11)', OLD.id;
            END IF;
        ELSIF NEW.status <> 'failed' OR NEW.sales_document_id IS NOT NULL THEN
            RAISE EXCEPTION 'a generated billing period % changes only to failed after a ZATCA rejection (billing R12)', OLD.id;
        END IF;
    ELSIF NEW.status = 'generated' AND (NEW.sales_document_id IS NULL OR NEW.invoice_lines IS NULL
                                        OR NEW.billed_version_id IS NULL) THEN
        RAISE EXCEPTION 'billing period % is generated only with its document, lines and version (billing R52)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION billing_adjustment_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'billing corrections are never deleted (billing R46)';
    END IF;
    changed := billing_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF changed && ARRAY['id', 'entity_id', 'contract_id', 'version_id', 'trigger', 'billing_period_id',
                        'original_document_id', 'kind', 'lines', 'supply_start', 'supply_end', 'created_at'] THEN
        RAISE EXCEPTION 'billing correction %: computed amounts are read-only (billing R42, OQ-26)', OLD.id;
    END IF;
    IF OLD.status IN ('final', 'discarded', 'superseded', 'failed') AND array_length(changed, 1) IS NOT NULL THEN
        RAISE EXCEPTION 'billing correction % is %; it never changes', OLD.id, OLD.status;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION billing_run_lock() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' OR OLD.status <> 'running' THEN
        RAISE EXCEPTION 'billing run % is finished; it never changes (billing 3.7)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER billing_contractversion_lock BEFORE INSERT OR UPDATE OR DELETE ON billing_contractversion
    FOR EACH ROW EXECUTE FUNCTION billing_version_lock();
CREATE TRIGGER billing_contractline_lock BEFORE INSERT OR UPDATE OR DELETE ON billing_contractline
    FOR EACH ROW EXECUTE FUNCTION billing_version_child_lock('line');
CREATE TRIGGER billing_contractpricestep_lock BEFORE INSERT OR UPDATE OR DELETE ON billing_contractpricestep
    FOR EACH ROW EXECUTE FUNCTION billing_version_child_lock('step');
CREATE TRIGGER billing_billingperiod_lock BEFORE INSERT OR UPDATE OR DELETE ON billing_billingperiod
    FOR EACH ROW EXECUTE FUNCTION billing_period_lock();
CREATE TRIGGER billing_billingadjustment_lock BEFORE UPDATE OR DELETE ON billing_billingadjustment
    FOR EACH ROW EXECUTE FUNCTION billing_adjustment_lock();
CREATE TRIGGER billing_billingrun_lock BEFORE UPDATE OR DELETE ON billing_billingrun
    FOR EACH ROW EXECUTE FUNCTION billing_run_lock();
CREATE TRIGGER billing_billingrunitem_append_only BEFORE UPDATE OR DELETE ON billing_billingrunitem
    FOR EACH ROW EXECUTE FUNCTION billing_append_only();
CREATE TRIGGER billing_contractevent_append_only BEFORE UPDATE OR DELETE ON billing_contractevent
    FOR EACH ROW EXECUTE FUNCTION billing_append_only();
"""

REVERSE = r"""
DROP TRIGGER IF EXISTS billing_contractevent_append_only ON billing_contractevent;
DROP TRIGGER IF EXISTS billing_billingrunitem_append_only ON billing_billingrunitem;
DROP TRIGGER IF EXISTS billing_billingrun_lock ON billing_billingrun;
DROP TRIGGER IF EXISTS billing_billingadjustment_lock ON billing_billingadjustment;
DROP TRIGGER IF EXISTS billing_billingperiod_lock ON billing_billingperiod;
DROP TRIGGER IF EXISTS billing_contractpricestep_lock ON billing_contractpricestep;
DROP TRIGGER IF EXISTS billing_contractline_lock ON billing_contractline;
DROP TRIGGER IF EXISTS billing_contractversion_lock ON billing_contractversion;
DROP FUNCTION IF EXISTS billing_run_lock();
DROP FUNCTION IF EXISTS billing_adjustment_lock();
DROP FUNCTION IF EXISTS billing_period_lock();
DROP FUNCTION IF EXISTS billing_version_child_lock();
DROP FUNCTION IF EXISTS billing_version_lock();
DROP FUNCTION IF EXISTS billing_append_only();
DROP FUNCTION IF EXISTS billing_changed_keys(jsonb, jsonb);
"""


def _run(schema_editor, sql):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(sql)  # no parameters


def forwards(apps, schema_editor):
    _run(schema_editor, FORWARD)


def backwards(apps, schema_editor):
    _run(schema_editor, REVERSE)


class Migration(migrations.Migration):
    dependencies = [("billing", "0001_initial")]

    operations = [migrations.RunPython(forwards, backwards)]
