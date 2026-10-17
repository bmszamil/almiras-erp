-- Re-applied after every migrate, as erp_owner (deploy/bin/migrate.sh). Idempotent. Plain SQL (no psql
-- meta-commands) so both psql and deploy/bin/dbrole.py can run it.
--
-- Default privileges (roles.sql) already give erp_app data rights on new tables; this file
--   1. re-asserts them on every existing table and sequence,
--   2. removes rights the application never needs or must not have (access.md R33, R45, OQ-10),
--   3. fails if erp_app owns anything or holds a privilege that would let it bypass the triggers.

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO erp_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO erp_app;
REVOKE TRUNCATE, REFERENCES, TRIGGER ON ALL TABLES IN SCHEMA public FROM erp_app;
REVOKE CREATE ON SCHEMA public FROM erp_app;

DO $$
BEGIN
    -- Append-only tables (access R33; OQ-10 "DB grants revoked"): insert and read only.
    IF to_regclass('public.core_accesslog') IS NOT NULL THEN
        REVOKE UPDATE, DELETE ON public.core_accesslog FROM erp_app;
    END IF;
    IF to_regclass('public.core_grantuse') IS NOT NULL THEN
        REVOKE UPDATE, DELETE ON public.core_grantuse FROM erp_app;
    END IF;
    -- Memberships are revoked by one UPDATE (trigger-checked), never deleted.
    IF to_regclass('public.core_membership') IS NOT NULL THEN
        REVOKE DELETE ON public.core_membership FROM erp_app;
    END IF;
    -- Migration history belongs to the migrate step.
    IF to_regclass('public.django_migrations') IS NOT NULL THEN
        REVOKE INSERT, UPDATE, DELETE ON public.django_migrations FROM erp_app;
    END IF;
END
$$;

DO $$
DECLARE
    owned integer;
BEGIN
    SELECT count(*) INTO owned FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relowner = 'erp_app'::regrole;
    IF owned > 0 THEN
        RAISE EXCEPTION 'R45: erp_app owns % relation(s) in public; reassign them to erp_owner', owned;
    END IF;
    SELECT count(*) INTO owned FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public' AND p.proowner = 'erp_app'::regrole;
    IF owned > 0 THEN
        RAISE EXCEPTION 'R45: erp_app owns % function(s) in public', owned;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'erp_app'
                AND (rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls)) THEN
        RAISE EXCEPTION 'R45: erp_app has a forbidden role attribute';
    END IF;
    IF pg_has_role('erp_app', 'erp_owner', 'MEMBER') THEN
        RAISE EXCEPTION 'R45: erp_app is a member of erp_owner';
    END IF;
    RAISE NOTICE 'grants applied; erp_app owns nothing and has no forbidden attribute';
END
$$;
