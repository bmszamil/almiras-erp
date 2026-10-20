"""invoicing.md R112, E28 and payroll.md §5 (CFO approval P19(g), owner decision E8 of 2026-10-18): add the approved
bai' ajel accounts 1241 (deferred profit, contra 1240, monetary), 1250 (assets acquired for employee deferred sale)
and 4610 (bai' ajel profit income) to entities that already have a chart (install_chart_of_accounts adds them to new
entities from the template). 1240 came with payroll migration 0002.

The values are copied here, not imported from coa_template, so this migration keeps doing the same thing when the
template changes later. An entity that already has an account with one of these codes keeps it unchanged.
"""
from django.db import migrations

# code, name_en, name_ar, type, header code, monetary
ACCOUNTS = (
    ("1241", "Deferred bai' ajel profit (contra 1240)", "أرباح البيع الآجل المؤجلة", "asset", "1000", True),
    ("1250", "Assets acquired for employee deferred sale", "أصول مشتراة للبيع الآجل للموظفين", "asset", "1000", False),
    ("4610", "Bai' ajel profit income", "إيرادات أرباح البيع الآجل", "income", "4000", False),
)


def forwards(apps, schema_editor):
    Account = apps.get_model("ledger", "Account")
    for entity_id in Account.objects.filter(code="1000").values_list("entity_id", flat=True):
        for code, en, ar, typ, header, monetary in ACCOUNTS:
            parent = Account.objects.filter(entity_id=entity_id, code=header).first()
            Account.objects.get_or_create(entity_id=entity_id, code=code, defaults=dict(
                name_en=en, name_ar=ar, type=typ, parent=parent, is_postable=True, is_monetary=monetary))


class Migration(migrations.Migration):
    dependencies = [("ledger", "0020_rate_source_sama_tax_point")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
