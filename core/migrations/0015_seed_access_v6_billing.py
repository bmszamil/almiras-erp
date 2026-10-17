"""ROLE_SEED_V6 (billing.md §7 as amended 2026-10-17; access A11): billing version, correction and recompute rights.

- accountant: ``billing.edit_version_draft``, ``billing.review_correction``, ``billing.recompute_corrections``.
- approver: ``billing.edit_version_draft``, ``billing.approve_version`` (A), ``billing.recompute_corrections``.
- receivables_clerk: ``billing.review_correction``; managing_partner: ``billing.approve_version`` (fallback approver).
- system_billing: ``invoicing.create_adjustment_from_billing`` (Sys:billing) and ``ledger.view_rates`` (billing R67).
- ``billing.create_credit_note_draft`` (pre-D9, withdrawn from the catalogue) is removed from every role.
"""
from django.db import migrations

from core.access.seed import ROLE_SEED_V5, ROLE_SEED_V6, apply_seed


def seed(apps, schema_editor):
    apply_seed(apps.get_model("core", "Role"), apps.get_model("core", "RolePermission"), ROLE_SEED_V6)


def unseed(apps, schema_editor):
    apply_seed(apps.get_model("core", "Role"), apps.get_model("core", "RolePermission"), ROLE_SEED_V5)


class Migration(migrations.Migration):
    dependencies = [("core", "0014_seed_access_v5_hr_counts")]

    operations = [migrations.RunPython(seed, unseed)]
