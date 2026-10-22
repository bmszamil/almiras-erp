"""Invoicing R97 as amended by D33(e) (migration M6, R8, R22): AT-115, migration side.

The migration opening entry (``source="import"`` linked to an ``ImportedEntry`` of kind ``opening``) is the one manual
journal the ledger posts on the control accounts, and only once its open-item tie-out holds (R22, enforced by the
registered post check); replayed journals (kind ``journal``) and unlinked imports are refused by the ledger itself
(``control_account``, en/ar), whichever path posts them.
"""
from datetime import date
from decimal import Decimal

import pytest

from core import access
from ledger import services as ledger
from ledger.models import JournalEntry, LedgerError
from migration import services
from migration import testing as t
from migration.conftest import BALANCES, YEAR_END, map_rows, tb_file
from migration.models import ImportedEntry

D = Decimal


def _opening_with_1210(w):
    rows = map_rows() + [{"legacy_code": "1301", "legacy_name": "مستحق من شركة تابعة", "erp_code": "1210",
                          "counterparty": "SUB"}]
    t.approve_map(services.create_account_map(w.alm, user=w.a, rows=rows), approver=w.p)
    balances = {**BALANCES, "1301": ("مستحق من شركة تابعة", "20000.00", "0", "0"),
                "2501": ("رأس المال", "-80000.00", "0", "0")}
    plan = services.load_opening_balances(w.alm, tb_file(balances), user=w.a, cutover=YEAR_END, apply=True)
    assert plan.applied, [p.message for p in plan.report.errors]
    return plan.created[0].journal_entry


def test_at115_migration_opening_entry_with_a_1210_line_posts_after_its_tie_out(w, open_items):
    entry = _opening_with_1210(w)
    assert entry.lines.filter(account__code="1210", counterparty_entity=w.sub).exists()
    open_items["ALM"] = {("1200", ""): D("20000.00"), ("1210", "SUB"): D("19000.00"), ("2100", ""): D("-15000.00")}
    with pytest.raises(LedgerError) as exc:  # the tie-out (R22) still refuses it, through the ledger as well
        ledger.post_entry(entry, access.require(w.p, w.alm, "ledger.post_entry", document=entry))
    assert exc.value.code == "import_not_ready" and "1210 (SUB)" in " ".join(exc.value.messages)
    open_items["ALM"][("1210", "SUB")] = D("20000.00")
    posted = ledger.post_entry(entry, access.require(w.p, w.alm, "ledger.post_entry", document=entry))
    assert posted.status == "posted" and posted.lines.filter(account__code__in=["1200", "1210", "2100"]).count() == 3


def test_r97_reversal_of_the_posted_opening_entry_still_posts(w, open_items):
    entry = _opening_with_1210(w)
    open_items["ALM"] = {("1200", ""): D("20000.00"), ("1210", "SUB"): D("20000.00"), ("2100", ""): D("-15000.00")}
    entry = ledger.post_entry(entry, access.require(w.p, w.alm, "ledger.post_entry", document=entry))
    rev = ledger.reverse_entry(entry, access.require(w.a2, w.alm, "ledger.reverse_entry", document=entry))
    assert rev.status == "posted" and rev.lines.get(account__code="1210").credit == D("20000.00")


def test_at115_replayed_migration_journal_with_a_1200_line_is_refused_by_the_ledger(w, mapped):
    """Posted straight through ledger.post_entry (e.g. from the approval inbox), not only by the migration command."""
    amap = services._approved_map(w.alm, services.FALCON)
    entry = ledger.create_entry(w.alm, date(2026, 3, 1), "Falcon 26000009", [
        {"account": "1200", "debit": D("100.00")}, {"account": "4100", "credit": D("100.00")}],
        user=w.a, reference="FALCON-26000009", source="import")
    ImportedEntry.objects.create(entity=w.alm, source_system="falcon", kind="journal", legacy_key="26000009",
                                 journal_entry=entry, account_map=amap, lines_hash=services._entry_lines_hash(entry),
                                 created_by=w.a)
    with pytest.raises(LedgerError) as exc:
        ledger.post_entry(entry, access.require(w.p, w.alm, "ledger.post_entry", document=entry))
    assert exc.value.code == "control_account"
    text = " ".join(exc.value.messages)
    assert "Account 1200" in text and "never a manual journal" in text and "حساب مراقبة" in text
    assert JournalEntry.objects.get(pk=entry.pk).status == "draft"


def test_at115_import_journal_without_imported_entry_is_refused(w, mapped):
    entry = ledger.create_entry(w.alm, date(2026, 3, 1), "Unlinked import", [
        {"account": "1210", "debit": D("100.00"), "counterparty": w.sub}, {"account": "4100", "credit": D("100.00")}],
        user=w.a, reference="FALCON-X", source="import")
    with pytest.raises(LedgerError) as exc:
        ledger.post_entry(entry, access.require(w.p, w.alm, "ledger.post_entry", document=entry))
    assert exc.value.code == "control_account" and "Account 1210" in " ".join(exc.value.messages)


def test_r97_an_opening_record_of_another_entity_does_not_open_the_exception(w, mapped):
    """The opening exception needs the ImportedEntry of the entry's own entity (as written by the loader)."""
    entry = ledger.create_entry(w.alm, date(2026, 3, 1), "Import", [
        {"account": "1200", "debit": D("100.00")}, {"account": "4100", "credit": D("100.00")}],
        user=w.a, source="import")
    sub_map = t.approve_map(services.create_account_map(w.sub, user=w.s, rows=map_rows()), approver=w.ps)
    ImportedEntry.objects.create(entity=w.sub, source_system="falcon", kind="opening", legacy_key="OPENING",
                                 journal_entry=entry, account_map=sub_map,
                                 lines_hash=services._entry_lines_hash(entry), created_by=w.s)
    assert any("Account 1200" in p for p in ledger.validate_entry(entry))
