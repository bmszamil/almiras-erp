"""ROLE_SEED_V14 (payroll review 2026-10-19, compliance C-A11): final settlements.

- payroll_officer: payroll.prepare_final_settlement.
- approver: payroll.approve_final_settlement (A, P; payroll OQ-36 default).
- managing_partner: payroll.approve_final_settlement as fallback (OQ-1).
"""
from django.db import migrations

from core.access.seed import ROLE_SEED_V13, ROLE_SEED_V14, apply_seed


def seed(apps, schema_editor):
    apply_seed(apps.get_model("core", "Role"), apps.get_model("core", "RolePermission"), ROLE_SEED_V14)


def unseed(apps, schema_editor):
    apply_seed(apps.get_model("core", "Role"), apps.get_model("core", "RolePermission"), ROLE_SEED_V13)


class Migration(migrations.Migration):
    dependencies = [("core", "0022_seed_access_v13_payroll")]

    operations = [migrations.RunPython(seed, unseed)]
