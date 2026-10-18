"""PostgreSQL guards for the review fixes (tax-returns §10.4; review item 9, items 3, 4, 6). Nothing on SQLite.

- ``tax_vatreturn``: an INSERT is a draft only (an amendment links a filed version of the same period); a return
  becomes ``filed`` only with its ZATCA reference, date and filer (and receipt unless imported), either from
  ``approved`` or, for a legacy import (R12), from ``reviewed`` with a reviewer and an approver other than its
  creator and a period before the entity's VAT go-live date; ``approved`` only from ``reviewed`` with an approver.
  The source never changes.
- ``tax_vatcorrection``: a correction carried by a locked return changes only by being filed.
- correcting entries are linked while the correction is open, or assigned to a return that is not locked.
- ``tax_vatfrequencychange``: append-only.
"""
import importlib

from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION tax_vatreturn_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
    golive date;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'VAT return % is never deleted (tax R7, R32)', OLD.id;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' THEN
            RAISE EXCEPTION 'A VAT return is created as a draft only (tax review item 9)';
        END IF;
        IF NEW.version > 0 AND NOT EXISTS (
                SELECT 1 FROM tax_vatreturn o WHERE o.id = NEW.amends_id AND o.entity_id = NEW.entity_id
                   AND o.period_start = NEW.period_start AND o.status IN ('filed', 'settled')) THEN
            RAISE EXCEPTION 'An amendment amends a filed return of the same entity and period (tax R28)';
        END IF;
        RETURN NEW;
    END IF;
    changed := tax_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF array_length(changed, 1) IS NULL THEN
        RETURN NEW;
    END IF;
    IF changed && ARRAY['id', 'entity_id', 'created_by_id', 'created_at', 'source'] THEN
        RAISE EXCEPTION 'VAT return %: entity, source and creator never change', OLD.id;
    END IF;
    IF NEW.status = 'filed' AND OLD.status NOT IN ('filed', 'settled') THEN
        IF coalesce(NEW.zatca_filing_reference, '') = '' OR NEW.filed_on IS NULL OR NEW.filed_by_id IS NULL
                OR (NEW.source <> 'import' AND coalesce(NEW.acknowledgement, '') = '') THEN
            RAISE EXCEPTION 'VAT return %: filing needs the ZATCA reference, date, filer and receipt (tax review item 9)',
                OLD.id;
        END IF;
        IF OLD.status = 'reviewed' THEN
            SELECT s.vat_golive_date INTO golive FROM tax_taxsettings s WHERE s.entity_id = NEW.entity_id;
            IF NEW.source = 'import' AND NEW.reviewed_by_id IS NOT NULL AND NEW.approved_by_id IS NOT NULL
                    AND NEW.reviewed_by_id <> NEW.created_by_id AND NEW.approved_by_id <> NEW.created_by_id
                    AND NEW.approved_by_id <> NEW.reviewed_by_id AND golive IS NOT NULL
                    AND NEW.period_end < golive THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'VAT return %: only an approved return is filed (imports: reviewed and approved by two '
                            'other people, before the go-live date)', OLD.id;
        END IF;
        IF OLD.status <> 'approved' THEN
            RAISE EXCEPTION 'VAT return %: only an approved return is filed', OLD.id;
        END IF;
    END IF;
    IF OLD.status NOT IN ('approved', 'filed', 'settled', 'amended') THEN
        IF NEW.status IN ('filed', 'settled', 'amended') THEN
            RAISE EXCEPTION 'VAT return %: % -> % is not a lifecycle step', OLD.id, OLD.status, NEW.status;
        END IF;
        IF NEW.status = 'approved' AND NOT (OLD.status = 'reviewed' AND NEW.approved_by_id IS NOT NULL
                                            AND NEW.reviewed_by_id IS NOT NULL) THEN
            RAISE EXCEPTION 'VAT return %: only a reviewed return is approved, by its approver', OLD.id;
        END IF;
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
    IF OLD.target_return_id IS NOT NULL AND tax_return_locked(OLD.target_return_id)
            AND to_jsonb(OLD) IS DISTINCT FROM to_jsonb(NEW)
            AND NOT (tax_changed_keys(to_jsonb(OLD), to_jsonb(NEW)) <@ ARRAY['status', 'voluntary_disclosure_ref']
                     AND NEW.status = 'filed') THEN
        RAISE EXCEPTION 'VAT correction % is carried by a locked return; only its filing changes it', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION tax_correction_entries_lock() RETURNS trigger AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'correcting entries are never unlinked or changed (tax R26)';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM tax_vatcorrection c WHERE c.id = NEW.vatcorrection_id
                      AND (c.status = 'open' OR (c.status = 'assigned' AND NOT tax_return_locked(c.target_return_id))))
    THEN
        RAISE EXCEPTION 'correcting entries are linked only before a locked return carries the correction (tax R26)';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER tax_vatreturn_insert BEFORE INSERT ON tax_vatreturn
    FOR EACH ROW EXECUTE FUNCTION tax_vatreturn_lock();
CREATE TRIGGER tax_vatfrequencychange_append_only BEFORE UPDATE OR DELETE ON tax_vatfrequencychange
    FOR EACH ROW EXECUTE FUNCTION tax_append_only();
"""

REVERSE_DROP = """
DROP TRIGGER IF EXISTS tax_vatreturn_insert ON tax_vatreturn;
DROP TRIGGER IF EXISTS tax_vatfrequencychange_append_only ON tax_vatfrequencychange;
"""


def _run(schema_editor, sql):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(sql)  # no parameters


def forwards(apps, schema_editor):
    _run(schema_editor, FORWARD)


def backwards(apps, schema_editor):
    previous = importlib.import_module("tax.migrations.0003_immutability_triggers").FORWARD
    _run(schema_editor, REVERSE_DROP + previous.split("CREATE TRIGGER")[0])  # the 0003 functions again


class Migration(migrations.Migration):
    dependencies = [("tax", "0005_seed_penalty_rates")]

    operations = [migrations.RunPython(forwards, backwards)]
