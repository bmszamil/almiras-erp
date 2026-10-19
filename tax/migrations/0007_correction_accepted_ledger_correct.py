"""Owner decision H4 (tax-returns OQ-45, §10.6): decision ``ledger_already_correct`` for a correction without a
correcting entry, which is no longer a blocking exception (label without "(blocking)"). Choices only; no data."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tax", "0006_review_triggers"),
    ]

    operations = [
        migrations.AlterField(
            model_name="vatexceptionack",
            name="decision",
            field=models.CharField(
                choices=[
                    ("tagged", "Linked to a correction / رُبط بتصحيح"),
                    (
                        "not_return_relevant",
                        "Not relevant to the return / لا يخص الإقرار",
                    ),
                    (
                        "included_by_adjustment",
                        "Included by a manual adjustment / أُدرج بتعديل يدوي",
                    ),
                    (
                        "tax_point_confirmed",
                        "Period confirmed against the tax point / تأكدت الفترة من تاريخ استحقاق الضريبة",
                    ),
                    (
                        "confirmed_correction",
                        "Confirmed as a correcting entry / تأكد أنه قيد تصحيح",
                    ),
                    ("difference_explained", "Difference explained / تم تفسير الفرق"),
                    (
                        "ledger_already_correct",
                        "Ledger already correct; only the filed return was wrong / الدفاتر صحيحة؛ الخطأ في الإقرار المقدَّم فقط",
                    ),
                ],
                max_length=24,
                verbose_name="Decision / القرار",
            ),
        ),
        migrations.AlterField(
            model_name="vatexceptionack",
            name="kind",
            field=models.CharField(
                choices=[
                    (
                        "untagged_vat_journal",
                        "Journal on a VAT account without a tax document / قيد على حساب ضريبة بلا مستند ضريبي",
                    ),
                    (
                        "rejected_document",
                        "Sales document rejected by ZATCA / مستند مبيعات رفضته الهيئة",
                    ),
                    (
                        "tie_out_difference",
                        "Difference to the VAT accounts / فرق مع حسابات الضريبة",
                    ),
                    (
                        "tax_point_fallback",
                        "Sales put in the period of their issue date (no tax point) / مبيعات أُدرجت بتاريخ إصدارها (لا يوجد تاريخ استحقاق الضريبة)",
                    ),
                    (
                        "period_shift",
                        "Document reported in another period than its journal date / مستند أُدرج في فترة غير فترة قيده",
                    ),
                    (
                        "sar_rate_missing",
                        "Foreign-currency sale without a SAR rate / بيع بعملة أجنبية دون سعر صرف بالريال",
                    ),
                    (
                        "purchase_sar_vat_missing",
                        "Foreign-currency bill without the SAR VAT of the supplier / فاتورة مورد بعملة أجنبية دون الضريبة بالريال",
                    ),
                    (
                        "correcting_entry",
                        "Entry left out as a correcting entry / قيد استُبعد كقيد تصحيح",
                    ),
                    (
                        "correction_mismatch",
                        "Correcting entries differ from the correction / قيود التصحيح لا تساوي فرق التصحيح",
                    ),
                    (
                        "correction_without_entry",
                        "Correction without a correcting entry / تصحيح دون قيد تصحيح",
                    ),
                    (
                        "carry_forward_gap",
                        "Earlier period without an approved return (blocking) / فترة سابقة بلا إقرار معتمد (مانع)",
                    ),
                    (
                        "unpaid_bill_12_months",
                        "Input VAT on a bill unpaid after 12 months / ضريبة مدخلات على فاتورة لم تُسدد بعد 12 شهرًا",
                    ),
                ],
                max_length=28,
                verbose_name="Exception / الاستثناء",
            ),
        ),
    ]
