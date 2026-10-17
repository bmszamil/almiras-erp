"""D31b (invoicing R54 amendment): ledger 0017 adds the proposed purification bank account 1140 to existing charts.

Migrates back to ledger 0016 (apps that depend on 0017 are unapplied too), builds charts with the historical models,
migrates forward and checks what 0017 added. Transactional because migrations cannot run inside the test
transaction; the database is flushed afterwards. Style of test_invoicing_accounts_migration.py.
"""
import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from core.currency_seed import apply_currency_seed
from ledger.coa_template import TEMPLATE
from ledger.models import Account as LiveAccount
from ledger.services_setup import install_chart_of_accounts

pytestmark = pytest.mark.django_db(transaction=True)
BEFORE = [("ledger", "0016_wht_accounts")]


def _migrate(targets):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)
    return MigrationExecutor(connection).loader.project_state(targets).apps


def test_0017_adds_1140_under_1100_to_existing_charts_only_and_keeps_an_existing_one():
    leaf = MigrationExecutor(connection).loader.graph.leaf_nodes()
    old = _migrate(BEFORE)
    try:
        apply_currency_seed(old.get_model("core", "Currency"))
        Entity = old.get_model("core", "Entity")
        Account = old.get_model("ledger", "Account")
        alm = Entity.objects.create(code="ALM", name_en="ALM", name_ar="كيان", consolidation="parent",
                                    base_currency_id="SAR")
        sub = Entity.objects.create(code="SUB", name_en="SUB", name_ar="كيان فرعي", parent=alm, base_currency_id="SAR")
        Entity.objects.create(code="NEW", name_en="NEW", name_ar="كيان جديد", parent=alm, base_currency_id="SAR")
        for e in (alm, sub):
            Account.objects.create(entity=e, code="1000", name_en="Assets", name_ar="الأصول", type="asset",
                                   is_postable=False)
        Account.objects.create(entity=alm, code="1100", name_en="Cash and bank", name_ar="النقد", type="asset",
                               parent=Account.objects.get(entity=alm, code="1000"), is_postable=True, is_monetary=True)
        # SUB already has its own 1140: 0017 leaves it as it is.
        Account.objects.create(entity=sub, code="1140", name_en="Own deposit", name_ar="وديعة خاصة", type="asset",
                               is_postable=False)
        before = {(a.entity_id, a.code): a.pk for a in Account.objects.all()}
    finally:
        new = _migrate(leaf)

    Account = new.get_model("ledger", "Account")
    added = Account.objects.get(entity__code="ALM", code="1140")
    assert (added.type, added.is_postable, added.is_monetary, added.currency_id) == ("asset", True, True, None)
    assert added.parent_id == Account.objects.get(entity__code="ALM", code="1100").pk  # banking R5: under 1100
    assert (added.name_en, added.name_ar) == ("Bank – purification account", "البنك – حساب التطهير")
    own = Account.objects.get(entity__code="SUB", code="1140")
    assert (own.pk, own.name_en, own.is_postable) == (before[(own.entity_id, "1140")], "Own deposit", False)
    assert not Account.objects.filter(entity__code="NEW").exists()  # no chart, nothing added
    for (entity_id, code), pk in before.items():
        assert Account.objects.filter(pk=pk, entity_id=entity_id, code=code).exists()


@pytest.mark.django_db
def test_new_charts_get_1140_as_a_sub_account_of_1100():
    from core.models import Currency, Entity

    apply_currency_seed(Currency)  # an earlier transactional test may have flushed the seeded currencies
    e = Entity.objects.create(code="NEW", name_en="NEW Co", name_ar="شركة جديدة")
    install_chart_of_accounts(e)
    account = LiveAccount.objects.get(entity=e, code="1140")
    assert account.parent.code == "1100" and account.is_postable and account.is_monetary
    assert [row for row in TEMPLATE if row[0] == "1140"][0][4]["parent"] == "1100"
    assert LiveAccount.objects.filter(entity=e).count() == len(TEMPLATE)
