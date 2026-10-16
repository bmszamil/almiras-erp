"""Reversal and document links in the database (PostgreSQL only): set at insert, never changed, checked at posting.

Replaces the function of 0009_status_transition_triggers (0009 itself is
applied and stays unchanged). Everything 0009 checks is kept; added:

- UPDATE of any entry, also a draft: ``reversal_of_id``, ``document_type``
  and ``document_id`` never change after insert. A forged ``reversal_of`` on
  a draft let a Sharia-prohibited draft post as a "reversal" (reviewer probe).
- INSERT with ``reversal_of_id``: the original exists, is posted, and has the
  same entity, ``document_type`` and ``document_id``.
- draft -> posted with ``reversal_of_id``: the original is still posted and
  the entry's lines mirror the original's, line by line in id order:
  same account, debit and credit swapped, same currency, transaction
  amount, rate and counterparty (what ledger.services._reverse writes).

Like 0009 this is a minimal check against writes that bypass the ORM, not
proof of origin: raw SQL that inserts a draft with a correct reversal link
and mirror lines, or that sets status together with number, period,
posted_at and posted_by on a balanced draft, is accepted. The database
role's privileges (access.md OQ-10) are the guard against such writes.

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
        IF NEW.reversal_of_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM ledger_journalentry o
             WHERE o.id = NEW.reversal_of_id AND o.status = 'posted' AND o.entity_id = NEW.entity_id
               AND o.document_type = NEW.document_type AND o.document_id IS NOT DISTINCT FROM NEW.document_id
        ) THEN
            RAISE EXCEPTION 'a reversal reverses a posted entry of the same entity and document';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.reversal_of_id IS DISTINCT FROM OLD.reversal_of_id
       OR NEW.document_type IS DISTINCT FROM OLD.document_type
       OR NEW.document_id IS DISTINCT FROM OLD.document_id THEN
        RAISE EXCEPTION 'journal entry %: reversal and document links never change after insert', OLD.id;
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
        IF NEW.reversal_of_id IS NOT NULL THEN
            IF NOT EXISTS (
                SELECT 1 FROM ledger_journalentry o WHERE o.id = NEW.reversal_of_id AND o.status = 'posted'
            ) THEN
                RAISE EXCEPTION 'journal entry % reverses an entry that is not posted', OLD.id;
            END IF;
            IF EXISTS (
                WITH o AS (
                    SELECT row_number() OVER (ORDER BY id) AS n, account_id, debit, credit, currency_id, txn_amount,
                           fx_rate, counterparty_entity_id
                      FROM ledger_journalline WHERE entry_id = NEW.reversal_of_id
                ), r AS (
                    SELECT row_number() OVER (ORDER BY id) AS n, account_id, debit, credit, currency_id, txn_amount,
                           fx_rate, counterparty_entity_id
                      FROM ledger_journalline WHERE entry_id = OLD.id
                )
                SELECT 1 FROM o FULL JOIN r ON o.n = r.n
                 WHERE o.n IS NULL OR r.n IS NULL
                    OR o.account_id <> r.account_id OR o.debit <> r.credit OR o.credit <> r.debit
                    OR o.currency_id <> r.currency_id OR o.txn_amount <> r.txn_amount
                    OR o.fx_rate IS DISTINCT FROM r.fx_rate
                    OR o.counterparty_entity_id IS DISTINCT FROM r.counterparty_entity_id
            ) THEN
                RAISE EXCEPTION 'journal entry % is a reversal whose lines do not mirror the original', OLD.id;
            END IF;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS ledger_journalentry_posting_lock ON ledger_journalentry;
CREATE TRIGGER ledger_journalentry_posting_lock BEFORE INSERT OR UPDATE OR DELETE ON ledger_journalentry
    FOR EACH ROW EXECUTE FUNCTION ledger_entry_posting_lock();
"""

# Back to the 0009 entry function (the trigger definition is the same).
REVERSE = r"""
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
    dependencies = [("ledger", "0011_exchange_rate_lock_trigger")]

    operations = [migrations.RunPython(forwards, backwards)]
