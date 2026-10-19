"""Owner decision H2 (ledger-fx R51): rate source ``sama_tax_point`` for sales document lines booked at the SAMA
rate on the tax point. The column widens from 10 to 16 characters; the PostgreSQL triggers read it in function
bodies only (reversal mirror, ``0014``), so the change needs no trigger update."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ledger", "0019_non_publication_day_verification"),
    ]

    operations = [
        migrations.AlterField(
            model_name="journalline",
            name="rate_source",
            field=models.CharField(
                choices=[
                    (
                        "functional",
                        "Functional currency (rate 1) / العملة الوظيفية (سعر 1)",
                    ),
                    ("table", "Rate table / جدول الأسعار"),
                    ("manual", "Manual rate / سعر يدوي"),
                    ("carrying", "Carrying rate (settlement) / السعر الدفتري (تسوية)"),
                    (
                        "sama_tax_point",
                        "SAMA rate on the tax point / سعر البنك المركزي في تاريخ استحقاق الضريبة",
                    ),
                ],
                default="functional",
                max_length=16,
                verbose_name="Rate source / مصدر السعر",
            ),
        ),
    ]
