"""CLAUDE.md rules 1, 2 and 3 in the database (PostgreSQL only): status changes as ledger.services makes them.

Replaces the functions of 0007_posting_lock_triggers (0007 itself is applied
and stays unchanged):

- ledger_journalentry
  - INSERT: a new entry is a draft.
  - draft -> posted: number, period, posted_at and posted_by are set, the
    entry has 2+ lines and balances (what ledger.services._post writes).
  - draft -> reversed: rejected.
  - posted -> reversed: nothing else changes, and a posted entry with
    reversal_of = this entry already exists (what reverse_entry writes).
  - a reversed entry never changes; posted / reversed entries are never deleted.
- ledger_journalline: the entry's status is read FOR SHARE, so a line write
  waits for a posting of the same entry in flight (which holds the entry FOR
  UPDATE) and then sees the posted status.

This is a minimal check against writes that bypass the ORM, not proof that
ledger.services made the change: raw SQL that sets ``status = 'posted'``
together with ``number``, ``period_id``, ``posted_at`` and ``posted_by_id``
on a draft with 2+ balancing lines is accepted (no Sharia, period-open,
rate or segregation check runs in the database). The database role's
privileges (access.md OQ-10) are the guard against such writes. 0012
replaces this function and adds the reversal and document link checks.

On SQLite this migration does nothing; the model and queryset guards apply.
"""
from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION ledger_entry_posting_lock() RETURNS trigger AS $$
DECLARE
    line_count integer;
    unbalanced boolean;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION 'journal entry % is %; it is never deleted (reverse it)', OLD.id, OLD.status;
        END IF;
        RETURN OLD;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' THEN
            RAISE EXCEPTION 'journal entries are created as drafts and posted through ledger.services';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.status = 'reversed' THEN
        RAISE EXCEPTION 'journal entry % is reversed; it never changes', OLD.id;
    END IF;
    IF OLD.status = 'posted' THEN
        IF NEW.status <> 'reversed' OR (to_jsonb(NEW) - 'status') IS DISTINCT FROM (to_jsonb(OLD) - 'status') THEN
            RAISE EXCEPTION 'journal entry % is posted; it is never edited (reverse it)', OLD.id;
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM ledger_journalentry r WHERE r.reversal_of_id = OLD.id AND r.status = 'posted'
        ) THEN
            RAISE EXCEPTION 'journal entry % is marked reversed only when its posted reversal exists', OLD.id;
        END IF;
        RETURN NEW;
    END IF;
    -- OLD.status = 'draft'
    IF NEW.status = 'reversed' THEN
        RAISE EXCEPTION 'draft journal entry % cannot be reversed', OLD.id;
    END IF;
    IF NEW.status = 'posted' THEN
        IF NEW.number = '' OR NEW.period_id IS NULL OR NEW.posted_at IS NULL OR NEW.posted_by_id IS NULL THEN
            RAISE EXCEPTION 'journal entry % is posted only through ledger.services (number, period, posted_at, posted_by)', OLD.id;
        END IF;
        SELECT count(*), coalesce(sum(debit), 0) <> coalesce(sum(credit), 0)
          INTO line_count, unbalanced FROM ledger_journalline WHERE entry_id = OLD.id;
        IF line_count < 2 OR unbalanced THEN
            RAISE EXCEPTION 'journal entry % needs 2+ lines that balance to be posted', OLD.id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS ledger_journalentry_posting_lock ON ledger_journalentry;
CREATE TRIGGER ledger_journalentry_posting_lock BEFORE INSERT OR UPDATE OR DELETE ON ledger_journalentry
    FOR EACH ROW EXECUTE FUNCTION ledger_entry_posting_lock();

CREATE OR REPLACE FUNCTION ledger_line_posting_lock() RETURNS trigger AS $$
DECLARE
    entry_status text;
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        SELECT status INTO entry_status FROM ledger_journalentry WHERE id = OLD.entry_id FOR SHARE;
        IF entry_status IS NOT NULL AND entry_status <> 'draft' THEN
            RAISE EXCEPTION 'line % belongs to a % entry; it never changes', OLD.id, entry_status;
        END IF;
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        SELECT status INTO entry_status FROM ledger_journalentry WHERE id = NEW.entry_id FOR SHARE;
        IF entry_status IS NOT NULL AND entry_status <> 'draft' THEN
            RAISE EXCEPTION 'lines cannot be added to or moved onto a % entry', entry_status;
        END IF;
        RETURN NEW;
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;
"""

# Back to the 0007 functions and trigger.
REVERSE = r"""
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

DROP TRIGGER IF EXISTS ledger_journalentry_posting_lock ON ledger_journalentry;
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
    dependencies = [("ledger", "0008_one_entry_per_document")]

    operations = [migrations.RunPython(forwards, backwards)]
