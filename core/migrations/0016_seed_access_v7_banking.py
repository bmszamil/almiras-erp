"""ROLE_SEED_V7 (banking.md §7): banking rights on the seeded roles.

- accountant, group_treasury: view, view_iban, manage_bank_account, import_statement, suggest_match, match,
  create/submit_bank_transaction, prepare_reconciliation (OQ-14 default).
- approver: view, view_iban, approve_bank_account (A, S!), approve_bank_transaction (A, P),
  reverse_bank_transaction (P, OQ-19), approve_reconciliation (A), reopen_reconciliation (S!, OQ-11), manage_settings.
- viewer: view (masked). managing_partner: view, view_iban, every banking A permission (fallback), reopen.
"""
from django.db import migrations

from core.access.seed import ROLE_SEED_V6, ROLE_SEED_V7, apply_seed


def seed(apps, schema_editor):
    apply_seed(apps.get_model("core", "Role"), apps.get_model("core", "RolePermission"), ROLE_SEED_V7)


def unseed(apps, schema_editor):
    apply_seed(apps.get_model("core", "Role"), apps.get_model("core", "RolePermission"), ROLE_SEED_V6)


class Migration(migrations.Migration):
    dependencies = [("core", "0015_seed_access_v6_billing")]

    operations = [migrations.RunPython(seed, unseed)]
