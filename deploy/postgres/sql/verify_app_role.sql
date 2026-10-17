-- access.md AT-47 / R45 deployment check. Run CONNECTED AS THE APPLICATION ROLE (erp_app) against the migrated
-- database: deploy/bin/dbrole.py verify (or psql -v ON_ERROR_STOP=1 -1 -f verify_app_role.sql).
-- Every forbidden statement must fail with insufficient_privilege (SQLSTATE 42501). If one succeeds, the block
-- raises 'R45 FAIL', the whole transaction rolls back (nothing is changed) and the command exits non-zero:
-- go-live fails. Plain SQL, no psql meta-commands. Run inside one transaction that is rolled back.

DO $$
DECLARE
    trig_name text;
    trig_func text;
    r record;
BEGIN
    IF current_user <> 'erp_app' THEN
        RAISE EXCEPTION 'R45 check must run as erp_app, not %', current_user;
    END IF;

    -- Role attributes (rolsuper, rolcreaterole, rolbypassrls, ...) all false.
    SELECT * INTO r FROM pg_roles WHERE rolname = current_user;
    IF r.rolsuper OR r.rolcreaterole OR r.rolcreatedb OR r.rolreplication OR r.rolbypassrls THEN
        RAISE EXCEPTION 'R45 FAIL: erp_app has a forbidden attribute (super=% createrole=% createdb=% replication=% bypassrls=%)',
            r.rolsuper, r.rolcreaterole, r.rolcreatedb, r.rolreplication, r.rolbypassrls;
    END IF;
    RAISE NOTICE 'PASS role attributes all false';

    -- Owns no table, sequence, view, function or schema.
    IF EXISTS (SELECT 1 FROM pg_class WHERE relowner = current_user::regrole)
       OR EXISTS (SELECT 1 FROM pg_proc WHERE proowner = current_user::regrole)
       OR EXISTS (SELECT 1 FROM pg_namespace WHERE nspowner = current_user::regrole)
       OR EXISTS (SELECT 1 FROM pg_database WHERE datdba = current_user::regrole) THEN
        RAISE EXCEPTION 'R45 FAIL: erp_app owns a database object';
    END IF;
    RAISE NOTICE 'PASS owns no table, function, schema or database';

    IF to_regclass('public.invoicing_salesdocument') IS NULL OR to_regclass('public.core_accesslog') IS NULL THEN
        RAISE EXCEPTION 'R45 check needs a migrated database (invoicing_salesdocument, core_accesslog missing)';
    END IF;
    SELECT t.tgname, p.proname INTO trig_name, trig_func
      FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid
     WHERE t.tgrelid = 'public.invoicing_salesdocument'::regclass AND NOT t.tgisinternal
     ORDER BY t.tgname LIMIT 1;
    IF trig_name IS NULL THEN
        RAISE EXCEPTION 'R45 check: no trigger on invoicing_salesdocument (were the migrations run on PostgreSQL?)';
    END IF;

    BEGIN
        EXECUTE 'ALTER TABLE public.invoicing_salesdocument DISABLE TRIGGER ALL';
        RAISE EXCEPTION 'R45 FAIL: ALTER TABLE ... DISABLE TRIGGER ALL succeeded';
    EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'PASS ALTER TABLE DISABLE TRIGGER refused';
    END;

    BEGIN
        EXECUTE 'SET LOCAL session_replication_role = replica';
        RAISE EXCEPTION 'R45 FAIL: SET session_replication_role succeeded';
    EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'PASS SET session_replication_role refused';
    END;

    BEGIN
        EXECUTE format('CREATE OR REPLACE FUNCTION public.%I() RETURNS trigger LANGUAGE plpgsql AS '
                       '$f$ BEGIN RETURN NEW; END $f$', trig_func);
        RAISE EXCEPTION 'R45 FAIL: CREATE OR REPLACE FUNCTION %() succeeded', trig_func;
    EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'PASS CREATE OR REPLACE FUNCTION %() refused', trig_func;
    END;

    BEGIN
        EXECUTE format('DROP FUNCTION public.%I() CASCADE', trig_func);
        RAISE EXCEPTION 'R45 FAIL: DROP FUNCTION %() succeeded', trig_func;
    EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'PASS DROP FUNCTION refused';
    END;

    BEGIN
        EXECUTE format('DROP TRIGGER %I ON public.invoicing_salesdocument', trig_name);
        RAISE EXCEPTION 'R45 FAIL: DROP TRIGGER % succeeded', trig_name;
    EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'PASS DROP TRIGGER % refused', trig_name;
    END;

    BEGIN
        EXECUTE 'CREATE TRIGGER r45_probe BEFORE UPDATE ON public.core_accesslog '
                'FOR EACH ROW EXECUTE FUNCTION suppress_redundant_updates_trigger()';
        RAISE EXCEPTION 'R45 FAIL: CREATE TRIGGER succeeded';
    EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'PASS CREATE TRIGGER refused';
    END;

    BEGIN
        EXECUTE 'TRUNCATE public.core_accesslog';
        RAISE EXCEPTION 'R45 FAIL: TRUNCATE core_accesslog succeeded';
    EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'PASS TRUNCATE core_accesslog refused';
    END;

    BEGIN
        EXECUTE 'UPDATE public.core_accesslog SET id = id WHERE false';
        RAISE EXCEPTION 'R45 FAIL: UPDATE core_accesslog allowed (OQ-10: app role has no UPDATE)';
    EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'PASS UPDATE core_accesslog refused';
    END;

    BEGIN
        EXECUTE 'DELETE FROM public.core_accesslog WHERE false';
        RAISE EXCEPTION 'R45 FAIL: DELETE core_accesslog allowed (OQ-10: app role has no DELETE)';
    EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'PASS DELETE core_accesslog refused';
    END;

    BEGIN
        EXECUTE 'CREATE TABLE public.r45_probe (id int)';
        RAISE EXCEPTION 'R45 FAIL: CREATE TABLE in public succeeded';
    EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'PASS CREATE TABLE refused';
    END;

    BEGIN
        EXECUTE 'CREATE TEMPORARY TABLE r45_probe (id int)';
        RAISE EXCEPTION 'R45 FAIL: CREATE TEMPORARY TABLE succeeded (pg_temp functions could shadow objects)';
    EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'PASS CREATE TEMPORARY TABLE refused';
    END;

    BEGIN
        EXECUTE 'ALTER TABLE public.invoicing_salesdocument OWNER TO erp_app';
        RAISE EXCEPTION 'R45 FAIL: ALTER TABLE OWNER succeeded';
    EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'PASS ALTER TABLE OWNER refused';
    END;

    BEGIN
        EXECUTE 'SET LOCAL ROLE erp_owner';
        RAISE EXCEPTION 'R45 FAIL: SET ROLE erp_owner succeeded';
    EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'PASS SET ROLE erp_owner refused';
    END;

    RAISE NOTICE 'R45 / AT-47: all checks passed for %', current_user;
END
$$;
