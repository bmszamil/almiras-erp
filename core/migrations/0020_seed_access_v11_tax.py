"""ROLE_SEED_V11 (tax-returns.md §7, VAT slice): VAT return, payment, refund and import VAT rights.

- accountant: tax.view, tax.prepare_return, tax.review_return (not on returns it prepared, R8), tax.record_filing,
  tax.reverse_document (P).
- approver: tax.view, tax.review_return, tax.approve_return (A, P), tax.approve_payment (A, P), tax.change_settings (A),
  tax.record_filing, tax.maintain_rates.
- viewer: tax.view. managing_partner: tax.view and every tax A permission (fallback approver, OQ-1).
"""
from django.db import migrations

from core.access.seed import ROLE_SEED_V10, ROLE_SEED_V11, apply_seed


def seed(apps, schema_editor):
    apply_seed(apps.get_model("core", "Role"), apps.get_model("core", "RolePermission"), ROLE_SEED_V11)


def unseed(apps, schema_editor):
    apply_seed(apps.get_model("core", "Role"), apps.get_model("core", "RolePermission"), ROLE_SEED_V10)


class Migration(migrations.Migration):
    dependencies = [("core", "0019_seed_access_v10_purification")]

    operations = [migrations.RunPython(seed, unseed)]
