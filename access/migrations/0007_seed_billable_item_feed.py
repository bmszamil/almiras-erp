"""Add intercompany.billable_item.feed (granted to no role by default; review finding 3)."""
from django.db import migrations


def forwards(apps, schema_editor):
    from access.catalogue import seed

    seed(apps.get_model("access", "Permission"), apps.get_model("access", "Role"))


class Migration(migrations.Migration):
    dependencies = [("access", "0006_seed_intercompany_permissions")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
