"""hr R29, R30, R35, R41 (review findings 1, 2, 6): opening balances as approved adjustments, termination accrual
rows for the partial last month, reversals of those rows on withdrawal, and request warnings (nit 12)."""

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("hr", "0011_parameter_immutability"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="leaveledger",
            name="hr_accrual_once_per_month",
        ),
        migrations.AddField(
            model_name="leaveadjustment",
            name="kind",
            field=models.CharField(
                choices=[
                    ("adjustment", "Adjustment / تسوية"),
                    ("opening", "Opening balance (import) / رصيد افتتاحي (ترحيل)"),
                ],
                default="adjustment",
                max_length=10,
                verbose_name="Kind / النوع",
            ),
        ),
        migrations.AddField(
            model_name="leaverequest",
            name="warnings",
            field=models.JSONField(
                blank=True, default=list, verbose_name="Warnings / تنبيهات"
            ),
        ),
        migrations.AddConstraint(
            model_name="leaveledger",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("kind", "accrual"), ("source_type", "hr_accrue_leave")
                ),
                fields=("employee", "leave_type", "period"),
                name="hr_accrual_once_per_month",
            ),
        ),
        migrations.AddConstraint(
            model_name="leaveledger",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("kind", "accrual"), ("source_type", "hr.termination")
                ),
                fields=("employee", "leave_type", "period", "source_id"),
                name="hr_termination_accrual_once",
            ),
        ),
        migrations.AddConstraint(
            model_name="leaveledger",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("kind", "reversal"), ("source_type", "hr.leaveledger")
                ),
                fields=("source_id",),
                name="hr_ledger_row_reversed_once",
            ),
        ),
    ]
