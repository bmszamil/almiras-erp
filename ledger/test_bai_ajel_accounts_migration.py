"""invoicing R112 / payroll §5 (CFO approval P19(g), owner decision E8): ledger 0021 adds the approved bai' ajel
accounts 1241, 1250 and 4610 to existing charts; new charts get them from the template.

Migrates back to ledger 0020 (apps that depend on 0021 are unapplied too), builds charts with the historical models,
migrates forward and checks what 0021 added. Transactional because migrations cannot run inside the test
transaction; the database is flushed afterwards. Style of test_purification_account_migration.py.
"""
import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from core.currency_seed import apply_currency_seed
from ledger.coa_template import TEMPLATE
from ledger.models import Account as LiveAccount
from ledger.services_setup import install_chart_of_accounts

pytestmark = pytest.mark.django_db(transaction=True)
BEFORE = [("ledger", "0020_rate_source_sama_tax_point")]
EXPECTED = {
    "1241": ("asset", True, "1000", "Deferred bai' ajel profit (contra 1240)", "أرباح البيع الآجل المؤجلة"),
    "1250": ("asset", False, "1000", "Assets acquired for employee deferred sale", "أصول مشتراة للبيع الآجل للموظفين"),
    "4610": ("income", False, "4000", "Bai' ajel profit income", "إيرادات أرباح البيع الآجل"),
}


def _migrate(targets):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)
    return MigrationExecutor(connection).loader.project_state(targets).apps


def test_0021_adds_the_bai_ajel_accounts_to_existing_charts_only_and_keeps_an_existing_one():
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
            Account.objects.create(entity=e, code="4000", name_en="Income", name_ar="الإيرادات", type="income",
                                   is_postable=False)
        # SUB already has its own 1250: 0021 leaves it as it is.
        Account.objects.create(entity=sub, code="1250", name_en="Own stock", name_ar="مخزون خاص", type="asset",
                               is_postable=False)
        before = {(a.entity_id, a.code): a.pk for a in Account.objects.all()}
    finally:
        new = _migrate(leaf)

    Account = new.get_model("ledger", "Account")
    for code, (typ, monetary, header, en, ar) in EXPECTED.items():
        added = Account.objects.get(entity__code="ALM", code=code)
        assert (added.type, added.is_postable, added.is_monetary, added.name_en, added.name_ar) == (
            typ, True, monetary, en, ar)
        assert added.parent_id == Account.objects.get(entity__code="ALM", code=header).pk
        assert not added.sharia_prohibited and not added.requires_purification  # 4610: sale profit, never purified
    own = Account.objects.get(entity__code="SUB", code="1250")
    assert (own.pk, own.name_en, own.is_postable) == (before[(own.entity_id, "1250")], "Own stock", False)
    assert Account.objects.filter(entity__code="SUB", code__in=["1241", "4610"]).count() == 2
    assert not Account.objects.filter(entity__code="NEW").exists()  # no chart, nothing added
    for (entity_id, code), pk in before.items():
        assert Account.objects.filter(pk=pk, entity_id=entity_id, code=code).exists()


@pytest.mark.django_db
def test_new_charts_get_the_bai_ajel_accounts():
    from core.models import Currency, Entity

    apply_currency_seed(Currency)  # an earlier transactional test may have flushed the seeded currencies
    e = Entity.objects.create(code="NEW", name_en="NEW Co", name_ar="شركة جديدة")
    install_chart_of_accounts(e)
    for code, (typ, monetary, header, en, ar) in EXPECTED.items():
        account = LiveAccount.objects.get(entity=e, code=code)
        assert (account.type, account.is_postable, account.is_monetary, account.parent.code) == (
            typ, True, monetary, header)
        assert (account.name_en, account.name_ar) == (en, ar)
    assert LiveAccount.objects.get(entity=e, code="1240").is_monetary  # the receivable came with payroll 0002
    assert LiveAccount.objects.filter(entity=e).count() == len(TEMPLATE)
