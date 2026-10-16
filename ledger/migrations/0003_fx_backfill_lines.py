"""R38 / R42 / AT-2: give every existing journal line its currency fields.

Every line written before multi-currency is in its entity's functional
currency, so: currency = entity base_currency, fx_rate = 1,
rate_source = functional, txn_amount = debit + credit, line_kind = normal.

Why ``QuerySet.update()``: ``JournalLine.save`` refuses any write to a line of
a posted entry (CLAUDE.md rule 1), and most existing lines are posted. This is
the one place the lock is bypassed, and only for the new columns: debit,
credit, account, counterparty and the entry (status, number, date) are not
touched, so functional amounts and trial balances are identical before and
after. The code-reviewer must approve this bypass (R38). Covered by
ledger/test_fx_migration.py.
"""
from decimal import Decimal

from django.db import migrations
from django.db.models import F


def backfill(apps, schema_editor):
    Entity = apps.get_model("core", "Entity")
    JournalLine = apps.get_model("ledger", "JournalLine")
    for entity_id, currency in Entity.objects.values_list("pk", "base_currency"):
        JournalLine.objects.filter(entry__entity_id=entity_id, currency__isnull=True).update(
            currency_id=currency,
            fx_rate=Decimal("1"),
            rate_source="functional",
            line_kind="normal",
            txn_amount=F("debit") + F("credit"),
        )


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0002_fx_schema"),
    ]

    # Reverse is a no-op: 0002's reverse drops the columns.
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
