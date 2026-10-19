"""ROLE_SEED_V13 (payroll.md §7, slice 1): payroll rights on the seeded roles and the new ``department_head`` role.

- payroll_officer: view, payslips, inputs (enter and approve, never its own), runs (prepare, submit), loan and bai'
  ajel drafts, payments (record), WPS files, Mudad records.
- hr_officer: approves (= posts) runs (P14; never the MP, P20(a)), inputs, GOSI maternity cases, payslips.
- approver: payments (approve = post), run reversals, settings, maternity legal clearances, payslips, cost reports.
- managing_partner: loans, bai' ajel terms, overtime (route managing_partner) and every A permission as fallback.
- department_head (new): overtime of its own department only.
- accountant, viewer: aggregates and cost reports.
"""
from django.db import migrations

from core.access.seed import ROLE_SEED_V12, ROLE_SEED_V13, apply_seed


def seed(apps, schema_editor):
    apply_seed(apps.get_model("core", "Role"), apps.get_model("core", "RolePermission"), ROLE_SEED_V13)


def unseed(apps, schema_editor):
    Role = apps.get_model("core", "Role")
    apply_seed(Role, apps.get_model("core", "RolePermission"), ROLE_SEED_V12)
    Role.objects.filter(code="department_head", memberships__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0021_seed_access_v12_box_proposal")]

    operations = [migrations.RunPython(seed, unseed)]
