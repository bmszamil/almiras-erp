"""Test documents record their creator (ledger.documents.register requires it) and an amount for validate_lines.

Test-only app: its tables hold no rows outside a test run, so making
``created_by`` non-null needs no default.
"""
from decimal import Decimal

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("testdocs", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="bill",
            name="created_by",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, related_name="+", to=settings.AUTH_USER_MODEL
            ),
        ),
        migrations.AddField(
            model_name="bill",
            name="amount",
            field=models.DecimalField(
                decimal_places=2, default=Decimal("10.00"), max_digits=18, verbose_name="Amount / المبلغ"
            ),
        ),
        migrations.AlterField(
            model_name="receipt",
            name="created_by",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, related_name="+", to=settings.AUTH_USER_MODEL
            ),
        ),
        migrations.AddField(
            model_name="receipt",
            name="amount",
            field=models.DecimalField(
                decimal_places=2, default=Decimal("10.00"), max_digits=18, verbose_name="Amount / المبلغ"
            ),
        ),
        migrations.AlterField(
            model_name="salesdocument",
            name="created_by",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, related_name="+", to=settings.AUTH_USER_MODEL
            ),
        ),
        migrations.AddField(
            model_name="salesdocument",
            name="amount",
            field=models.DecimalField(
                decimal_places=2, default=Decimal("10.00"), max_digits=18, verbose_name="Amount / المبلغ"
            ),
        ),
        migrations.AlterField(
            model_name="unregistered",
            name="created_by",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, related_name="+", to=settings.AUTH_USER_MODEL
            ),
        ),
        migrations.AddField(
            model_name="unregistered",
            name="amount",
            field=models.DecimalField(
                decimal_places=2, default=Decimal("10.00"), max_digits=18, verbose_name="Amount / المبلغ"
            ),
        ),
    ]
