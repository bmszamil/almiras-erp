"""ROLE_SEED_V17 (payroll slice 3, payroll.md §11.7).

- payroll_officer: payroll.prepare_liability_report, payroll.record_mudad_justification.
- accountant: payroll.prepare_liability_report.
- hr_officer: payroll.approve_mudad_justification (A; matrix activity payroll.mudad_justification, code default).
- managing_partner: payroll.approve_mudad_justification as fallback (OQ-1); payroll's seam refuses it for the activity.

The parallel approval-engine build may add its own seed version after core.0025; the two are renumbered at merge.
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
