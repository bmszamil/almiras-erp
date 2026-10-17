"""Load VAT rates, VATEX reasons, the five note reasons and income types (invoicing.md 3.2)."""
from django.db import migrations


def forwards(apps, schema_editor):
    from invoicing.reference_seed import apply

    apply(apps)


class Migration(migrations.Migration):
    dependencies = [("invoicing", "0001_initial")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
