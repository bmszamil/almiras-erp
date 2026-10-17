"""Add the intercompany billing permissions and give them to the system roles (intercompany spec §7)."""
from django.db import migrations


def forwards(apps, schema_editor):
    from access.catalogue import seed

    seed(apps.get_model("access", "Permission"), apps.get_model("access", "Role"))


class Migration(migrations.Migration):
    dependencies = [("access", "0005_reserve_system_user")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
