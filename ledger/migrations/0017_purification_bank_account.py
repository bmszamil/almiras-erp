"""invoicing.md R54 amendment D31b (D29): add the PROPOSED account 1140 "Bank – purification account" to entities that
already have a chart (install_chart_of_accounts adds it to new entities). Asset, postable, monetary, a sub-account of
1100 so banking R5 accepts it as the GL account of the designated purification bank account. Its currency is set
when that bank account is approved (banking R6). Stays proposed until the accountant / CFO review (D29).

The values are copied here, not imported from coa_template, so this migration keeps doing the same thing when the
template changes later. An entity that already has its own 1140 keeps it unchanged.
"""
from django.db import migrations

CODE, NAME_EN, NAME_AR, PARENT, HEADER = "1140", "Bank – purification account", "البنك – حساب التطهير", "1100", "1000"


def forwards(apps, schema_editor):
    Account = apps.get_model("ledger", "Account")
    for entity_id in Account.objects.filter(code=HEADER).values_list("entity_id", flat=True):
        parent = (Account.objects.filter(entity_id=entity_id, code=PARENT).first()
                  or Account.objects.filter(entity_id=entity_id, code=HEADER).first())
        Account.objects.get_or_create(
            entity_id=entity_id, code=CODE,
            defaults=dict(name_en=NAME_EN, name_ar=NAME_AR, type="asset", parent=parent, is_postable=True,
                          is_monetary=True),
        )


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0016_wht_accounts"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
