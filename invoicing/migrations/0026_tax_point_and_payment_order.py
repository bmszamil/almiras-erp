"""Tax point, payment received date, payment order date and the SAMA rate row on sales documents (invoicing R100 as
amended by owner decisions G6 / G7, R117 for G1).

Data: every existing document gets ``tax_point_date`` = its issue date with ``tax_point_basis`` = ``legacy`` (not
computed: the VAT return still lists such invoices as ``tax_point_fallback``; tax-returns R17). Drafts and submitted
documents get the computed tax point when they are next edited or issued. On PostgreSQL the backfill runs with the
sales-document lock trigger (0007, 0017) disabled for that one statement, as in 0010.
"""
import django.db.models.deletion
from django.db import migrations, models


def backfill_postgresql(cursor):
    cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
    cursor.execute("ALTER TABLE invoicing_salesdocument DISABLE TRIGGER invoicing_salesdocument_lock")
    cursor.execute("UPDATE invoicing_salesdocument SET tax_point_date = issue_date, tax_point_basis = 'legacy' "
                   "WHERE tax_point_date IS NULL")
    cursor.execute("ALTER TABLE invoicing_salesdocument ENABLE TRIGGER invoicing_salesdocument_lock")
    cursor.execute("SET CONSTRAINTS ALL DEFERRED")


def forwards(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            backfill_postgresql(cursor)
    else:
        SalesDocument = apps.get_model("invoicing", "SalesDocument")
        SalesDocument._base_manager.filter(tax_point_date__isnull=True).update(
            tax_point_date=models.F("issue_date"), tax_point_basis="legacy")


class Migration(migrations.Migration):

    dependencies = [
        ("invoicing", "0025_purchase_vendor_vat_sar"),
        ("ledger", "0018_sama_non_publication_days"),
    ]

    operations = [
        migrations.AddField(
            model_name="salesdocument",
            name="payment_order_date",
            field=models.DateField(
                blank=True,
                help_text="Government customers (B2G, VAT IR Art. 20(5)): ZATCA TaxPointDate (BR-KSA-92); required before a standard document is issued (G1) / العملاء الحكوميون (المادة 20(5) من اللائحة التنفيذية): تاريخ الاستحقاق الضريبي لدى الهيئة؛ مطلوب قبل إصدار المستند القياسي",
                null=True,
                verbose_name="Payment order date / تاريخ أمر الدفع",
            ),
        ),
        migrations.AddField(
            model_name="salesdocument",
            name="payment_received_date",
            field=models.DateField(
                blank=True,
                help_text="First payment or advance received for this supply, if before the invoice (tax point, R100) / أول دفعة أو دفعة مقدمة مستلمة عن هذا التوريد إن سبقت الفاتورة (نقطة الاستحقاق الضريبي)",
                null=True,
                verbose_name="Payment or advance received on / تاريخ استلام الدفعة أو الدفعة المقدمة",
            ),
        ),
        migrations.AddField(
            model_name="salesdocument",
            name="tax_point_basis",
            field=models.CharField(
                blank=True,
                choices=[
                    ("supply", "Supply date / تاريخ التوريد"),
                    ("supply_end", "End of the supply period / نهاية فترة التوريد"),
                    (
                        "payment",
                        "Payment or advance received / استلام الدفعة أو الدفعة المقدمة",
                    ),
                    ("issue", "Issue date / تاريخ الإصدار"),
                    (
                        "manual",
                        "Entered by hand, with a reason / مُدخل يدويًا مع السبب",
                    ),
                    (
                        "legacy",
                        "Issue date (set by migration, not computed) / تاريخ الإصدار (من الترحيل، غير محسوب)",
                    ),
                ],
                max_length=10,
                verbose_name="Tax point taken from / مصدر تاريخ الاستحقاق الضريبي",
            ),
        ),
        migrations.AddField(
            model_name="salesdocument",
            name="tax_point_date",
            field=models.DateField(
                blank=True,
                help_text="Earliest of supply (end of the supply period), issue and payment; notes: their own date. Decides the VAT return period and the SAMA rate for VAT in SAR (R100, G6, G7) / الأسبق من التوريد (نهاية فترة التوريد) والإصدار والدفع؛ الإشعارات: تاريخها. يحدد فترة الإقرار وسعر البنك المركزي لضريبة القيمة المضافة بالريال",
                null=True,
                verbose_name="Tax point date / تاريخ الاستحقاق الضريبي",
            ),
        ),
        migrations.AddField(
            model_name="salesdocument",
            name="tax_point_reason",
            field=models.TextField(
                blank=True,
                verbose_name="Reason for the tax point entered by hand / سبب إدخال تاريخ الاستحقاق الضريبي يدويًا",
            ),
        ),
        migrations.AddField(
            model_name="salesdocument",
            name="vat_sar_exchange_rate",
            field=models.ForeignKey(
                blank=True,
                help_text="Pinned at approval: the SAMA rate on the tax point (R100, G7) / يُثبَّت عند الاعتماد: سعر البنك المركزي في تاريخ الاستحقاق الضريبي",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="ledger.exchangerate",
                verbose_name="SAMA rate row for VAT in SAR / سجل سعر البنك المركزي لضريبة القيمة المضافة بالريال",
            ),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
