"""ledger-fx.md 5.0: add the PROPOSED FX accounts to entities that already have a chart,
and set ``is_monetary`` on their existing accounts (install_chart_of_accounts uses
get_or_create, so re-running it would not update the flag).

The lists are copied here, not imported from coa_template, so this migration keeps
doing the same thing when the template changes later. Pending accountant approval (OQ-6).
"""
from django.db import migrations

MONETARY_CODES = (
    "1100", "1110", "1200", "1210", "1220", "1300", "1310",
    "2100", "2110", "2200", "2210", "2300", "2400", "2500", "2600",
)
# code, name_en, name_ar, type, header code, postable
FX_ACCOUNTS = (
    ("3400", "Foreign currency translation reserve", "احتياطي ترجمة العملات الأجنبية", "equity", "3000", False),
    ("4700", "Foreign exchange gain – realised", "أرباح فروقات العملة المحققة", "income", "4000", True),
    ("4710", "Foreign exchange gain – unrealised", "أرباح فروقات العملة غير المحققة", "income", "4000", True),
    ("5750", "Foreign exchange loss – realised", "خسائر فروقات العملة المحققة", "expense", "5000", True),
    ("5760", "Foreign exchange loss – unrealised", "خسائر فروقات العملة غير المحققة", "expense", "5000", True),
    ("5770", "Currency rounding differences", "فروقات تقريب العملات", "expense", "5000", True),
)


def forwards(apps, schema_editor):
    Account = apps.get_model("ledger", "Account")
    # Only entities that have the template chart (they have the 1000 header).
    for entity_id in Account.objects.filter(code="1000").values_list("entity_id", flat=True):
        for code, en, ar, typ, header, postable in FX_ACCOUNTS:
            parent = Account.objects.filter(entity_id=entity_id, code=header).first()
            Account.objects.get_or_create(
                entity_id=entity_id, code=code,
                defaults=dict(name_en=en, name_ar=ar, type=typ, parent=parent, is_postable=postable),
            )
        # Account rows hold no amounts, so a bulk update changes no balances.
        Account.objects.filter(entity_id=entity_id, code__in=MONETARY_CODES).update(is_monetary=True)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0004_fx_required"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
