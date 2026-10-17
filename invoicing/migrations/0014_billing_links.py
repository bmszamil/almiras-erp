"""Links from sales documents to the billing period or correction they come from (invoicing R60, R73; billing
R52, R56): one non-rejected document per period and per correction (partial unique constraints)."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0001_initial"),
        ("invoicing", "0013_invoice_type_label"),
    ]

    operations = [
        migrations.AddField(
            model_name="salesdocument",
            name="billing_adjustment",
            field=models.ForeignKey(
                blank=True,
                help_text="Set by create_adjustment_from_billing; its lines are read-only for every user (R73) / يُحدَّد عند إنشاء الإشعار من تصحيح الفوترة؛ بنوده للقراءة فقط لكل المستخدمين",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="billing.billingadjustment",
                verbose_name="Billing correction / تصحيح الفوترة",
            ),
        ),
        migrations.AddField(
            model_name="salesdocument",
            name="billing_item",
            field=models.ForeignKey(
                blank=True,
                help_text="Set by create_from_billing; one non-rejected document per period (R60) / يُحدَّد عند الإنشاء من الفوترة؛ مستند واحد غير مرفوض لكل فترة",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="billing.billingperiod",
                verbose_name="Billing period / فترة الفوترة",
            ),
        ),
        migrations.AddConstraint(
            model_name="salesdocument",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("billing_item__isnull", False),
                    models.Q(("status", "rejected"), _negated=True),
                ),
                fields=("billing_item",),
                name="one_live_document_per_billing_period",
            ),
        ),
        migrations.AddConstraint(
            model_name="salesdocument",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("billing_adjustment__isnull", False),
                    models.Q(("status", "rejected"), _negated=True),
                ),
                fields=("billing_adjustment",),
                name="one_live_note_per_billing_adjustment",
            ),
        ),
    ]
