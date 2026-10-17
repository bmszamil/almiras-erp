"""ROLE_SEED_V3 (access.md A17, R47, R48): the group-level ``chairman`` role and the Sharia switch-off right.

- ``chairman`` (group, human) holds only ``core.disable_sharia_enforcement`` (the second approval).
- ``managing_partner`` also holds it (request = first approval, then apply).
- PostgreSQL only: at most one active ``chairman`` membership per group parent entity (R47). A plain partial
  unique index cannot express "overlapping in time", so a BEFORE INSERT trigger checks overlapping unrevoked
  chairman memberships of the same entity, after locking the role row so two grants at the same moment run one
  after the other. The model check in ``Membership.save`` applies on SQLite.
"""
from django.conf import settings
from django.db import migrations

from core.access.seed import ROLE_SEED_V3, SYSTEM_USERS_V1, apply_seed, apply_system_users

FORWARD = r"""
CREATE OR REPLACE FUNCTION core_one_chairman() RETURNS trigger AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM core_role WHERE id = NEW.role_id AND code = 'chairman') THEN
        PERFORM 1 FROM core_role WHERE id = NEW.role_id FOR UPDATE;
        IF EXISTS (
            SELECT 1 FROM core_membership m
            WHERE m.role_id = NEW.role_id AND m.entity_id = NEW.entity_id AND m.id IS DISTINCT FROM NEW.id
              AND (m.revoked_at IS NULL OR m.revoked_at > NEW.valid_from)
              AND (m.valid_to IS NULL OR m.valid_to > NEW.valid_from)
              AND (NEW.valid_to IS NULL OR m.valid_from < NEW.valid_to)
        ) THEN
            RAISE EXCEPTION 'one active chairman per group parent entity (access.md R47)';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER core_membership_one_chairman BEFORE INSERT ON core_membership
    FOR EACH ROW EXECUTE FUNCTION core_one_chairman();
"""

REVERSE = r"""
DROP TRIGGER IF EXISTS core_membership_one_chairman ON core_membership;
DROP FUNCTION IF EXISTS core_one_chairman();
"""


def seed(apps, schema_editor):
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    apply_seed(apps.get_model("core", "Role"), apps.get_model("core", "RolePermission"), ROLE_SEED_V3)
    apply_system_users(User, apps.get_model("core", "UserProfile"), SYSTEM_USERS_V1)


def _run(schema_editor, sql):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(sql)  # no parameters


def forwards(apps, schema_editor):
    _run(schema_editor, FORWARD)


def backwards(apps, schema_editor):
    _run(schema_editor, REVERSE)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0011_membership_revocation_time"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
        migrations.RunPython(forwards, backwards),
    ]
