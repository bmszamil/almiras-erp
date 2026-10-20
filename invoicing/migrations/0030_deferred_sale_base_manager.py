"""R123: the bai' ajel tables use their guarded manager as ``_base_manager`` too, like the other guarded models (review
6 pattern), so no bulk write bypasses invoicing.services."""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("invoicing", "0029_deferred_sale_data_and_guards"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="deferredprofitrelease",
            options={
                "base_manager_name": "objects",
                "ordering": ["deferred_sale", "date", "id"],
                "verbose_name": "bai' ajel profit recognition / تحقق أرباح البيع الآجل",
                "verbose_name_plural": "bai' ajel profit recognitions / تحقق أرباح البيع الآجل",
            },
        ),
        migrations.AlterModelOptions(
            name="deferredsale",
            options={
                "base_manager_name": "objects",
                "ordering": ["entity", "-created_at"],
                "verbose_name": "bai' ajel sale / بيع آجل",
                "verbose_name_plural": "bai' ajel sales / عمليات البيع الآجل",
            },
        ),
    ]
