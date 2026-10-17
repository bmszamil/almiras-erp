"""Vendor kind (invoicing D24, R91; AT-93): business, individual or government, as for customers.

Existing vendors become ``business``; new vendors must state their kind (no default on the field).
Individual vendors are personal data: their names are masked in labels without the right (R86).
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("invoicing", "0010_approved_version"),
    ]

    operations = [
        migrations.AddField(
            model_name="vendor",
            name="vendor_kind",
            field=models.CharField(
                choices=[("business", "Business / منشأة"), ("individual", "Individual / فرد"),
                         ("government", "Government / جهة حكومية")],
                default="business",
                help_text="Individuals are personal data: names masked without the right (R91, R86) / "
                          "الأفراد بيانات شخصية: تُخفى أسماؤهم دون الصلاحية",
                max_length=12,
                verbose_name="Vendor kind / نوع المورد",
            ),
            preserve_default=False,
        ),
    ]
