"""Payroll review 5 (2026-10-21, item 3; payroll OQ-33): a bai' ajel profit recognition may have no new collection
(0) when it catches up the profit held back while a rebate credit note ZATCA later rejected was pending."""

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0027_seed_access_v17_payroll_slice3"),
        ("invoicing", "0032_review_fixes"),
        ("ledger", "0021_bai_ajel_accounts"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="deferredprofitrelease",
            name="deferred_profit_collected_positive",
        ),
        migrations.AddConstraint(
            model_name="deferredprofitrelease",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("collected_amount__gt", 0),
                    models.Q(
                        ("collected_amount", 0),
                        ("source_ref__startswith", "payroll.deferredsalerebate:"),
                        ("source_ref__contains", ":catch_up"),
                    ),
                    _connector="OR",
                ),
                name="deferred_profit_collected_positive",
            ),
        ),
    ]
