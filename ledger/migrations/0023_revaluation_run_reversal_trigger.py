"""RevaluationRun status in the database with the posted -> reversed step (PostgreSQL only; ledger-fx OQ-9 team
default, invoicing R97 review 2026-10-22).

Replaces ``ledger_revaluation_run_lock`` of 0015 with the same rules plus one transition, the one
``reverse_revaluation`` writes:

- posted -> reversed: reversed_by, reversed_at, correction_entry and correction_reversal are set; nothing but
  status, those four columns and reversal_reason changes.
- INSERT and every other state keep the correction columns empty; reversed runs never change.

On SQLite this migration does nothing; the model and queryset guards apply.
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
           OR NEW.discarded_by_id IS NOT NULL OR NEW.discarded_at IS NOT NULL OR NEW.reversal_id IS NOT NULL
           OR NEW.reversed_by_id IS NOT NULL OR NEW.reversed_at IS NOT NULL OR NEW.correction_entry_id IS NOT NULL
           OR NEW.correction_reversal_id IS NOT NULL THEN
            RAISE EXCEPTION 'revaluation runs are created as drafts by revalue_period';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.entity_id <> OLD.entity_id OR NEW.period_id <> OLD.period_id OR NEW.run_by_id <> OLD.run_by_id
       OR NEW.run_at <> OLD.run_at THEN
        RAISE EXCEPTION 'revaluation run %: entity, period and who ran it never change', OLD.id;
    END IF;
    IF OLD.status = 'posted' AND NEW.status = 'reversed' THEN
        IF NEW.reversed_by_id IS NULL OR NEW.reversed_at IS NULL OR NEW.correction_entry_id IS NULL
           OR NEW.correction_reversal_id IS NULL
           OR (to_jsonb(NEW) - ARRAY['status', 'reversed_by_id', 'reversed_at', 'correction_entry_id',
                                     'correction_reversal_id', 'reversal_reason'])
              IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['status', 'reversed_by_id', 'reversed_at', 'correction_entry_id',
                                                      'correction_reversal_id', 'reversal_reason']) THEN
            RAISE EXCEPTION 'revaluation run % is reversed only by reverse_revaluation (who, when, corrections)', OLD.id;
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.status <> 'draft' THEN
        RAISE EXCEPTION 'revaluation run % is %; it never changes (R44, R45)', OLD.id, OLD.status;
    END IF;
    IF NEW.reversed_by_id IS NOT NULL OR NEW.reversed_at IS NOT NULL OR NEW.correction_entry_id IS NOT NULL
       OR NEW.correction_reversal_id IS NOT NULL THEN
        RAISE EXCEPTION 'revaluation run % is corrected only once posted, by reverse_revaluation', OLD.id;
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


def _run(schema_editor, sql):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(sql)  # no parameters: the "%" in RAISE messages is not a placeholder


def forwards(apps, schema_editor):
    _run(schema_editor, FORWARD)


def backwards(apps, schema_editor):
    from importlib import import_module

    _run(schema_editor, import_module("ledger.migrations.0015_revaluation_run_trigger").FORWARD)


class Migration(migrations.Migration):
    dependencies = [("ledger", "0022_revaluation_run_reversal")]

    operations = [migrations.RunPython(forwards, backwards)]
