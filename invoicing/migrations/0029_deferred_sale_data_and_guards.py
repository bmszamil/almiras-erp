"""R112 (bai' ajel sale to an employee): reference data V3 (income type ``deferred_sale``) and, on PostgreSQL, the
database guards of the two bai' ajel tables, like the other posted records (R13, R36).

- ``invoicing_deferredsale``: written once by invoicing.services; afterwards only ``active`` goes from true to
  false (its sale invoice was rejected by ZATCA and replaced). Never deleted.
- ``invoicing_deferredprofitrelease``: inserted as posted without an entry; its entry is recorded once; posted ->
  reversed only with the reversal of its own entry (or with no entry when no profit was recognised), set once with
  the reverser, time and reason; reversed rows never change. Never deleted.
"""
from django.db import migrations

TRIGGER = r"""
CREATE OR REPLACE FUNCTION invoicing_deferredsale_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'bai'' ajel sale % is never deleted (R112)', OLD.id;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NOT NEW.active THEN
            RAISE EXCEPTION 'a bai'' ajel sale is created active through invoicing.services (R112)';
        END IF;
        RETURN NEW;
    END IF;
    changed := invoicing_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF array_length(changed, 1) IS NULL THEN
        RETURN NEW;
    END IF;
    IF NOT changed <@ ARRAY['active'] OR NOT OLD.active OR NEW.active THEN
        RAISE EXCEPTION 'bai'' ajel sale % never changes; only a rejected sale invoice deactivates it (R112)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS invoicing_deferredsale_lock ON invoicing_deferredsale;
CREATE TRIGGER invoicing_deferredsale_lock BEFORE INSERT OR UPDATE OR DELETE ON invoicing_deferredsale
    FOR EACH ROW EXECUTE FUNCTION invoicing_deferredsale_lock();

CREATE OR REPLACE FUNCTION invoicing_deferredprofitrelease_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'bai'' ajel profit recognition % is never deleted; it is reversed (R112)', OLD.id;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'posted' OR NEW.journal_entry_id IS NOT NULL OR NEW.reversal_entry_id IS NOT NULL
           OR NEW.reversed_by_id IS NOT NULL OR NEW.reversed_at IS NOT NULL THEN
            RAISE EXCEPTION 'a bai'' ajel profit recognition is created through invoicing.services (R112)';
        END IF;
        RETURN NEW;
    END IF;
    changed := invoicing_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF array_length(changed, 1) IS NULL THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'posted' AND NEW.status = 'posted' THEN
        IF NOT changed <@ ARRAY['journal_entry_id'] OR OLD.journal_entry_id IS NOT NULL THEN
            RAISE EXCEPTION 'bai'' ajel profit recognition % never changes; its entry is recorded once', OLD.id;
        END IF;
    ELSIF OLD.status = 'posted' AND NEW.status = 'reversed' THEN
        IF NOT changed <@ ARRAY['status', 'reversal_entry_id', 'reversed_by_id', 'reversed_at', 'reversal_reason']
           OR NEW.reversed_by_id IS NULL OR NEW.reversed_at IS NULL
           OR (OLD.journal_entry_id IS NULL AND NEW.reversal_entry_id IS NOT NULL)
           OR (OLD.journal_entry_id IS NOT NULL AND NOT EXISTS (
               SELECT 1 FROM ledger_journalentry e
                WHERE e.id = NEW.reversal_entry_id AND e.reversal_of_id = OLD.journal_entry_id
                  AND e.entity_id = OLD.entity_id)) THEN
            RAISE EXCEPTION 'bai'' ajel profit recognition % is reversed only with the reversal of its own entry', OLD.id;
        END IF;
    ELSE
        RAISE EXCEPTION 'bai'' ajel profit recognition % is %; it never changes', OLD.id, OLD.status;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS invoicing_deferredprofitrelease_lock ON invoicing_deferredprofitrelease;
CREATE TRIGGER invoicing_deferredprofitrelease_lock BEFORE INSERT OR UPDATE OR DELETE
    ON invoicing_deferredprofitrelease FOR EACH ROW EXECUTE FUNCTION invoicing_deferredprofitrelease_lock();
"""

DROP = r"""
DROP TRIGGER IF EXISTS invoicing_deferredsale_lock ON invoicing_deferredsale;
DROP FUNCTION IF EXISTS invoicing_deferredsale_lock();
DROP TRIGGER IF EXISTS invoicing_deferredprofitrelease_lock ON invoicing_deferredprofitrelease;
DROP FUNCTION IF EXISTS invoicing_deferredprofitrelease_lock();
"""


def _run(schema_editor, sql):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(sql)  # no parameters: the "%" in RAISE messages is not a placeholder


def forwards(apps, schema_editor):
    from invoicing.reference_seed import apply_v3

    apply_v3(apps)
    _run(schema_editor, TRIGGER)


def backwards(apps, schema_editor):
    _run(schema_editor, DROP)


class Migration(migrations.Migration):
    dependencies = [("invoicing", "0028_deferred_sale")]

    operations = [migrations.RunPython(forwards, backwards)]
