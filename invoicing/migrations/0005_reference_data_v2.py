"""Reference data V2 (compliance check 2026-10-17, rows 1b, 1c, 2, 10c): VATEX and note-reason texts exactly as
ZATCA publishes them, and VATEX-SA-30 as the default exemption reason of residential rent. V1 (0002) is applied
and stays unchanged."""
from django.db import migrations


def forwards(apps, schema_editor):
    from invoicing.reference_seed import apply_v2

    apply_v2(apps)


class Migration(migrations.Migration):
    dependencies = [("invoicing", "0004_pin_exchange_rate")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
