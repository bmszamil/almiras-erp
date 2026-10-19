"""ROLE_SEED_V12 (owner decision H3, 2026-10-19; tax-returns OQ-46): VAT box verification in two steps by two roles.

- accountant: tax.propose_box_verification (proposes a box definition as checked against the ZATCA form).
- approver (CFO): keeps tax.maintain_rates (verifies a proposed definition; never the proposer).
"""
from django.db import migrations

from core.access.seed import ROLE_SEED_V11, ROLE_SEED_V12, apply_seed


def seed(apps, schema_editor):
    apply_seed(apps.get_model("core", "Role"), apps.get_model("core", "RolePermission"), ROLE_SEED_V12)


def unseed(apps, schema_editor):
    apply_seed(apps.get_model("core", "Role"), apps.get_model("core", "RolePermission"), ROLE_SEED_V11)


class Migration(migrations.Migration):
    dependencies = [("core", "0020_seed_access_v11_tax")]

    operations = [migrations.RunPython(seed, unseed)]
