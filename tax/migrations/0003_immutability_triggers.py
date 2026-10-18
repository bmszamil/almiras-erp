"""Tax guards in the database (PostgreSQL only), mirroring the model and queryset guards of tax/models.py for writes
that bypass the ORM (tax-returns R3, R7, R10, R32, R36). On SQLite this migration does nothing.

- VAT returns: never deleted; the entity never changes; an approved, filed, settled or amended return is locked
  (R7): approved -> filed only with the filing fields, approved gets its T1 entry once, filed <-> settled and
  filed / settled -> amended change the status only, amended never changes.
- boxes, source lines, manual adjustments (and the journal lines they explain), exception acknowledgements: no
  insert, update or delete while their return is locked (R7, R10).
- import VAT records, tax payments, refunds: never deleted; a posted document gets its journal entry once or
  becomes reversed with its reversal entry; a reversed one never changes (R36).
- corrections: never deleted; filed and cancelled ones never change; correcting entries are linked only while the
  correction is open and never unlinked (R26).
- alerts: append-only. Settings: never deleted.
- return periods: never overlap per entity (R3); a period that has a return never changes.
"""
from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION tax_changed_keys(old_row jsonb, new_row jsonb) RETURNS text[] AS $$
    SELECT coalesce(array_agg(n.key ORDER BY n.key), ARRAY[]::text[])
      FROM jsonb_each(new_row) n
     WHERE n.value IS DISTINCT FROM (old_row -> n.key);
$$ LANGUAGE sql IMMUTABLE;

CREATE OR REPLACE FUNCTION tax_return_locked(return_id bigint) RETURNS boolean AS $$
    SELECT EXISTS (SELECT 1 FROM tax_vatreturn
                    WHERE id = return_id AND status IN ('approved', 'filed', 'settled', 'amended'));
$$ LANGUAGE sql STABLE;

CREATE OR REPLACE FUNCTION tax_vatreturn_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'VAT return % is never deleted (tax R7, R32)', OLD.id;
    END IF;
    changed := tax_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF array_length(changed, 1) IS NULL THEN
        RETURN NEW;
    END IF;
    IF changed && ARRAY['id', 'entity_id', 'created_by_id', 'created_at'] THEN
        RAISE EXCEPTION 'VAT return %: entity and creator never change', OLD.id;
    END IF;
    IF OLD.status NOT IN ('approved', 'filed', 'settled', 'amended') THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'approved' AND NEW.status = 'approved' AND changed <@ ARRAY['settlement_entry_id']
            AND OLD.settlement_entry_id IS NULL THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'approved' AND NEW.status = 'filed'
            AND changed <@ ARRAY['status', 'filed_by_id', 'filed_at', 'zatca_filing_reference', 'filed_on',
                                 'acknowledgement', 'sadad_bill_number'] THEN
        RETURN NEW;
    END IF;
    IF (OLD.status, NEW.status) IN (('filed', 'settled'), ('settled', 'filed'), ('filed', 'amended'),
                                    ('settled', 'amended')) AND changed <@ ARRAY['status'] THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'VAT return % is % and locked; it is never recomputed or edited (tax R7)', OLD.id, OLD.status;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION tax_return_child_lock() RETURNS trigger AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') AND tax_return_locked(OLD.vat_return_id) THEN
        RAISE EXCEPTION '%: VAT return % is locked (tax R7, R10)', TG_TABLE_NAME, OLD.vat_return_id;
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') AND tax_return_locked(NEW.vat_return_id) THEN
        RAISE EXCEPTION '%: VAT return % is locked (tax R7, R10)', TG_TABLE_NAME, NEW.vat_return_id;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION tax_adjustment_lines_lock() RETURNS trigger AS $$
DECLARE
    adj bigint;
