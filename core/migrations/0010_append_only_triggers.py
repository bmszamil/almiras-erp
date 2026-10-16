"""R33: AccessLog, GrantUse and Membership are append-only in the database too (PostgreSQL only).

- core_accesslog, core_grantuse: every UPDATE and DELETE is rejected.
- core_membership: DELETE is rejected; UPDATE only as the single revocation
  write (revoked_at set on an unrevoked row, nothing but the three
  revocation columns changed).

TRUNCATE (used by test database flushes) does not fire row triggers. On
SQLite this migration does nothing; the model and queryset guards apply.
"""
from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION core_reject_change() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% on % is not allowed: the table is append-only (access.md R33)', TG_OP, TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER core_accesslog_append_only BEFORE UPDATE OR DELETE ON core_accesslog
    FOR EACH ROW EXECUTE FUNCTION core_reject_change();
CREATE TRIGGER core_grantuse_append_only BEFORE UPDATE OR DELETE ON core_grantuse
    FOR EACH ROW EXECUTE FUNCTION core_reject_change();

CREATE OR REPLACE FUNCTION core_membership_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'memberships are never deleted (access.md R9, R33)';
    END IF;
    IF OLD.revoked_at IS NOT NULL THEN
        RAISE EXCEPTION 'a revoked membership is final (access.md R9, R33)';
    END IF;
    IF NEW.revoked_at IS NULL THEN
        RAISE EXCEPTION 'a membership changes only by its revocation (access.md R9, R33)';
    END IF;
    IF (to_jsonb(NEW) - 'revoked_at' - 'revoked_by_id' - 'revoke_reason')
       IS DISTINCT FROM (to_jsonb(OLD) - 'revoked_at' - 'revoked_by_id' - 'revoke_reason') THEN
        RAISE EXCEPTION 'memberships are never edited; revoke and grant a new one (access.md R9, R33)';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER core_membership_append_only BEFORE UPDATE OR DELETE ON core_membership
    FOR EACH ROW EXECUTE FUNCTION core_membership_guard();
"""

REVERSE = r"""
DROP TRIGGER IF EXISTS core_membership_append_only ON core_membership;
DROP TRIGGER IF EXISTS core_grantuse_append_only ON core_grantuse;
DROP TRIGGER IF EXISTS core_accesslog_append_only ON core_accesslog;
DROP FUNCTION IF EXISTS core_membership_guard();
DROP FUNCTION IF EXISTS core_reject_change();
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
    dependencies = [("core", "0009_seed_access_v2")]

    operations = [migrations.RunPython(forwards, backwards)]
