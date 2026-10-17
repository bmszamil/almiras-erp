"""Banking guards in the database (PostgreSQL only), mirroring the model and queryset guards for writes that
bypass the ORM (R15, R35, R40, R45). On SQLite this migration does nothing.

- statement imports, statement lines, match parts, reconciliation notes and banking events: append-only (R15, R45).
- matches: never deleted; who proposed what never changes; proposed -> confirmed | rejected, confirmed -> undone only
  while not locked by a reconciliation; rejected and undone groups never change (R17, R23, R40).
- bank transactions: never deleted; a posted document changes only to reversed (with its reversal entry) or gets
  its journal entry once; reversed and discarded documents never change (R25, R35).
- reconciliations: never deleted; an approved one changes only to superseded on reopening (R40); superseded never
  changes.
- bank accounts: never deleted; the owning entity never changes (R2).
"""
from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION banking_changed_keys(old_row jsonb, new_row jsonb) RETURNS text[] AS $$
    SELECT coalesce(array_agg(n.key ORDER BY n.key), ARRAY[]::text[])
      FROM jsonb_each(new_row) n
     WHERE n.value IS DISTINCT FROM (old_row -> n.key);
$$ LANGUAGE sql IMMUTABLE;

CREATE OR REPLACE FUNCTION banking_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only (banking R15, R45)', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION banking_match_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'bank match % is never deleted (banking R23)', OLD.id;
    END IF;
    changed := banking_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF changed && ARRAY['id', 'entity_id', 'bank_account_id', 'rule', 'confidence', 'proposed_by_id', 'proposed_at'] THEN
        RAISE EXCEPTION 'bank match %: entity, account, rule and proposer never change', OLD.id;
    END IF;
    IF OLD.status IN ('rejected', 'undone') AND array_length(changed, 1) IS NOT NULL THEN
        RAISE EXCEPTION 'bank match % is %; it never changes', OLD.id, OLD.status;
    END IF;
    IF OLD.status = 'proposed' AND NEW.status NOT IN ('proposed', 'confirmed', 'rejected') THEN
        RAISE EXCEPTION 'bank match %: a proposal is confirmed or rejected', OLD.id;
    END IF;
    IF OLD.status = 'confirmed' AND NEW.status NOT IN ('confirmed', 'undone') THEN
        RAISE EXCEPTION 'bank match %: a confirmed match is only undone', OLD.id;
    END IF;
    IF OLD.status = 'confirmed' AND NEW.status = 'undone' AND (OLD.reconciliation_id IS NOT NULL
                                                             OR NEW.reconciliation_id IS NOT NULL) THEN
        RAISE EXCEPTION 'bank match % is locked by a reconciliation (banking R40)', OLD.id;
    END IF;
    IF OLD.status = 'confirmed' AND changed && ARRAY['confirmed_by_id', 'confirmed_at', 'auto'] THEN
        RAISE EXCEPTION 'bank match %: the confirmation never changes', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION banking_transaction_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'bank transaction % is never deleted (banking R35)', OLD.id;
    END IF;
    changed := banking_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF changed && ARRAY['id', 'entity_id', 'created_by_id', 'created_at'] THEN
        RAISE EXCEPTION 'bank transaction %: entity and creator never change', OLD.id;
    END IF;
    IF OLD.status IN ('reversed', 'discarded') AND array_length(changed, 1) IS NOT NULL THEN
        RAISE EXCEPTION 'bank transaction % is %; it never changes', OLD.id, OLD.status;
    END IF;
    IF OLD.status = 'posted' THEN
        IF NOT (changed <@ ARRAY['status', 'reversal_entry_id', 'journal_entry_id']) THEN
            RAISE EXCEPTION 'bank transaction % is posted; reverse it instead (banking R35)', OLD.id;
        END IF;
        IF 'journal_entry_id' = ANY(changed) AND OLD.journal_entry_id IS NOT NULL THEN
            RAISE EXCEPTION 'bank transaction % is booked once', OLD.id;
        END IF;
        IF NEW.status NOT IN ('posted', 'reversed') OR (NEW.status = 'reversed' AND NEW.reversal_entry_id IS NULL) THEN
            RAISE EXCEPTION 'bank transaction %: a posted document only becomes reversed, with its reversal', OLD.id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION banking_reconciliation_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'bank reconciliation % is never deleted (banking R40)', OLD.id;
    END IF;
    changed := banking_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF changed && ARRAY['id', 'entity_id', 'bank_account_id', 'period_id', 'prepared_by_id'] THEN
        RAISE EXCEPTION 'bank reconciliation %: account, period and preparer never change', OLD.id;
    END IF;
    IF OLD.status = 'superseded' AND array_length(changed, 1) IS NOT NULL THEN
        RAISE EXCEPTION 'bank reconciliation % is superseded; it never changes', OLD.id;
    END IF;
    IF OLD.status = 'approved' AND NOT (NEW.status = 'superseded'
            AND changed <@ ARRAY['status', 'reopened_by_id', 'reopened_at', 'reopen_reason']
            AND NEW.reopened_by_id IS NOT NULL) THEN
        RAISE EXCEPTION 'bank reconciliation % is approved and frozen; only reopening changes it (banking R40)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION banking_account_lock() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'bank account % is never deleted', OLD.id;
    END IF;
    IF NEW.entity_id IS DISTINCT FROM OLD.entity_id THEN
        RAISE EXCEPTION 'bank account %: the owning entity never changes (banking R2)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER banking_statementimport_append_only BEFORE UPDATE OR DELETE ON banking_statementimport
    FOR EACH ROW EXECUTE FUNCTION banking_append_only();
