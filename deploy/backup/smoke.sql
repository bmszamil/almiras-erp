-- Smoke checks on a RESTORED copy of the Almiras ERP database (backup/restore-test.sh). Read-only.
-- Fails (non-zero psql exit with ON_ERROR_STOP) if the copy is not usable.
DO $$
DECLARE
    n_tables integer;
    n_triggers integer;
    n_entities integer;
    n_unbalanced integer;
    n_short integer;
    n_posted integer;
    n_log integer;
BEGIN
    SELECT count(*) INTO n_tables FROM pg_tables WHERE schemaname = 'public';
    IF n_tables < 50 THEN
        RAISE EXCEPTION 'restore smoke: only % tables in public', n_tables;
    END IF;

    -- The database-level guards (ledger, access, invoicing, zatca triggers) must come back with the data.
    SELECT count(*) INTO n_triggers FROM pg_trigger WHERE NOT tgisinternal;
    IF n_triggers < 20 THEN
        RAISE EXCEPTION 'restore smoke: only % triggers restored', n_triggers;
    END IF;

    SELECT count(*) INTO n_entities FROM core_entity;
    IF n_entities < 1 THEN
        RAISE EXCEPTION 'restore smoke: no entity in the restored copy';
    END IF;

    -- CLAUDE.md rule 2: every posted entry balances and has 2+ lines.
    SELECT count(*) INTO n_unbalanced FROM (
        SELECT e.id FROM ledger_journalentry e JOIN ledger_journalline l ON l.entry_id = e.id
         WHERE e.status = 'posted' GROUP BY e.id HAVING sum(l.debit) <> sum(l.credit)) x;
    SELECT count(*) INTO n_short FROM (
        SELECT e.id FROM ledger_journalentry e LEFT JOIN ledger_journalline l ON l.entry_id = e.id
         WHERE e.status = 'posted' GROUP BY e.id HAVING count(l.id) < 2) y;
    IF n_unbalanced > 0 OR n_short > 0 THEN
        RAISE EXCEPTION 'restore smoke: % unbalanced and % short posted entries', n_unbalanced, n_short;
    END IF;

    SELECT count(*) INTO n_posted FROM ledger_journalentry WHERE status = 'posted';
    SELECT count(*) INTO n_log FROM core_accesslog;
    RAISE NOTICE 'restore smoke OK: % tables, % triggers, % entities, % posted entries (all balanced), % access-log rows',
        n_tables, n_triggers, n_entities, n_posted, n_log;
END
$$;

-- Trial-balance totals per entity, for the drill record (compare with the production report of the same time).
SELECT en.code AS entity, sum(l.debit) AS total_debit, sum(l.credit) AS total_credit
  FROM ledger_journalline l
  JOIN ledger_journalentry e ON e.id = l.entry_id AND e.status = 'posted'
  JOIN core_entity en ON en.id = e.entity_id
 GROUP BY en.code ORDER BY en.code;
