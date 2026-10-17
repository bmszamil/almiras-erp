"""Reversal mirror check in the database also compares the rate source, rate row, settled line and line kind.

Replaces the entry function of 0012_entry_link_triggers (0012 itself is applied and stays unchanged).
ledger.services._mirrors already compares these columns; the 0012 trigger compared only account, sides,
currency, transaction amount, rate and counterparty, so raw SQL could post a "reversal" whose lines claimed
another rate source or rate row, settled another item, or changed a settlement into a normal line (spec-writer
follow-up L2). Everything else in the function is as in 0012.

On SQLite this migration does nothing; the model and queryset guards apply.
"""
import importlib

from django.db import migrations

_0012 = importlib.import_module("ledger.migrations.0012_entry_link_triggers")

_COLUMNS_0012 = """account_id, debit, credit, currency_id, txn_amount,
                           fx_rate, counterparty_entity_id"""
_COLUMNS = """account_id, debit, credit, currency_id, txn_amount,
                           fx_rate, counterparty_entity_id, rate_source, exchange_rate_id, settles_line_id, line_kind"""
_COMPARE_0012 = """                    OR o.counterparty_entity_id IS DISTINCT FROM r.counterparty_entity_id
"""
_COMPARE = """                    OR o.counterparty_entity_id IS DISTINCT FROM r.counterparty_entity_id
                    OR o.rate_source <> r.rate_source
                    OR o.exchange_rate_id IS DISTINCT FROM r.exchange_rate_id
                    OR o.settles_line_id IS DISTINCT FROM r.settles_line_id
                    OR o.line_kind <> r.line_kind
"""
assert _0012.FORWARD.count(_COLUMNS_0012) == 2 and _0012.FORWARD.count(_COMPARE_0012) == 1
FORWARD = _0012.FORWARD.replace(_COLUMNS_0012, _COLUMNS).replace(_COMPARE_0012, _COMPARE).replace(
    "whose lines do not mirror the original", "whose lines do not mirror the original (all columns)")
REVERSE = _0012.FORWARD  # back to the 0012 function


def _run(schema_editor, sql):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(sql)  # no parameters: the "%" in RAISE messages is not a placeholder


def forwards(apps, schema_editor):
    _run(schema_editor, FORWARD)


def backwards(apps, schema_editor):
    _run(schema_editor, REVERSE)


class Migration(migrations.Migration):
    dependencies = [("ledger", "0013_invoicing_income_accounts")]

    operations = [migrations.RunPython(forwards, backwards)]
