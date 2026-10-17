"""Purchase documents: the purification flag is written once, with the entry (verification review item 3).

Replaces ``invoicing_booked_document_lock`` (migration 0007). A posted purchase document's
``requires_purification`` may change only in the update that first records its ``journal_entry_id``, and
then it must equal what the entry's lines carry (CLAUDE.md rule 5, invoicing R6). Before, raw SQL could flip
the flag on a posted bill at any time. On SQLite this migration does nothing; the model guards apply.
"""
from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION invoicing_booked_document_lock() RETURNS trigger AS $$
-- Purchase documents, receipts and vendor payments. TG_ARGV[0] = 'purchase' | 'receipt' | 'payment'.
DECLARE
    changed text[];
    kind text := TG_ARGV[0];
    booked text[] := ARRAY['number', 'journal_entry_id', 'reversal_entry_id'];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION '% % is %; only drafts are deleted (R24)', kind, OLD.id, OLD.status;
        END IF;
        RETURN OLD;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' OR NEW.number <> '' OR NEW.journal_entry_id IS NOT NULL
           OR NEW.reversal_entry_id IS NOT NULL THEN
            RAISE EXCEPTION '% records are created as drafts through invoicing.services', kind;
        END IF;
        RETURN NEW;
    END IF;
    changed := invoicing_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF changed && ARRAY['id', 'entity_id', 'created_by_id', 'created_at'] OR (OLD.number <> '' AND NEW.number <> OLD.number) THEN
        RAISE EXCEPTION '% %: identity, creator and number never change', kind, OLD.id;
    END IF;
    IF OLD.status = 'draft' THEN
        IF NEW.status = 'draft' OR (NEW.status = 'submitted' AND kind IN ('purchase', 'payment')) THEN
            IF changed && booked THEN
                RAISE EXCEPTION 'draft % %: no number or entry before posting', kind, OLD.id;
            END IF;
        ELSIF NEW.status = 'posted' AND kind = 'receipt' THEN
            IF NEW.number = '' OR (to_jsonb(NEW) ->> 'posted_by_id') IS NULL
               OR changed && ARRAY['journal_entry_id', 'reversal_entry_id'] THEN
                RAISE EXCEPTION 'receipt % is posted only through invoicing.services (number, poster)', OLD.id;
            END IF;
        ELSE
            RAISE EXCEPTION '% %: draft -> % is not a transition (R58, R59)', kind, OLD.id, NEW.status;
        END IF;
    ELSIF OLD.status = 'submitted' THEN
        IF NEW.status = 'posted' THEN
            IF NEW.number = '' OR (to_jsonb(NEW) ->> 'approved_by_id') IS NULL
               OR changed && ARRAY['journal_entry_id', 'reversal_entry_id'] THEN
                RAISE EXCEPTION '% % is posted only by approval (number, approver)', kind, OLD.id;
            END IF;
        ELSIF array_length(changed, 1) IS NOT NULL THEN
            RAISE EXCEPTION 'submitted % % changes only by approval (R58, R59)', kind, OLD.id;
        END IF;
    ELSIF OLD.status = 'posted' THEN
        IF NEW.status = 'posted' THEN
            IF NOT changed <@ ARRAY['journal_entry_id', 'requires_purification']
               OR ('journal_entry_id' = ANY(changed) AND OLD.journal_entry_id IS NOT NULL) THEN
                RAISE EXCEPTION 'posted % % is never edited; its entry is recorded once', kind, OLD.id;
            END IF;
            -- The purification flag is the entry's: written only with the entry, and equal to its lines.
            IF 'requires_purification' = ANY(changed) AND NOT 'journal_entry_id' = ANY(changed) THEN
                RAISE EXCEPTION '% %: the purification flag changes only when its entry is recorded (rule 5)',
                    kind, OLD.id;
            END IF;
            IF 'journal_entry_id' = ANY(changed) AND kind = 'purchase'
               AND (to_jsonb(NEW) ->> 'requires_purification')::boolean IS DISTINCT FROM EXISTS (
                   SELECT 1 FROM ledger_journalline l
                    WHERE l.entry_id = NEW.journal_entry_id AND l.requires_purification) THEN
                RAISE EXCEPTION '% %: the purification flag is the one its entry''s lines carry (rule 5)', kind, OLD.id;
            END IF;
        ELSIF NEW.status = 'reversed' THEN
            IF NOT changed <@ ARRAY['status', 'reversal_entry_id'] OR OLD.reversal_entry_id IS NOT NULL
               OR NEW.reversal_entry_id IS NULL OR OLD.journal_entry_id IS NULL THEN
                RAISE EXCEPTION '% % is reversed only with its reversal entry (R24, R32)', kind, OLD.id;
            END IF;
        ELSE
            RAISE EXCEPTION 'posted % % is only reversed (R24)', kind, OLD.id;
        END IF;
    ELSIF array_length(changed, 1) IS NOT NULL THEN
        RAISE EXCEPTION '% % is %; it never changes', kind, OLD.id, OLD.status;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

