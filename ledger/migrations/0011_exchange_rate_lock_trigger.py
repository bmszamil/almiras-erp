"""ledger-fx R11 in the database (PostgreSQL only): a rate row used by a booked line never changes.

UPDATE or DELETE of a ledger_exchangerate row is rejected while any line of a
posted or reversed entry references it. The entry status is read FOR SHARE,
so a change racing a posting waits for it. Postings also hold the rate rows
they use FOR SHARE (ledger.services._lock_for_posting), so a rate cannot be
changed between validation and commit. On SQLite this migration does
nothing; the model and queryset guards apply.
"""
from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION ledger_rate_lock() RETURNS trigger AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM ledger_journalline l JOIN ledger_journalentry e ON e.id = l.entry_id
        WHERE l.exchange_rate_id = OLD.id AND e.status <> 'draft'
        FOR SHARE OF e
    ) THEN
        RAISE EXCEPTION 'exchange rate % is used by posted entries; add a new rate row (ledger-fx R11)', OLD.id;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER ledger_exchangerate_lock BEFORE UPDATE OR DELETE ON ledger_exchangerate
    FOR EACH ROW EXECUTE FUNCTION ledger_rate_lock();
"""

REVERSE = r"""
DROP TRIGGER IF EXISTS ledger_exchangerate_lock ON ledger_exchangerate;
DROP FUNCTION IF EXISTS ledger_rate_lock();
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
    dependencies = [("ledger", "0010_revaluation_run_status")]

    operations = [migrations.RunPython(forwards, backwards)]
