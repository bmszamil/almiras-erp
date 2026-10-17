"""Seed the permission catalogue and starting roles (spec §7, D13)."""
from django.db import migrations


def forwards(apps, schema_editor):
    from access.catalogue import seed

    seed(apps.get_model("access", "Permission"), apps.get_model("access", "Role"))


class Migration(migrations.Migration):
    dependencies = [("access", "0001_initial")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
