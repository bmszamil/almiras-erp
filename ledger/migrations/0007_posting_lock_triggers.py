"""CLAUDE.md rules 1 and 3 in the database (PostgreSQL only): posted entries and their lines are locked.

- ledger_journalentry: a posted or reversed row is never deleted. A posted
  row may change in one way only: status posted -> reversed with every other
  column unchanged (reverse_entry). A reversed row never changes. Drafts are
  free (posting itself is the draft -> posted write).
- ledger_journalline: no INSERT, UPDATE or DELETE of a line whose entry (old
  or new) is posted or reversed.

Any later data migration that must touch posted rows has to drop and
re-create these triggers explicitly, so the bypass is visible in review (as
0003_fx_backfill_lines was, R38). TRUNCATE does not fire row triggers. On
SQLite this migration does nothing; the model and queryset guards apply.
"""
from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION ledger_entry_posting_lock() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION 'journal entry % is %; it is never deleted (reverse it)', OLD.id, OLD.status;
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status = 'reversed' THEN
        RAISE EXCEPTION 'journal entry % is reversed; it never changes', OLD.id;
    END IF;
    IF OLD.status = 'posted' AND (
        NEW.status <> 'reversed' OR (to_jsonb(NEW) - 'status') IS DISTINCT FROM (to_jsonb(OLD) - 'status')
    ) THEN
        RAISE EXCEPTION 'journal entry % is posted; it is never edited (reverse it)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER ledger_journalentry_posting_lock BEFORE UPDATE OR DELETE ON ledger_journalentry
    FOR EACH ROW EXECUTE FUNCTION ledger_entry_posting_lock();

CREATE OR REPLACE FUNCTION ledger_line_posting_lock() RETURNS trigger AS $$
DECLARE
    entry_status text;
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        SELECT status INTO entry_status FROM ledger_journalentry WHERE id = OLD.entry_id;
        IF entry_status IS NOT NULL AND entry_status <> 'draft' THEN
            RAISE EXCEPTION 'line % belongs to a % entry; it never changes', OLD.id, entry_status;
        END IF;
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        SELECT status INTO entry_status FROM ledger_journalentry WHERE id = NEW.entry_id;
        IF entry_status IS NOT NULL AND entry_status <> 'draft' THEN
            RAISE EXCEPTION 'lines cannot be added to or moved onto a % entry', entry_status;
        END IF;
        RETURN NEW;
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER ledger_journalline_posting_lock BEFORE INSERT OR UPDATE OR DELETE ON ledger_journalline
    FOR EACH ROW EXECUTE FUNCTION ledger_line_posting_lock();
"""

REVERSE = r"""
DROP TRIGGER IF EXISTS ledger_journalline_posting_lock ON ledger_journalline;
DROP TRIGGER IF EXISTS ledger_journalentry_posting_lock ON ledger_journalentry;
DROP FUNCTION IF EXISTS ledger_line_posting_lock();
DROP FUNCTION IF EXISTS ledger_entry_posting_lock();
"""


def _run(schema_editor, sql):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(sql)  # no parameters: the "%" in RAISE messages is not a placeholder


def forwards(apps, schema_editor):
    _run(schema_editor, FORWARD)


def backwards(apps, schema_editor):
    _run(schema_editor, REVERSE)


class Migration(migrations.Migration):
    dependencies = [("ledger", "0006_entry_source_document_segregation")]

    operations = [migrations.RunPython(forwards, backwards)]
