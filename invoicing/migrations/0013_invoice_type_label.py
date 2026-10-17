"""Label of the ``invoice`` document type: "Invoice / فاتورة" (invoicing R47, R94).

The type label is not the printed title: entities with a VAT number still print "Tax Invoice" (``title_en`` /
``title_ar``), and an entity without one is never shown "Tax Invoice" / "فاتورة ضريبية". Choices only; no data
changes.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("invoicing", "0012_document_currency_three_decimals"),
    ]

    operations = [
        migrations.AlterField(
            model_name="salesdocument",
            name="doc_type",
            field=models.CharField(
                choices=[
                    ("invoice", "Invoice / فاتورة"),
                    ("credit_note", "Credit note / إشعار دائن"),
                    ("debit_note", "Debit note / إشعار مدين"),
                ],
                max_length=12,
                verbose_name="Document type / نوع المستند",
            ),
        ),
    ]
