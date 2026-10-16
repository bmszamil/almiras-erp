"""access.md R9 / R33 in the database (PostgreSQL only): a revocation is dated when it is written.

Replaces core_membership_guard from 0010_append_only_triggers (0010 itself
is applied and stays unchanged): in addition, revoked_at must be within 10
seconds of the database clock (clock_timestamp(), not the transaction start)
when the single revocation write happens, so a membership cannot be revoked
"as of" an earlier or later moment. Same tolerance as
core.access.models.REVOCATION_CLOCK_TOLERANCE.
"""
from django.db import migrations

GUARD = r"""
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
    IF NEW.revoked_at < clock_timestamp() - interval '%(tol)s seconds'
       OR NEW.revoked_at > clock_timestamp() + interval '%(tol)s seconds' THEN
        RAISE EXCEPTION 'a revocation is dated now, not in the past or the future (access.md R9, R33)';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

PREVIOUS = r"""
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
"""


def _run(schema_editor, sql):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(sql)  # no parameters: the "%" in RAISE messages is not a placeholder


def forwards(apps, schema_editor):
    _run(schema_editor, GUARD.replace("%(tol)s", "10"))


def backwards(apps, schema_editor):
    _run(schema_editor, PREVIOUS)


class Migration(migrations.Migration):
    dependencies = [("core", "0010_append_only_triggers")]

    operations = [migrations.RunPython(forwards, backwards)]