BEGIN
    adj := CASE WHEN TG_OP = 'DELETE' THEN OLD.vatmanualadjustment_id ELSE NEW.vatmanualadjustment_id END;
    IF EXISTS (SELECT 1 FROM tax_vatmanualadjustment a WHERE a.id = adj AND tax_return_locked(a.vat_return_id)) THEN
        RAISE EXCEPTION 'manual adjustment %: its VAT return is locked (tax R7)', adj;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION tax_document_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION '% % is never deleted (tax R32, R36)', TG_TABLE_NAME, OLD.id;
    END IF;
    changed := tax_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF array_length(changed, 1) IS NULL THEN
        RETURN NEW;
    END IF;
    IF changed && ARRAY['id', 'entity_id', 'created_by_id', 'created_at'] THEN
        RAISE EXCEPTION '% %: entity and creator never change', TG_TABLE_NAME, OLD.id;
    END IF;
    IF OLD.status = 'reversed' THEN
        RAISE EXCEPTION '% % is reversed; it never changes', TG_TABLE_NAME, OLD.id;
    END IF;
    IF OLD.status = 'posted' THEN
        IF changed <@ ARRAY['journal_entry_id'] AND OLD.journal_entry_id IS NULL THEN
            RETURN NEW;
        END IF;
        IF changed <@ ARRAY['status', 'reversal_entry_id'] AND NEW.status = 'reversed'
                AND NEW.reversal_entry_id IS NOT NULL THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION '% % is posted; it is corrected only by reversal (tax R36)', TG_TABLE_NAME, OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION tax_correction_lock() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'VAT correction % is never deleted', OLD.id;
    END IF;
    IF OLD.status IN ('filed', 'cancelled') AND to_jsonb(OLD) IS DISTINCT FROM to_jsonb(NEW) THEN
        RAISE EXCEPTION 'VAT correction % is %; it never changes', OLD.id, OLD.status;
    END IF;
    IF NEW.entity_id IS DISTINCT FROM OLD.entity_id THEN
        RAISE EXCEPTION 'VAT correction %: the entity never changes', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION tax_correction_entries_lock() RETURNS trigger AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'correcting entries are never unlinked or changed (tax R26)';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM tax_vatcorrection c WHERE c.id = NEW.vatcorrection_id AND c.status = 'open') THEN
        RAISE EXCEPTION 'correcting entries are linked only while the correction is open (tax R26)';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION tax_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION tax_no_delete() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% rows are never deleted', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION tax_schedule_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND EXISTS (SELECT 1 FROM tax_vatreturn r WHERE r.schedule_id = OLD.id)
            AND to_jsonb(OLD) IS DISTINCT FROM to_jsonb(NEW) THEN
        RAISE EXCEPTION 'VAT return period % has a return; it never changes (tax R3)', OLD.id;
    END IF;
    IF EXISTS (SELECT 1 FROM tax_vatreturnschedule s
                WHERE s.entity_id = NEW.entity_id AND s.id <> NEW.id
                  AND s.period_start <= NEW.period_end AND s.period_end >= NEW.period_start) THEN
        RAISE EXCEPTION 'VAT return periods of entity % never overlap (tax R3)', NEW.entity_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER tax_vatreturn_lock BEFORE UPDATE OR DELETE ON tax_vatreturn
    FOR EACH ROW EXECUTE FUNCTION tax_vatreturn_lock();
CREATE TRIGGER tax_vatreturnbox_lock BEFORE INSERT OR UPDATE OR DELETE ON tax_vatreturnbox
    FOR EACH ROW EXECUTE FUNCTION tax_return_child_lock();
CREATE TRIGGER tax_vatreturnsourceline_lock BEFORE INSERT OR UPDATE OR DELETE ON tax_vatreturnsourceline
    FOR EACH ROW EXECUTE FUNCTION tax_return_child_lock();
CREATE TRIGGER tax_vatmanualadjustment_lock BEFORE INSERT OR UPDATE OR DELETE ON tax_vatmanualadjustment
    FOR EACH ROW EXECUTE FUNCTION tax_return_child_lock();
CREATE TRIGGER tax_vatexceptionack_lock BEFORE INSERT OR UPDATE OR DELETE ON tax_vatexceptionack
    FOR EACH ROW EXECUTE FUNCTION tax_return_child_lock();
