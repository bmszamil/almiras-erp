"""ROLE_SEED_V2: managing_partner gets the R32 rights (OQ-18 default); system users re-checked (R20).

Re-applying the system users fails loudly if a system username belongs to
any other account, and forces an unusable password and no superuser / staff
flags on every system user.
"""
from django.conf import settings
from django.db import migrations

from core.access.seed import ROLE_SEED_V2, SYSTEM_USERS_V1, apply_seed, apply_system_users


def forwards(apps, schema_editor):
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    apply_seed(apps.get_model("core", "Role"), apps.get_model("core", "RolePermission"), ROLE_SEED_V2)
    apply_system_users(User, apps.get_model("core", "UserProfile"), SYSTEM_USERS_V1)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0008_access_hardening"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
