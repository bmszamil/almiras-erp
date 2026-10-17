"""hr 3.2 (GOSI check) and review finding 7: ``gosi_cohort`` and ``gosi_first_coverage_date`` (entered from the GOSI
record, never derived from the hire date), and the GOSI number encrypted like other personal data (masked in the
services and the admin). Existing plain GOSI numbers are encrypted in place."""
import hr.crypto
from django.db import migrations


def encrypt_gosi_numbers(apps, schema_editor):
    Employee = apps.get_model("hr", "Employee")
    for emp in Employee.objects.exclude(gosi_number="").iterator():
        if not str(emp.gosi_number).startswith(hr.crypto.PREFIX):
            Employee.objects.filter(pk=emp.pk).update(gosi_number=emp.gosi_number)  # get_prep_value encrypts


class Migration(migrations.Migration):

    dependencies = [
        ("hr", "0012_leave_ledger_corrections"),
    ]

    operations = [
        migrations.AddField(
            model_name="employee",
            name="gosi_cohort",
            field=hr.crypto.EncryptedTextField(
                blank=True,
                choices=[
                    ("legacy_m33", "Legacy (SIL M/33) / النظام السابق (م/33)"),
                    ("new_m273", "New law (SIL M/273) / النظام الجديد (م/273)"),
                ],
                default="",
                plain_max_length=10,
                verbose_name="GOSI cohort / فئة التأمينات",
            ),
        ),
        migrations.AddField(
            model_name="employee",
            name="gosi_first_coverage_date",
            field=hr.crypto.EncryptedDateField(
                blank=True,
                null=True,
                verbose_name="First subject to GOSI on / تاريخ أول خضوع للتأمينات",
            ),
        ),
        migrations.AlterField(
            model_name="employee",
            name="gosi_number",
            field=hr.crypto.EncryptedTextField(
                blank=True,
                default="",
                plain_max_length=20,
                verbose_name="GOSI number / رقم التأمينات",
            ),
        ),
        migrations.RunPython(encrypt_gosi_numbers, migrations.RunPython.noop),
    ]
