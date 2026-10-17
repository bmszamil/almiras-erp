"""ROLE_SEED_V9 (migration.md §7): legacy data migration (Falcon) rights.

- accountant: migration.view, migration.manage_account_map, migration.import.
- approver: migration.view, migration.approve_account_map (A: not the map's creator or last modifier).
- viewer: migration.view. managing_partner: migration.view, migration.approve_account_map (fallback, OQ-1).
"""
from django.db import migrations

from core.access.seed import ROLE_SEED_V8, ROLE_SEED_V9, apply_seed


def seed(apps, schema_editor):
    apply_seed(apps.get_model("core", "Role"), apps.get_model("core", "RolePermission"), ROLE_SEED_V9)


def unseed(apps, schema_editor):
    apply_seed(apps.get_model("core", "Role"), apps.get_model("core", "RolePermission"), ROLE_SEED_V8)


class Migration(migrations.Migration):
    dependencies = [("core", "0017_seed_access_v8_charity_recipients")]

    operations = [migrations.RunPython(seed, unseed)]
