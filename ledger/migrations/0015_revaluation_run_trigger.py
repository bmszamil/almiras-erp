"""RevaluationRun status in the database (PostgreSQL only; ledger-fx R44, R45; spec-writer follow-up L1).

The same rules as ``RevaluationRun.save`` / ``delete`` (ledger/models.py), for writes that bypass the ORM:

- INSERT: a draft, with no poster, discarder or reversal.
- A run is never deleted; posted and discarded runs never change.
- entity, period, run_by and run_at never change.
- draft -> draft: still no poster, discarder or reversal.
- draft -> posted: posted_by, posted_at and the reversal are set, no discarder; nothing but status,
  reversal, posted_by and posted_at changes (what _post_revaluation writes).
- draft -> discarded: discarded_by and discarded_at are set, no poster, the entry is cleared; nothing but
  status, entry, discarded_by and discarded_at changes (what discard_revaluation writes).

As with the other ledger triggers, raw SQL that makes one of these transitions with plausible values is
accepted; the database role's privileges (access.md OQ-10) are the guard against that. On SQLite this
migration does nothing; the model and queryset guards apply.
"""
from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION ledger_revaluation_run_lock() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'revaluation run % is kept for audit; it is never deleted (R45)', OLD.id;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' OR NEW.posted_by_id IS NOT NULL OR NEW.posted_at IS NOT NULL
           OR NEW.discarded_by_id IS NOT NULL OR NEW.discarded_at IS NOT NULL OR NEW.reversal_id IS NOT NULL THEN
            RAISE EXCEPTION 'revaluation runs are created as drafts by revalue_period';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.status <> 'draft' THEN
        RAISE EXCEPTION 'revaluation run % is %; it never changes (R44, R45)', OLD.id, OLD.status;
    END IF;
    IF NEW.entity_id <> OLD.entity_id OR NEW.period_id <> OLD.period_id OR NEW.run_by_id <> OLD.run_by_id
       OR NEW.run_at <> OLD.run_at THEN
        RAISE EXCEPTION 'revaluation run %: entity, period and who ran it never change', OLD.id;
    END IF;
    IF NEW.status = 'draft' THEN
        IF NEW.posted_by_id IS NOT NULL OR NEW.posted_at IS NOT NULL OR NEW.discarded_by_id IS NOT NULL
           OR NEW.discarded_at IS NOT NULL OR NEW.reversal_id IS NOT NULL THEN
            RAISE EXCEPTION 'draft revaluation run % has no poster, discarder or reversal', OLD.id;
        END IF;
    ELSIF NEW.status = 'posted' THEN
        IF NEW.posted_by_id IS NULL OR NEW.posted_at IS NULL OR NEW.reversal_id IS NULL
           OR NEW.discarded_by_id IS NOT NULL OR NEW.discarded_at IS NOT NULL
           OR (to_jsonb(NEW) - ARRAY['status', 'reversal_id', 'posted_by_id', 'posted_at'])
              IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['status', 'reversal_id', 'posted_by_id', 'posted_at']) THEN
            RAISE EXCEPTION 'revaluation run % is posted only by post_revaluation (poster, time, reversal)', OLD.id;
        END IF;
    ELSIF NEW.status = 'discarded' THEN
        IF NEW.discarded_by_id IS NULL OR NEW.discarded_at IS NULL OR NEW.entry_id IS NOT NULL
           OR NEW.posted_by_id IS NOT NULL OR NEW.posted_at IS NOT NULL
           OR (to_jsonb(NEW) - ARRAY['status', 'entry_id', 'discarded_by_id', 'discarded_at'])
              IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['status', 'entry_id', 'discarded_by_id', 'discarded_at']) THEN
            RAISE EXCEPTION 'revaluation run % is discarded only by discard_revaluation (who, when, no entry)', OLD.id;
        END IF;
    ELSE
        RAISE EXCEPTION 'revaluation run %: unknown status %', OLD.id, NEW.status;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS ledger_revaluationrun_lock ON ledger_revaluationrun;
CREATE TRIGGER ledger_revaluationrun_lock BEFORE INSERT OR UPDATE OR DELETE ON ledger_revaluationrun
    FOR EACH ROW EXECUTE FUNCTION ledger_revaluation_run_lock();
"""

REVERSE = r"""
DROP TRIGGER IF EXISTS ledger_revaluationrun_lock ON ledger_revaluationrun;
DROP FUNCTION IF EXISTS ledger_revaluation_run_lock();
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
    dependencies = [("ledger", "0014_reversal_mirror_all_columns")]

    operations = [migrations.RunPython(forwards, backwards)]
