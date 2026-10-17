"""Restricted accounts and migration (invoicing R54 amendment D31b, review fix 1; migration.md R14 to R16).

GL 1140 (the purification account) takes a balance from a migration **opening** entry (an import draft, posted by a
second person), never from a replayed legacy journal. Synthetic Falcon files only.
"""
from datetime import date

import pytest

from ledger.models import JournalEntry, JournalLine
from migration import services
from migration import testing as t
from migration.conftest import BALANCES, CHART, YEAR_END, D, journal_file, map_rows, tb_file

pytestmark = pytest.mark.django_db
CHART_1140 = {**CHART, "1103": ("حساب التطهير", "1140")}


@pytest.fixture
def mapped_1140(w):
    amap = services.create_account_map(w.alm, user=w.a, rows=map_rows(CHART_1140))
    return services.approve_account_map(amap, user=w.p)


def test_opening_entry_may_carry_the_purification_account_balance(w, mapped_1140):
    balances = {**BALANCES, "1103": ("حساب التطهير", "500.00", "0", "0"),
                "2501": ("رأس المال", "-60500.00", "0", "0")}
    plan = services.load_opening_balances(w.alm, tb_file(balances), user=w.a, cutover=YEAR_END, apply=True)
    assert plan.applied, [p.message for p in plan.report.problems]
    entry = plan.created[0].journal_entry
    assert entry.lines.get(account__code="1140").debit == D("500.00")  # the import draft is accepted
    result = services.post_imported_entries(w.alm, user=w.p, kind="opening", apply=True)
    assert result["posted"][0].status == JournalEntry.Status.POSTED


def test_replayed_journal_on_the_purification_account_is_refused_at_posting(w, mapped_1140):
    journal = journal_file([t.line(26000020, date(2026, 3, 5), "1103", "حساب التطهير", debit="200.00"),
                            t.line(26000020, date(2026, 3, 5), "1102", "بنك تجريبي", credit="200.00")])
    services.load_journal_entries(w.alm, journal, user=w.a, apply=True)  # an import draft may be created
    result = services.post_imported_entries(w.alm, user=w.p, kind="journal")
    assert result["problems"] == ["26000020"]
    text = " ".join(result["details"]["26000020"])
    assert "1140" in text and "never by a manual journal" in text and "يُرحَّل" in text
    result = services.post_imported_entries(w.alm, user=w.p, kind="journal", apply=True)
    assert not result["posted"]
    assert not JournalLine.objects.filter(account__code="1140", entry__status="posted").exists()
