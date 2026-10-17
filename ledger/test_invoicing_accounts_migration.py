"""Review item 9: migration ledger 0013 adds the proposed invoicing income accounts to existing charts.

Migrates back to ledger 0012 (before 0013; invoicing, which depends on it, is unapplied too), builds charts
with the historical models, migrates forward and checks what 0013 added. Transactional because migrations
cannot run inside the test transaction; the database is flushed afterwards. Style of test_fx_migration.py.
"""
import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from core.currency_seed import apply_currency_seed

pytestmark = pytest.mark.django_db(transaction=True)
BEFORE = [("ledger", "0012_entry_link_triggers")]
ADDED = {"4120", "4220", "4230", "4240", "4310"}


def _migrate(targets):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)
    return MigrationExecutor(connection).loader.project_state(targets).apps


def test_0013_adds_the_income_accounts_to_existing_charts_only_and_keeps_existing_ones():
    leaf = MigrationExecutor(connection).loader.graph.leaf_nodes()
    old = _migrate(BEFORE)
    try:
        apply_currency_seed(old.get_model("core", "Currency"))
        Entity = old.get_model("core", "Entity")
        Account = old.get_model("ledger", "Account")
        alm = Entity.objects.create(code="ALM", name_en="ALM", name_ar="كيان", consolidation="parent",
                                    base_currency_id="SAR")
        sub = Entity.objects.create(code="SUB", name_en="SUB", name_ar="كيان فرعي", parent=alm, base_currency_id="SAR")
        Entity.objects.create(code="NEW", name_en="NEW", name_ar="كيان جديد", parent=alm, base_currency_id="SAR")  # no chart
        for e in (alm, sub):
            Account.objects.create(entity=e, code="1000", name_en="Assets", name_ar="الأصول", type="asset",
                                   is_postable=False)
            Account.objects.create(entity=e, code="4000", name_en="Income", name_ar="الإيرادات", type="income",
                                   is_postable=False)
        # ALM already has its own 4220: 0013 leaves it as it is.
        Account.objects.create(entity=alm, code="4220", name_en="Own fee income", name_ar="إيراد خاص", type="income",
                               is_postable=False)
        before = {(a.entity_id, a.code): a.pk for a in Account.objects.all()}
    finally:
        new = _migrate(leaf)

    Account = new.get_model("ledger", "Account")
    for entity_code in ("ALM", "SUB"):
        added = Account.objects.filter(entity__code=entity_code, code__in=ADDED)
        assert set(added.values_list("code", flat=True)) == ADDED
        header = Account.objects.get(entity__code=entity_code, code="4000")
        for account in added:
            if (account.entity_id, account.code) in before:
                continue
            assert (account.type, account.parent_id, account.is_postable) == ("income", header.pk, True)
            assert account.name_en and account.name_ar
    own = Account.objects.get(entity__code="ALM", code="4220")
    assert (own.pk, own.name_en, own.is_postable) == (before[(own.entity_id, "4220")], "Own fee income", False)
    assert Account.objects.get(entity__code="SUB", code="4120").name_ar == "إيرادات الإيجار السكني (معفاة)"
    assert not Account.objects.filter(entity__code="NEW").exists()  # no chart, nothing added
    for (entity_id, code), pk in before.items():  # nothing that existed was replaced
        assert Account.objects.filter(pk=pk, entity_id=entity_id, code=code).exists()