CREATE TRIGGER banking_statementline_append_only BEFORE UPDATE OR DELETE ON banking_statementline
    FOR EACH ROW EXECUTE FUNCTION banking_append_only();
CREATE TRIGGER banking_matchpart_append_only BEFORE UPDATE OR DELETE ON banking_matchpart
    FOR EACH ROW EXECUTE FUNCTION banking_append_only();
CREATE TRIGGER banking_reconciliationnote_append_only BEFORE UPDATE OR DELETE ON banking_reconciliationnote
    FOR EACH ROW EXECUTE FUNCTION banking_append_only();
CREATE TRIGGER banking_bankingevent_append_only BEFORE UPDATE OR DELETE ON banking_bankingevent
    FOR EACH ROW EXECUTE FUNCTION banking_append_only();
CREATE TRIGGER banking_match_lock BEFORE UPDATE OR DELETE ON banking_match
    FOR EACH ROW EXECUTE FUNCTION banking_match_lock();
CREATE TRIGGER banking_banktransaction_lock BEFORE UPDATE OR DELETE ON banking_banktransaction
    FOR EACH ROW EXECUTE FUNCTION banking_transaction_lock();
CREATE TRIGGER banking_reconciliation_lock BEFORE UPDATE OR DELETE ON banking_reconciliation
    FOR EACH ROW EXECUTE FUNCTION banking_reconciliation_lock();
CREATE TRIGGER banking_bankaccount_lock BEFORE UPDATE OR DELETE ON banking_bankaccount
    FOR EACH ROW EXECUTE FUNCTION banking_account_lock();
"""

REVERSE = r"""
DROP TRIGGER IF EXISTS banking_bankaccount_lock ON banking_bankaccount;
DROP TRIGGER IF EXISTS banking_reconciliation_lock ON banking_reconciliation;
DROP TRIGGER IF EXISTS banking_banktransaction_lock ON banking_banktransaction;
DROP TRIGGER IF EXISTS banking_match_lock ON banking_match;
DROP TRIGGER IF EXISTS banking_bankingevent_append_only ON banking_bankingevent;
DROP TRIGGER IF EXISTS banking_reconciliationnote_append_only ON banking_reconciliationnote;
DROP TRIGGER IF EXISTS banking_matchpart_append_only ON banking_matchpart;
DROP TRIGGER IF EXISTS banking_statementline_append_only ON banking_statementline;
DROP TRIGGER IF EXISTS banking_statementimport_append_only ON banking_statementimport;
DROP FUNCTION IF EXISTS banking_account_lock();
DROP FUNCTION IF EXISTS banking_reconciliation_lock();
DROP FUNCTION IF EXISTS banking_transaction_lock();
DROP FUNCTION IF EXISTS banking_match_lock();
DROP FUNCTION IF EXISTS banking_append_only();
DROP FUNCTION IF EXISTS banking_changed_keys(jsonb, jsonb);
"""


def _run(schema_editor, sql):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(sql)  # no parameters


def forwards(apps, schema_editor):
    _run(schema_editor, FORWARD)


def backwards(apps, schema_editor):
    _run(schema_editor, REVERSE)


class Migration(migrations.Migration):
    dependencies = [("banking", "0002_seed_banks")]

    operations = [migrations.RunPython(forwards, backwards)]
