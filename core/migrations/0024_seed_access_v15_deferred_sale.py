"""ROLE_SEED_V15 (invoicing R112, payroll R47, owner decision E8 of 2026-10-18): the bai' ajel system user.

- New system code ``deferred_sale`` and its system user ``system.deferred_sale`` (SYSTEM_USERS_V2).
- system_deferred_sale: invoicing.create_deferred_sale (creates and issues the sale invoice after the matrix approver
  approved the sale in payroll) and invoicing.post_deferred_profit (P; posts the profit recognised from 1241 to 4610
  as payroll collects the instalments).

The system user still needs a membership with this role in each entity that sells assets to employees (setup data).
"""
from django.conf import settings
from django.db import migrations, models

from core.access.seed import ROLE_SEED_V14, ROLE_SEED_V15, SYSTEM_USERS_V2, apply_seed, apply_system_users


def seed(apps, schema_editor):
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    apply_seed(apps.get_model("core", "Role"), apps.get_model("core", "RolePermission"), ROLE_SEED_V15)
    apply_system_users(User, apps.get_model("core", "UserProfile"), SYSTEM_USERS_V2)


def unseed(apps, schema_editor):
    apply_seed(apps.get_model("core", "Role"), apps.get_model("core", "RolePermission"), ROLE_SEED_V14)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0023_seed_access_v14_final_settlement"),
    ]

    operations = [
        migrations.AlterField(
            model_name="role",
            name="system_code",
            field=models.CharField(
                blank=True,
                choices=[
                    ("billing", "Billing"),
                    ("zatca_queue", "ZATCA queue"),
                    ("fx_import", "FX import"),
                    ("deferred_sale", "Bai' ajel sales"),
                ],
                help_text="For system roles: the system user this role belongs to (R11, R13)",
                max_length=20,
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="userprofile",
            name="system_code",
            field=models.CharField(
                blank=True,
                choices=[
                    ("billing", "Billing"),
                    ("zatca_queue", "ZATCA queue"),
                    ("fx_import", "FX import"),
                    ("deferred_sale", "Bai' ajel sales"),
                ],
                max_length=20,
                null=True,
                unique=True,
            ),
        ),
        migrations.RunPython(seed, unseed),
    ]
