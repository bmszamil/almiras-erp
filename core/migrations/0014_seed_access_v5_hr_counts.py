"""hr.md §7: viewers see HR counts only. The viewer role loses the masked ``hr.view`` and gets ``hr.view_counts``
(ROLE_SEED_V5); every other role is unchanged."""
from django.db import migrations

from core.access.seed import ROLE_SEED_V5, apply_seed


def seed(apps, schema_editor):
    apply_seed(apps.get_model("core", "Role"), apps.get_model("core", "RolePermission"), {"viewer": ROLE_SEED_V5["viewer"]})


def unseed(apps, schema_editor):
    from core.access.seed import ROLE_SEED_V4

    apply_seed(apps.get_model("core", "Role"), apps.get_model("core", "RolePermission"), {"viewer": ROLE_SEED_V4["viewer"]})


class Migration(migrations.Migration):
    dependencies = [("core", "0013_seed_access_v4_hr")]

    operations = [migrations.RunPython(seed, unseed)]