CREATE TRIGGER tax_adjustment_lines_lock BEFORE INSERT OR UPDATE OR DELETE ON tax_vatmanualadjustment_journal_lines
    FOR EACH ROW EXECUTE FUNCTION tax_adjustment_lines_lock();
CREATE TRIGGER tax_importvatrecord_lock BEFORE UPDATE OR DELETE ON tax_importvatrecord
    FOR EACH ROW EXECUTE FUNCTION tax_document_lock();
CREATE TRIGGER tax_taxpayment_lock BEFORE UPDATE OR DELETE ON tax_taxpayment
    FOR EACH ROW EXECUTE FUNCTION tax_document_lock();
CREATE TRIGGER tax_taxrefund_lock BEFORE UPDATE OR DELETE ON tax_taxrefund
    FOR EACH ROW EXECUTE FUNCTION tax_document_lock();
CREATE TRIGGER tax_vatcorrection_lock BEFORE UPDATE OR DELETE ON tax_vatcorrection
    FOR EACH ROW EXECUTE FUNCTION tax_correction_lock();
CREATE TRIGGER tax_correction_entries_lock BEFORE INSERT OR UPDATE OR DELETE ON tax_vatcorrection_correcting_entries
    FOR EACH ROW EXECUTE FUNCTION tax_correction_entries_lock();
CREATE TRIGGER tax_taxalert_append_only BEFORE UPDATE OR DELETE ON tax_taxalert
    FOR EACH ROW EXECUTE FUNCTION tax_append_only();
CREATE TRIGGER tax_taxsettings_no_delete BEFORE DELETE ON tax_taxsettings
    FOR EACH ROW EXECUTE FUNCTION tax_no_delete();
CREATE TRIGGER tax_vatreturnschedule_guard BEFORE INSERT OR UPDATE ON tax_vatreturnschedule
    FOR EACH ROW EXECUTE FUNCTION tax_schedule_guard();
"""

TRIGGERS = [
    ("tax_vatreturn_lock", "tax_vatreturn"), ("tax_vatreturnbox_lock", "tax_vatreturnbox"),
    ("tax_vatreturnsourceline_lock", "tax_vatreturnsourceline"),
    ("tax_vatmanualadjustment_lock", "tax_vatmanualadjustment"), ("tax_vatexceptionack_lock", "tax_vatexceptionack"),
    ("tax_adjustment_lines_lock", "tax_vatmanualadjustment_journal_lines"),
    ("tax_importvatrecord_lock", "tax_importvatrecord"), ("tax_taxpayment_lock", "tax_taxpayment"),
    ("tax_taxrefund_lock", "tax_taxrefund"), ("tax_vatcorrection_lock", "tax_vatcorrection"),
    ("tax_correction_entries_lock", "tax_vatcorrection_correcting_entries"),
    ("tax_taxalert_append_only", "tax_taxalert"), ("tax_taxsettings_no_delete", "tax_taxsettings"),
    ("tax_vatreturnschedule_guard", "tax_vatreturnschedule"),
]
FUNCTIONS = ["tax_vatreturn_lock()", "tax_return_child_lock()", "tax_adjustment_lines_lock()", "tax_document_lock()",
             "tax_correction_lock()", "tax_correction_entries_lock()", "tax_append_only()", "tax_no_delete()",
             "tax_schedule_guard()", "tax_return_locked(bigint)", "tax_changed_keys(jsonb, jsonb)"]
REVERSE = "\n".join([f"DROP TRIGGER IF EXISTS {t} ON {table};" for t, table in TRIGGERS]
                    + [f"DROP FUNCTION IF EXISTS {f};" for f in FUNCTIONS])


def _run(schema_editor, sql):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(sql)  # no parameters


def forwards(apps, schema_editor):
    _run(schema_editor, FORWARD)


def backwards(apps, schema_editor):
    _run(schema_editor, REVERSE)


class Migration(migrations.Migration):
    dependencies = [("tax", "0002_seed_reference")]

    operations = [migrations.RunPython(forwards, backwards)]
