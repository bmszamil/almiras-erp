"""ROLE_SEED_V17 (zatca.md §7 as amended 2026-10-21, D-19).

- zatca_admin, viewer: zatca.view_key_events (secret-version retirement events and pending retirements; the
  viewer read-only). Not the accountant, not agents.
"""
from django.db import migrations

from core.access.seed import ROLE_SEED_V16, ROLE_SEED_V17, apply_seed


def seed(apps, schema_editor):
    apply_seed(apps.get_model("core", "Role"), apps.get_model("core", "RolePermission"), ROLE_SEED_V17)


def unseed(apps, schema_editor):
    apply_seed(apps.get_model("core", "Role"), apps.get_model("core", "RolePermission"), ROLE_SEED_V16)


class Migration(migrations.Migration):
    dependencies = [("core", "0025_seed_access_v16_payroll_slice2")]

    operations = [migrations.RunPython(seed, unseed)]
