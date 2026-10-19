"""Payroll seed data (slice 1):

- ``hr.StatutoryParameter`` rows of payroll.statutory_seed (SA payroll and GOSI rows, ``jurisdiction.active`` for SA
  and EG, the Egypt research rows), all ``unverified`` (hr R7).
- The CFO-approved payroll accounts 1230, 1240, 2330, 2340, 5120 (§5, P19(g)) for entities that already have a
  chart (install_chart_of_accounts adds them to new entities from the template).
- ``PayrollSettings`` (disabled) for every entity with HR settings (R3, OQ-2).
"""
from django.db import migrations

# code, name_en, name_ar, type, header code, monetary (copied so the migration never changes)
ACCOUNTS = (
    ("1230", "Employee loans and advances", "قروض وسلف الموظفين", "asset", "1000", True),
    ("1240", "Employee deferred-sale (bai' ajel) receivable", "ذمم البيع الآجل للموظفين", "asset", "1000", True),
    ("2330", "Salaries payable", "رواتب مستحقة", "liability", "2000", True),
    ("2340", "GOSI payable", "اشتراكات التأمينات الاجتماعية المستحقة", "liability", "2000", True),
    ("5120", "End-of-service benefit expense", "مصروف مكافأة نهاية الخدمة", "expense", "5000", False),
)


def forwards(apps, schema_editor):
    from payroll import statutory_seed

    statutory_seed.apply(apps.get_model("hr", "StatutoryParameter"))
    Account = apps.get_model("ledger", "Account")
    for entity_id in Account.objects.filter(code="1000").values_list("entity_id", flat=True):
        for code, en, ar, typ, header, monetary in ACCOUNTS:
            parent = Account.objects.filter(entity_id=entity_id, code=header).first()
            Account.objects.get_or_create(entity_id=entity_id, code=code, defaults=dict(
                name_en=en, name_ar=ar, type=typ, parent=parent, is_postable=True, is_monetary=monetary))
    Settings = apps.get_model("payroll", "PayrollSettings")
    for hs in apps.get_model("hr", "HrSettings").objects.all():
        bank = Account.objects.filter(entity_id=hs.entity_id, code="1100").first()
        if bank is not None:
            Settings.objects.get_or_create(entity_id=hs.entity_id, defaults=dict(salary_bank_account=bank))


class Migration(migrations.Migration):
    dependencies = [("payroll", "0001_initial")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
