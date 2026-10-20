"""ROLE_SEED_V16 (payroll slice 2, payroll.md §10.8).

- payroll_officer: payroll.prepare_settlement_correction, payroll.prepare_loan_write_off, payroll.manage_cost_items,
  payroll.prepare_cost_report, payroll.record_gosi_fine, payroll.prepare_profit_recognition.
- approver: payroll.approve_settlement_correction (A, P), payroll.approve_cost_item (A), payroll.approve_cost_report
  (A), payroll.approve_gosi_fine (A, P), payroll.approve_profit_recognition (A), payroll.reverse_payment (P).
- managing_partner: the same approvals as fallback (OQ-1) and payroll.approve_loan_write_off (A, P; the group
  parent's top authority, OQ-7 default; other entities' ``ceo`` waits for the access build, A28).
- accountant: payroll.prepare_profit_recognition (§7).
"""
from django.db import migrations

from core.access.seed import ROLE_SEED_V15, ROLE_SEED_V16, apply_seed


def seed(apps, schema_editor):
    apply_seed(apps.get_model("core", "Role"), apps.get_model("core", "RolePermission"), ROLE_SEED_V16)


def unseed(apps, schema_editor):
    apply_seed(apps.get_model("core", "Role"), apps.get_model("core", "RolePermission"), ROLE_SEED_V15)


class Migration(migrations.Migration):
    dependencies = [("core", "0024_seed_access_v15_deferred_sale")]

    operations = [migrations.RunPython(seed, unseed)]
