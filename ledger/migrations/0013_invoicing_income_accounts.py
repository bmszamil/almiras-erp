"""invoicing.md §5 (D16, D18): add the PROPOSED income accounts used by income types to entities
that already have a chart (install_chart_of_accounts adds them to new entities).

The list is copied here, not imported from coa_template, so this migration keeps doing the same
thing when the template changes later. Pending accountant review before merge (D18).
"""
from django.db import migrations

# code, name_en, name_ar, type, header code
ACCOUNTS = (
    ("4120", "Residential rental income (VAT-exempt)", "إيرادات الإيجار السكني (معفاة)", "income", "4000"),
    ("4220", "Management fee income", "إيرادات رسوم الإدارة", "income", "4000"),
    ("4230", "Service charge income", "إيرادات رسوم الخدمات المشتركة", "income", "4000"),
    ("4240", "Service fee income", "إيرادات أتعاب الخدمات", "income", "4000"),
    ("4310", "Intercompany IP and licence income", "إيرادات ملكية فكرية من شركات المجموعة", "income", "4000"),
)


def forwards(apps, schema_editor):
    Account = apps.get_model("ledger", "Account")
    for entity_id in Account.objects.filter(code="1000").values_list("entity_id", flat=True):
        for code, en, ar, typ, header in ACCOUNTS:
            parent = Account.objects.filter(entity_id=entity_id, code=header).first()
            Account.objects.get_or_create(
                entity_id=entity_id, code=code,
                defaults=dict(name_en=en, name_ar=ar, type=typ, parent=parent, is_postable=True),
            )


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0012_entry_link_triggers"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
