-- Almiras ERP database roles (access.md R45, OQ-10; zatca R-43). Run ONCE by the PostgreSQL superuser on a new
-- cluster, connected to the application database, by postgres/initdb/10-almiras-roles.sh. psql variables
-- :owner_password, :app_password, :backup_password, :monitor_password come from Docker secret files; no
-- password is written here.
--
--   erp_owner   owns the database, the public schema and every application table, function and trigger.
--               Used only by the migrate step at deployment (deploy/bin/migrate.sh). Never by the running app.
--   erp_app     what Django connects with in production (web and scheduler). Data rights only: SELECT, INSERT,
--               UPDATE, DELETE on tables and USAGE on sequences, granted by default privileges of erp_owner.
--               Not superuser, owns nothing, no CREATEDB / CREATEROLE / REPLICATION / BYPASSRLS, no CREATE on the
--               database or schema, no TRUNCATE / TRIGGER / REFERENCES. It therefore cannot ALTER TABLE ...
--               DISABLE TRIGGER, DROP or CREATE OR REPLACE a function or trigger, TRUNCATE, or SET
--               session_replication_role (superuser-only). Checked by sql/verify_app_role.sql (AT-47).
--   erp_backup  read-only (pg_read_all_data, default_transaction_read_only) for pg_dump, and REPLICATION for the
--               optional pg_basebackup used with WAL archiving (backup/pg-basebackup.sh). Not used by the app.
--   erp_monitor pg_monitor for the Prometheus postgres exporter.

-- The database must be UTF8 with the C.UTF-8 locale (initdb -E UTF8 --locale=C.UTF-8).
DO $$
DECLARE
    enc text;
    coll text;
BEGIN
    SELECT pg_encoding_to_char(encoding), datcollate INTO enc, coll FROM pg_database WHERE datname = current_database();
    IF enc <> 'UTF8' THEN
        RAISE EXCEPTION 'database % has encoding %, expected UTF8', current_database(), enc;
    END IF;
    IF coll NOT IN ('C.UTF-8', 'C.utf8') THEN
        RAISE EXCEPTION 'database % has collation %, expected C.UTF-8', current_database(), coll;
    END IF;
END
$$;

CREATE ROLE erp_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT
    PASSWORD :'owner_password';
CREATE ROLE erp_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT
    CONNECTION LIMIT 200 PASSWORD :'app_password';
CREATE ROLE erp_backup LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE REPLICATION NOBYPASSRLS INHERIT
    CONNECTION LIMIT 5 PASSWORD :'backup_password';
CREATE ROLE erp_monitor LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT
    CONNECTION LIMIT 5 PASSWORD :'monitor_password';

GRANT pg_read_all_data TO erp_backup;
GRANT pg_monitor TO erp_monitor;

-- Ownership: the owner role, never the application role.
SELECT format('ALTER DATABASE %I OWNER TO erp_owner', current_database()) \gexec
ALTER SCHEMA public OWNER TO erp_owner;

-- Nobody but the owner creates objects or temporary tables; the others may only connect.
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database()) \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO erp_app, erp_backup, erp_monitor', current_database()) \gexec
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO erp_app, erp_backup, erp_monitor;

-- Data rights on everything erp_owner creates from now on (migrations). No TRUNCATE, TRIGGER or REFERENCES.
ALTER DEFAULT PRIVILEGES FOR ROLE erp_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO erp_app;
ALTER DEFAULT PRIVILEGES FOR ROLE erp_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO erp_app;

-- Session safety nets for the application role (the migrate step is not affected).
ALTER ROLE erp_app SET idle_in_transaction_session_timeout = '5min';
ALTER ROLE erp_app SET lock_timeout = '30s';
ALTER ROLE erp_backup SET default_transaction_read_only = on;
