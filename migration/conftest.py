"""Fixtures for migration tests (migration.md §8). Anonymised entities and users, synthetic Falcon files only."""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from core.access.testing import make_user, member
from core.models import Entity
from ledger.services_setup import create_monthly_periods, install_chart_of_accounts
from migration import readers, services
from migration import testing as t

D = Decimal
CUTOVER = date(2025, 12, 31)  # opening TB at the start of FY2026, the 2026 journals replayed after it
YEAR_END = date(2026, 12, 31)

# Falcon code -> (name, ERP code); fake names.
CHART = {
    "1101": ("الصندوق الرئيسي", "1100"),
    "1102": ("بنك تجريبي", "1100"),
    "1201": ("عملاء تجريبيون", "1200"),
    "2101": ("موردون تجريبيون", "2100"),
    "2201": ("ضريبة القيمة المضافة المستحقة", "2200"),
    "2501": ("رأس المال", "3100"),
    "4101": ("إيرادات إيجار", "4100"),
    "5101": ("رواتب وأجور", "5100"),
    "5201": ("مصروف إيجار", "5200"),
}
# code -> (name, opening signed, movement debit, movement credit): ties out with JOURNAL below. The replayed journal
# never touches a control account (1201 -> 1200, 2101 -> 2100; R8 as amended by M7): they carry opening balances only.
BALANCES = {
    "1101": ("الصندوق الرئيسي", "10000.00", "500.00", "3000.00"),
    "1102": ("بنك تجريبي", "50000.00", "11500.00", "8500.00"),
    "1201": ("عملاء تجريبيون", "20000.00", "0", "0"),
    "2101": ("موردون تجريبيون", "-15000.00", "0", "0"),
    "2201": ("ضريبة القيمة المضافة المستحقة", "-5000.00", "0", "1500.00"),
    "2501": ("رأس المال", "-60000.00", "0", "0"),
    "4101": ("إيرادات إيجار", "0", "0", "10000.00"),
    "5101": ("رواتب وأجور", "0", "8000.00", "0"),
    "5201": ("مصروف إيجار", "0", "3000.00", "0"),
}


def journal_rows():
    L = t.line
    return [
        L(26000001, date(2026, 1, 15), "1102", "بنك تجريبي", debit="11500.00", description="مستخلص رقم 1 محصل",
          source="مستخلص عميل", source_no=501, serial=1),
        L(26000001, date(2026, 1, 15), "4101", "إيرادات إيجار", credit="10000.00", description="مستخلص رقم 1",
          source="مستخلص عميل", source_no=501, serial=2),
        L(26000001, date(2026, 1, 15), "2201", "ضريبة القيمة المضافة المستحقة", credit="1500.00",
          description="ضريبة مستخلص 1", source="مستخلص عميل", source_no=501, serial=3),
        L(26000002, date(2026, 1, 20), "1101", "الصندوق الرئيسي", debit="500.00", description="سحب نقدي", serial=1),
        L(26000002, date(2026, 1, 20), "1102", "بنك تجريبي", credit="500.00", description="سحب نقدي", serial=2),
        L(26000003, date(2026, 2, 1), "5101", "رواتب وأجور", debit="8000.00", description="رواتب يناير",
          cost_centre="الإدارة", serial=1),
        L(26000003, date(2026, 2, 1), "1102", "بنك تجريبي", credit="8000.00", description="رواتب يناير", serial=2),
        L(26000004, date(2026, 2, 10), "5201", "مصروف إيجار", debit="3000.00", description="إيجار مكتب", serial=1),
        L(26000004, date(2026, 2, 10), "1101", "الصندوق الرئيسي", credit="3000.00", description="إيجار مكتب", serial=2),
    ]


def journal_file(rows=None, **kw):
    return readers.read_falcon_journal(t.falcon_xlsx(journal_rows() if rows is None else rows, **kw))


def tb_file(balances=None, extra_rows=(), fmt="csv"):
    rows = t.tb_rows(BALANCES if balances is None else balances) + list(extra_rows)
    data = t.table_csv(t.TB_HEADER, rows) if fmt == "csv" else t.table_xlsx(t.TB_HEADER, rows)
    return readers.read_trial_balance(data, {"columns": t.TB_COLUMNS})


def make_entity(code, consolidation="full", parent=None, **extra):
    e = Entity.objects.create(code=code, name_en=f"{code} Co", name_ar=f"شركة {code}", consolidation=consolidation,
                              parent=parent, **extra)
    install_chart_of_accounts(e)
    for year in (2025, 2026, 2027):
        create_monthly_periods(e, year)
    return e


@pytest.fixture
def w(db):
    alm = make_entity("ALM", "parent")
    sub = make_entity("SUB", "full", alm)
    ns = SimpleNamespace(alm=alm, sub=sub)
    for name, entity, role in (("a", alm, "accountant"), ("a2", alm, "accountant"), ("p", alm, "approver"),
                               ("v", alm, "viewer"), ("s", sub, "accountant"), ("ps", sub, "approver")):
        user = make_user(f"mig.{name}")
        member(user, entity, role)
        setattr(ns, name, user)
    return ns


def map_rows(chart=None):
    return [{"legacy_code": c, "legacy_name": n, "erp_code": erp} for c, (n, erp) in (chart or CHART).items()]


# The TB's control-account balances (the same at the cut-over and the year end: no replayed movement on them).
OPEN_ITEMS = {("1200", ""): D("20000.00"), ("2100", ""): D("-15000.00")}


@pytest.fixture
def open_items():
    """Stands in for the sub-ledger loads (R23; ``migration.subledgers`` is tested in ``test_subledgers.py``):
    ``open_items["ALM"] = {(account code, counterparty code or ""): signed SAR}`` is added to what the R22 posting
    gate sees as loaded for that entity, on the control accounts, 2500 and 1240 / 1241."""
    loaded = {}

    def source(entity):
        return loaded.get(entity.code, {})

    services.register_open_items_source(source, codes=services.CONTROL_CODES | {"2500", "1240", "1241"})
    yield loaded
    services.unregister_open_items_source(source)


@pytest.fixture
def loaded(open_items):
    """ALM's open items loaded and equal to its control accounts (R22 passes)."""
    open_items["ALM"] = dict(OPEN_ITEMS)
    return open_items


@pytest.fixture
def mapped(w):
    """ALM's Falcon map drafted by A and approved by P."""
    amap = services.create_account_map(w.alm, user=w.a, rows=map_rows())
    return t.approve_map(amap, approver=w.p)
