"""invoicing.md §5 (D14, D18, D29): add the PROPOSED withholding-tax accounts 2220 (WHT payable, E20 to E22) and 5920
(WHT borne on gross-up, E21) to entities that already have a chart (install_chart_of_accounts adds them to new
entities). They stay proposed until the accountant / CFO review (D29).

The list is copied here, not imported from coa_template, so this migration keeps doing the same thing when the
template changes later.
"""
from django.db import migrations

# code, name_en, name_ar, type, header code, monetary
ACCOUNTS = (
    ("2220", "Withholding tax payable", "ضريبة الاستقطاع المستحقة", "liability", "2000", True),
    ("5920", "Withholding tax borne (gross-up)", "ضريبة استقطاع محملة على الشركة", "expense", "5000", False),
)


def forwards(apps, schema_editor):
    Account = apps.get_model("ledger", "Account")
    for entity_id in Account.objects.filter(code="1000").values_list("entity_id", flat=True):
        for code, en, ar, typ, header, monetary in ACCOUNTS:
            parent = Account.objects.filter(entity_id=entity_id, code=header).first()
            Account.objects.get_or_create(
                entity_id=entity_id, code=code,
                defaults=dict(name_en=en, name_ar=ar, type=typ, parent=parent, is_postable=True, is_monetary=monetary),
            )


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0015_revaluation_run_trigger"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
