"""Add permissions from the 17 Oct 2026 review (D15 reverse, period/account management, audit security view)."""
from django.db import migrations


def forwards(apps, schema_editor):
    from access.catalogue import seed

    seed(apps.get_model("access", "Permission"), apps.get_model("access", "Role"))


class Migration(migrations.Migration):
    dependencies = [("access", "0002_seed_roles")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
