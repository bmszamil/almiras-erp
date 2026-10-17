"""Document-currency amounts with up to 3 decimals (invoicing D27, R93; billing D11): KWD, BHD, OMR, JOD.

Unit prices, discounts, line net and VAT, document totals, receipt, payment, application and allocation amounts
move from numeric(18, 2) to numeric(21, 3). SAR amounts (``total_vat_sar``, ``total_gross_sar``) stay 2 dp.
Existing values are unchanged (100.00 becomes 100.000). The services limit the decimals used to the currency's
own minor units. The PostgreSQL lock triggers (0007 to 0010) compare rows as JSON and name no amount type, so they
need no change.
"""
from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("invoicing", "0011_vendor_kind"),
    ]

    operations = [
        migrations.AlterField(
            model_name="customerreceipt",
            name="amount",
            field=models.DecimalField(
                decimal_places=3, max_digits=21, verbose_name="Amount / المبلغ"
            ),
        ),
        migrations.AlterField(
            model_name="customerreceipt",
            name="bank_charge",
            field=models.DecimalField(
                decimal_places=3,
                default=Decimal("0"),
                max_digits=21,
                verbose_name="Bank charge / رسوم بنكية",
            ),
        ),
        migrations.AlterField(
            model_name="payableallocation",
            name="amount",
            field=models.DecimalField(
                decimal_places=3, max_digits=21, verbose_name="Amount / المبلغ"
            ),
        ),
        migrations.AlterField(
            model_name="paymentapplication",
            name="amount",
            field=models.DecimalField(
                decimal_places=3, max_digits=21, verbose_name="Amount / المبلغ"
            ),
        ),
        migrations.AlterField(
            model_name="purchasedocument",
            name="reverse_charge_vat",
            field=models.DecimalField(
                decimal_places=3,
                default=Decimal("0"),
                max_digits=21,
                verbose_name="Reverse-charge VAT / ضريبة الاحتساب العكسي",
            ),
        ),
        migrations.AlterField(
            model_name="purchasedocument",
            name="total_gross",
            field=models.DecimalField(
                decimal_places=3,
                default=Decimal("0"),
                max_digits=21,
                verbose_name="Total payable / إجمالي المستحق",
            ),
        ),
        migrations.AlterField(
            model_name="purchasedocument",
            name="total_net",
            field=models.DecimalField(
                decimal_places=3,
                default=Decimal("0"),
                max_digits=21,
                verbose_name="Total net / الإجمالي الصافي",
            ),
        ),
        migrations.AlterField(
            model_name="purchasedocument",
            name="total_vat",
            field=models.DecimalField(
                decimal_places=3,
                default=Decimal("0"),
                max_digits=21,
                verbose_name="VAT charged by the vendor / الضريبة التي فرضها المورد",
            ),
        ),
        migrations.AlterField(
            model_name="purchasedocument",
            name="vendor_total_gross",
            field=models.DecimalField(
                decimal_places=3,
                default=Decimal("0"),
                max_digits=21,
                verbose_name="Total on the vendor document / الإجمالي في مستند المورد",
            ),
        ),
        migrations.AlterField(
            model_name="purchasedocument",
            name="vendor_total_vat",
            field=models.DecimalField(
                decimal_places=3,
                default=Decimal("0"),
                max_digits=21,
                verbose_name="VAT on the vendor document / الضريبة في مستند المورد",
            ),
        ),
        migrations.AlterField(
            model_name="purchasedocumentline",
            name="line_discount",
            field=models.DecimalField(
                decimal_places=3,
                default=Decimal("0"),
                max_digits=21,
                verbose_name="Line discount / خصم البند",
            ),
        ),
        migrations.AlterField(
            model_name="purchasedocumentline",
            name="net_amount",
            field=models.DecimalField(
                decimal_places=3,
                default=Decimal("0"),
                max_digits=21,
                verbose_name="Net amount / المبلغ الصافي",
            ),
        ),
        migrations.AlterField(
            model_name="purchasedocumentline",
            name="unit_price",
            field=models.DecimalField(
                decimal_places=3, max_digits=21, verbose_name="Unit price / سعر الوحدة"
            ),
        ),
        migrations.AlterField(
            model_name="purchasedocumentline",
            name="vat_amount",
            field=models.DecimalField(
                decimal_places=3,
                default=Decimal("0"),
                max_digits=21,
                verbose_name="Line VAT (display) / ضريبة البند (للعرض)",
            ),
        ),
        migrations.AlterField(
            model_name="receiptapplication",
            name="amount",
            field=models.DecimalField(
                decimal_places=3, max_digits=21, verbose_name="Amount / المبلغ"
            ),
        ),
        migrations.AlterField(
            model_name="receivableallocation",
            name="amount",
            field=models.DecimalField(
                decimal_places=3, max_digits=21, verbose_name="Amount / المبلغ"
            ),
        ),
        migrations.AlterField(
            model_name="salesdocument",
            name="document_discount",
            field=models.DecimalField(
                decimal_places=3,
                default=Decimal("0"),
                max_digits=21,
                verbose_name="Document discount / خصم المستند",
            ),
        ),
        migrations.AlterField(
            model_name="salesdocument",
            name="total_gross",
            field=models.DecimalField(
                decimal_places=3,
                default=Decimal("0"),
                max_digits=21,
                verbose_name="Total gross / الإجمالي شامل الضريبة",
            ),
        ),
        migrations.AlterField(
            model_name="salesdocument",
            name="total_net",
            field=models.DecimalField(
                decimal_places=3,
                default=Decimal("0"),
                max_digits=21,
                verbose_name="Total net / الإجمالي الصافي",
            ),
        ),
        migrations.AlterField(
            model_name="salesdocument",
            name="total_vat",
            field=models.DecimalField(
                decimal_places=3,
                default=Decimal("0"),
                max_digits=21,
                verbose_name="Total VAT / إجمالي الضريبة",
            ),
        ),
        migrations.AlterField(
            model_name="salesdocumentline",
            name="line_discount",
            field=models.DecimalField(
                decimal_places=3,
                default=Decimal("0"),
                max_digits=21,
                verbose_name="Line discount / خصم البند",
            ),
        ),
        migrations.AlterField(
            model_name="salesdocumentline",
            name="net_amount",
            field=models.DecimalField(
                decimal_places=3,
                default=Decimal("0"),
                max_digits=21,
                verbose_name="Net amount / المبلغ الصافي",
            ),
        ),
        migrations.AlterField(
            model_name="salesdocumentline",
            name="unit_price",
            field=models.DecimalField(
                decimal_places=3, max_digits=21, verbose_name="Unit price / سعر الوحدة"
            ),
        ),
        migrations.AlterField(
            model_name="salesdocumentline",
            name="vat_amount",
            field=models.DecimalField(
                decimal_places=3,
                default=Decimal("0"),
                max_digits=21,
                verbose_name="Line VAT (display) / ضريبة البند (للعرض)",
            ),
        ),
        migrations.AlterField(
            model_name="vendorpayment",
            name="amount",
            field=models.DecimalField(
                decimal_places=3, max_digits=21, verbose_name="Amount / المبلغ"
            ),
        ),
    ]
