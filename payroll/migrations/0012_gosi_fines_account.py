"""payroll.md §5 E14 (CFO approval P19(g)): add 5930 "GOSI late-payment fines / غرامات تأخير التأمينات" to entities
that already have a chart (install_chart_of_accounts adds it to new entities from the template). An ordinary
expense: never interest, no purification flag (R37); tax penalties never go there (P20(f)).

The values are copied here, not imported from coa_template, so this migration keeps doing the same thing when the
template changes later. An entity that already has an account 5930 keeps it unchanged.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    Account = apps.get_model("ledger", "Account")
    for entity_id in Account.objects.filter(code="5000").values_list("entity_id", flat=True):
        parent = Account.objects.filter(entity_id=entity_id, code="5000").first()
        Account.objects.get_or_create(entity_id=entity_id, code="5930", defaults=dict(
            name_en="GOSI late-payment fines", name_ar="غرامات تأخير التأمينات", type="expense", parent=parent,
            is_postable=True, is_monetary=False))


class Migration(migrations.Migration):
    dependencies = [("payroll", "0011_rereview_settlement_guards"), ("ledger", "0021_bai_ajel_accounts")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