PREVIOUS = r"""
CREATE OR REPLACE FUNCTION invoicing_booked_document_lock() RETURNS trigger AS $$
-- Purchase documents, receipts and vendor payments. TG_ARGV[0] = 'purchase' | 'receipt' | 'payment'.
DECLARE
    changed text[];
    kind text := TG_ARGV[0];
    booked text[] := ARRAY['number', 'journal_entry_id', 'reversal_entry_id'];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION '% % is %; only drafts are deleted (R24)', kind, OLD.id, OLD.status;
        END IF;
        RETURN OLD;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' OR NEW.number <> '' OR NEW.journal_entry_id IS NOT NULL
           OR NEW.reversal_entry_id IS NOT NULL THEN
            RAISE EXCEPTION '% records are created as drafts through invoicing.services', kind;
        END IF;
        RETURN NEW;
    END IF;
    changed := invoicing_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF changed && ARRAY['id', 'entity_id', 'created_by_id', 'created_at'] OR (OLD.number <> '' AND NEW.number <> OLD.number) THEN
        RAISE EXCEPTION '% %: identity, creator and number never change', kind, OLD.id;
    END IF;
    IF OLD.status = 'draft' THEN
        IF NEW.status = 'draft' OR (NEW.status = 'submitted' AND kind IN ('purchase', 'payment')) THEN
            IF changed && booked THEN
                RAISE EXCEPTION 'draft % %: no number or entry before posting', kind, OLD.id;
            END IF;
        ELSIF NEW.status = 'posted' AND kind = 'receipt' THEN
            IF NEW.number = '' OR (to_jsonb(NEW) ->> 'posted_by_id') IS NULL
               OR changed && ARRAY['journal_entry_id', 'reversal_entry_id'] THEN
                RAISE EXCEPTION 'receipt % is posted only through invoicing.services (number, poster)', OLD.id;
            END IF;
        ELSE
            RAISE EXCEPTION '% %: draft -> % is not a transition (R58, R59)', kind, OLD.id, NEW.status;
        END IF;
    ELSIF OLD.status = 'submitted' THEN
        IF NEW.status = 'posted' THEN
            IF NEW.number = '' OR (to_jsonb(NEW) ->> 'approved_by_id') IS NULL
               OR changed && ARRAY['journal_entry_id', 'reversal_entry_id'] THEN
                RAISE EXCEPTION '% % is posted only by approval (number, approver)', kind, OLD.id;
            END IF;
        ELSIF array_length(changed, 1) IS NOT NULL THEN
            RAISE EXCEPTION 'submitted % % changes only by approval (R58, R59)', kind, OLD.id;
        END IF;
    ELSIF OLD.status = 'posted' THEN
        IF NEW.status = 'posted' THEN
            IF NOT changed <@ ARRAY['journal_entry_id', 'requires_purification']
               OR ('journal_entry_id' = ANY(changed) AND OLD.journal_entry_id IS NOT NULL) THEN
                RAISE EXCEPTION 'posted % % is never edited; its entry is recorded once', kind, OLD.id;
            END IF;
        ELSIF NEW.status = 'reversed' THEN
            IF NOT changed <@ ARRAY['status', 'reversal_entry_id'] OR OLD.reversal_entry_id IS NOT NULL
               OR NEW.reversal_entry_id IS NULL OR OLD.journal_entry_id IS NULL THEN
                RAISE EXCEPTION '% % is reversed only with its reversal entry (R24, R32)', kind, OLD.id;
            END IF;
        ELSE
            RAISE EXCEPTION 'posted % % is only reversed (R24)', kind, OLD.id;
        END IF;
    ELSIF array_length(changed, 1) IS NOT NULL THEN
        RAISE EXCEPTION '% % is %; it never changes', kind, OLD.id, OLD.status;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def _run(schema_editor, sql):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(sql)  # no parameters: the "%" in RAISE messages is not a placeholder


def forwards(apps, schema_editor):
    _run(schema_editor, FORWARD)


def backwards(apps, schema_editor):
    _run(schema_editor, PREVIOUS)


class Migration(migrations.Migration):
    dependencies = [("invoicing", "0007_document_lock_triggers")]

    operations = [migrations.RunPython(forwards, backwards)]
