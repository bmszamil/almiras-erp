"""ledger-fx.md 3.5: after 0003 has filled every line, currency and txn_amount become required
and the line checks are added. Separate migration so PostgreSQL does not alter a table
with pending deferred-constraint events from the backfill.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0007_entity_functional_currency"),
        ("ledger", "0003_fx_backfill_lines"),
    ]

    operations = [
        migrations.AlterField(
            model_name="journalline",
            name="currency",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.currency",
                verbose_name="Transaction currency / عملة المعاملة",
            ),
        ),
        migrations.AlterField(
            model_name="journalline",
            name="txn_amount",
            field=models.DecimalField(
                decimal_places=3,
                help_text="In the transaction currency, same side as debit/credit / بعملة المعاملة، في نفس جانب المدين/الدائن",
                max_digits=21,
                verbose_name="Transaction amount / مبلغ المعاملة",
            ),
        ),
        migrations.AddConstraint(
            model_name="journalline",
            constraint=models.CheckConstraint(
                condition=models.Q(("txn_amount__gte", 0)),
                name="line_txn_amount_not_negative",
            ),
        ),
        migrations.AddConstraint(
            model_name="journalline",
            constraint=models.CheckConstraint(
                condition=models.Q(("fx_rate__isnull", True), ("fx_rate__gt", 0), _connector="OR"),
                name="line_fx_rate_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="journalline",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("fx_rate__isnull", True), ("line_kind", "revaluation"), ("txn_amount", 0)),
                    models.Q(
                        models.Q(("line_kind", "revaluation"), _negated=True),
                        models.Q(("fx_rate__isnull", True), ("txn_amount", 0), _negated=True),
                    ),
                    _connector="OR",
                ),
                name="line_revaluation_kind",
            ),
        ),
    ]
