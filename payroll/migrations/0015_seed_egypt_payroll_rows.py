"""Payroll slice 2 (§10.8 item 7): the Egypt rows the local calculation reads (insurable components, partial month,
the salary-tax band exclusion table, the residents-only zero band, the withholding method, Universal Health
Insurance rates and live governorates, no monthly EOSB accrual), from docs/compliance/egypt-payroll-2026-10-18.md.
All ``unverified`` (hr R7): an Egypt run is refused until a person verifies every row it reads.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    from payroll import statutory_seed

    statutory_seed.apply(apps.get_model("hr", "StatutoryParameter"))


class Migration(migrations.Migration):
    dependencies = [("payroll", "0014_slice2_guards")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
