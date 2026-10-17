"""Seed the SA statutory parameters, all ``unverified`` (hr R6, R7; values in hr/statutory_seed.py)."""
from django.db import migrations

from hr import statutory_seed


def seed(apps, schema_editor):
    statutory_seed.apply(apps.get_model("hr", "StatutoryParameter"))


class Migration(migrations.Migration):
    dependencies = [("hr", "0001_initial")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
