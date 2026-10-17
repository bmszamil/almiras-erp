"""ROLE_SEED_V8 (invoicing R54 amendment D31b): charity recipients of purification payouts.

- accountant: invoicing.manage_charity_recipient (add and deactivate recipients).
- managing_partner: invoicing.approve_charity_recipient (A, S!: not the person who added the recipient).
"""
from django.db import migrations

from core.access.seed import ROLE_SEED_V7, ROLE_SEED_V8, apply_seed


def seed(apps, schema_editor):
    apply_seed(apps.get_model("core", "Role"), apps.get_model("core", "RolePermission"), ROLE_SEED_V8)


def unseed(apps, schema_editor):
    apply_seed(apps.get_model("core", "Role"), apps.get_model("core", "RolePermission"), ROLE_SEED_V7)


class Migration(migrations.Migration):
    dependencies = [("core", "0016_seed_access_v7_banking")]

    operations = [migrations.RunPython(seed, unseed)]
