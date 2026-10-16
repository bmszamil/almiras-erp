# Entity.base_currency becomes the functional currency, a FK to Currency (ledger-fx.md 3.2).
# Separate from 0006 so the currency rows exist (and are committed) before the FK is added.
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0006_currency"),
    ]

    operations = [
        migrations.AddField(
            model_name="entity",
            name="presentation_currency",
            field=models.ForeignKey(
                blank=True,
                help_text="Group reporting entity only (default SAR, OQ-7) / للكيان المُعِدّ لتقارير المجموعة فقط (الافتراضي الريال، OQ-7)",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.currency",
                verbose_name="Presentation currency / عملة العرض",
            ),
        ),
        migrations.AlterField(
            model_name="entity",
            name="base_currency",
            field=models.ForeignKey(
                db_column="base_currency",
                default="SAR",
                help_text="Must have 2 minor units; locked after the first posting / يجب أن تكون بخانتين عشريتين؛ تُقفل بعد أول ترحيل",
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.currency",
                verbose_name="Functional currency / العملة الوظيفية",
            ),
        ),
    ]
