"""ROLE_SEED_V10 (owner decisions 2026-10-18 C2 and C4; invoicing R54).

- approver (CFO): invoicing.approve_purification_funding (A, P: never the person who recorded the funding).
- managing_partner: invoicing.approve_purification_funding (fallback approver, every A permission, OQ-1).
- accountant: invoicing.mark_purification_instruction_sent (marks purification bank instructions exported or sent).
"""
from django.db import migrations

from core.access.seed import ROLE_SEED_V9, ROLE_SEED_V10, apply_seed


def seed(apps, schema_editor):
    apply_seed(apps.get_model("core", "Role"), apps.get_model("core", "RolePermission"), ROLE_SEED_V10)


def unseed(apps, schema_editor):
    apply_seed(apps.get_model("core", "Role"), apps.get_model("core", "RolePermission"), ROLE_SEED_V9)


class Migration(migrations.Migration):
    dependencies = [("core", "0018_seed_access_v9_migration")]

    operations = [migrations.RunPython(seed, unseed)]
