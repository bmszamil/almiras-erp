"""R38, R42 / AT-2: the backfill migration fills the currency fields and changes nothing else.

Migrates ledger back to 0001 (before multi-currency), books posted SAR entries with the
historical models, then migrates forward and compares. Transactional because migrations
cannot run inside the test transaction; the database is flushed afterwards, so the
currencies are seeded here (as the access-log tests seed roles).
"""
from datetime import date
from decimal import Decimal

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from core.currency_seed import apply_currency_seed

pytestmark = pytest.mark.django_db(transaction=True)
D = Decimal
BEFORE = [("core", "0007_entity_functional_currency"), ("ledger", "0001_initial")]


def _migrate(targets):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)
    return MigrationExecutor(connection).loader.project_state(targets).apps


def _leaf_targets():
    executor = MigrationExecutor(connection)
    return [node for node in executor.loader.graph.leaf_nodes() if node[0] in ("core", "ledger")]


def _snapshot(apps):
    Line = apps.get_model("ledger", "JournalLine")
    Entry = apps.get_model("ledger", "JournalEntry")
    lines = sorted(Line.objects.values_list("pk", "entry_id", "account_id", "debit", "credit", "counterparty_entity_id"))
    entries = sorted(Entry.objects.values_list("pk", "number", "status", "date", "posted_at", "reversal_of_id"))
    tb = {}
    for _, _, account, debit, credit, _ in lines:
        tb[account] = tb.get(account, D("0")) + debit - credit
    return lines, entries, tb


def test_at2_backfill_sets_currency_fields_and_leaves_amounts_and_status_alone():
    leaf = _leaf_targets()
    old = _migrate(BEFORE)
    try:
        apply_currency_seed(old.get_model("core", "Currency"))
        Entity = old.get_model("core", "Entity")
        Account = old.get_model("ledger", "Account")
        Period = old.get_model("ledger", "FiscalPeriod")
        Entry = old.get_model("ledger", "JournalEntry")
        Line = old.get_model("ledger", "JournalLine")
        alm = Entity.objects.create(code="ALM", name_en="ALM", name_ar="كيان", consolidation="parent", base_currency_id="SAR")
        sub = Entity.objects.create(code="SUB", name_en="SUB", name_ar="كيان فرعي", parent=alm, base_currency_id="SAR")
        accounts = {}
        for e in (alm, sub):
            Account.objects.create(entity=e, code="1000", name_en="Assets", name_ar="الأصول", type="asset", is_postable=False)
            for code, typ in (("1100", "asset"), ("1210", "asset"), ("3100", "equity"), ("4110", "income")):
                accounts[(e.code, code)] = Account.objects.create(
                    entity=e, code=code, name_en=code, name_ar=code, type=typ, is_intercompany=code == "1210"
                )
            Period.objects.create(entity=e, name="2027-01", start_date=date(2027, 1, 1), end_date=date(2027, 1, 31))
        posted = Entry.objects.create(entity=alm, number="ALM-JE-2027-000001", date=date(2027, 1, 5), description="old",
                                      status="posted")
        Line.objects.create(entry=posted, account=accounts[("ALM", "1210")], debit=D("1150.00"), counterparty_entity=sub)
        Line.objects.create(entry=posted, account=accounts[("ALM", "4110")], credit=D("1150.00"))
        reversed_ = Entry.objects.create(entity=sub, number="SUB-JE-2027-000001", date=date(2027, 1, 6), description="old",
                                         status="reversed")
        Line.objects.create(entry=reversed_, account=accounts[("SUB", "1100")], debit=D("10.25"))
        Line.objects.create(entry=reversed_, account=accounts[("SUB", "3100")], credit=D("10.25"))
        rev = Entry.objects.create(entity=sub, number="SUB-JE-2027-000002", date=date(2027, 1, 7), description="rev",
                                   status="posted", reversal_of=reversed_)
        Line.objects.create(entry=rev, account=accounts[("SUB", "1100")], credit=D("10.25"))
        Line.objects.create(entry=rev, account=accounts[("SUB", "3100")], debit=D("10.25"))
        draft = Entry.objects.create(entity=alm, date=date(2027, 1, 8), description="draft")
        Line.objects.create(entry=draft, account=accounts[("ALM", "1100")], debit=D("3.00"))
        Line.objects.create(entry=draft, account=accounts[("ALM", "3100")], credit=D("3.00"))
        before = _snapshot(old)
    finally:
        new = _migrate(leaf)

    after = _snapshot(new)
    assert after == before  # amounts, accounts, counterparties, numbers, statuses, dates: identical
    Line = new.get_model("ledger", "JournalLine")
    assert Line.objects.count() == 8
    for line in Line.objects.all():
        assert (line.currency_id, line.fx_rate, line.rate_source, line.line_kind) == ("SAR", D("1"), "functional", "normal")
        assert line.txn_amount == line.debit + line.credit
        assert line.exchange_rate_id is None and line.settles_line_id is None
    Account = new.get_model("ledger", "Account")
    # 0005: the proposed FX accounts reach existing charts; the monetary flag is set on existing accounts
    assert set(Account.objects.filter(entity__code="ALM", code__in=["3400", "4700", "4710", "5750", "5760", "5770"])
               .values_list("code", flat=True)) == {"3400", "4700", "4710", "5750", "5760", "5770"}
    assert set(Account.objects.filter(entity__code="SUB", is_monetary=True).values_list("code", flat=True)) == {"1100", "1210"}
