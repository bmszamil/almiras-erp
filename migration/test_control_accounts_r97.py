"""Invoicing R97 as amended by D33(e) (migration M6, R8, R22): AT-115, migration side.

The migration opening entry (``source="import"`` linked to an ``ImportedEntry`` of kind ``opening``) is the one manual
journal the ledger posts on the control accounts, and only once its open-item tie-out holds (R22, enforced by the
registered post check); replayed journals (kind ``journal``) and unlinked imports are refused by the ledger itself
(``control_account``, en/ar), whichever path posts them.
"""
from datetime import date
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

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


def test_review_reversing_the_reversal_of_the_opening_entry_is_refused(w, open_items):
    """Review of 3b4cb6e, item 1 (reviewer probe): O -> R1 -> R2 would book the opening entry's control lines again
    without the R22 tie-out. To restore an opening entry, the migration load is run again."""
    entry = _opening_with_1210(w)
    open_items["ALM"] = {("1200", ""): D("20000.00"), ("1210", "SUB"): D("20000.00"), ("2100", ""): D("-15000.00")}
    entry = ledger.post_entry(entry, access.require(w.p, w.alm, "ledger.post_entry", document=entry))
    rev = ledger.reverse_entry(entry, access.require(w.a2, w.alm, "ledger.reverse_entry", document=entry))
    open_items["ALM"] = {}  # the sub-ledger is empty now: a tie-out would refuse
    with pytest.raises(LedgerError) as exc:
        ledger.reverse_entry(rev, access.require(w.a2, w.alm, "ledger.reverse_entry", document=rev))
    assert exc.value.code == "control_account"
    text = " ".join(exc.value.messages)
    assert "run the migration load again" in text and "أعد تشغيل تحميل الترحيل" in text
    assert "Account 1210" in text and "Account 1200" in text and "Account 2100" in text
    assert JournalEntry.objects.get(pk=rev.pk).status == "posted"
    assert not JournalEntry.objects.filter(reversal_of=rev).exists()  # the refused reversal rolled back


# --- re-review N1 (team default 2026-10-23, migration R14): a reversed opening entry is superseded by a reload ----

def _post_opening(w, entry):
    return ledger.post_entry(entry, access.require(w.p, w.alm, "ledger.post_entry", document=entry))


def _reload(w, sub_balance="20000.00"):
    balances = {**BALANCES, "1301": ("مستحق من شركة تابعة", sub_balance, "0", "0"),
                "2501": ("رأس المال", str(D("-60000.00") - D(sub_balance)), "0", "0")}
    return services.load_opening_balances(w.alm, tb_file(balances), user=w.a, cutover=YEAR_END, apply=True)


@pytest.mark.parametrize("sub_balance", ["20000.00", "25000.00"])  # the same balances, and other balances
def test_n1_reversed_opening_entry_is_superseded_by_a_reload_that_posts_after_its_tie_out(w, open_items, sub_balance):
    old = _opening_with_1210(w)
    open_items["ALM"] = {("1200", ""): D("20000.00"), ("1210", "SUB"): D("20000.00"), ("2100", ""): D("-15000.00")}
    old = _post_opening(w, old)
    assert ledger.migration_opening_posted(w.alm)
    ledger.reverse_entry(old, access.require(w.a2, w.alm, "ledger.reverse_entry", document=old))
    assert ledger.migration_opening_posted(w.alm)  # reversed, not yet superseded: the sub-ledger loads stay closed

    plan = _reload(w, sub_balance)
    assert plan.applied, [p.message for p in plan.report.errors]
    new_imp = plan.created[0]
    assert new_imp.legacy_key == "opening-2" and new_imp.journal_entry.reference == "FALCON-OPENING-2"
    kept = ImportedEntry.objects.get(journal_entry=old)  # keep everything: the old import stays, superseded
    assert kept.legacy_key == "opening" and kept.superseded_at is not None
    assert not ledger.migration_opening_posted(w.alm)  # R25: loads are open again until the new entry posts

    open_items["ALM"][("1210", "SUB")] = D("19000.00")
    result = services.post_imported_entries(w.alm, user=w.p, kind="opening", apply=True)
    assert result["problems"] == ["opening-2"] and result["already_posted"] == ["opening"]  # R22 again
    open_items["ALM"][("1210", "SUB")] = D(sub_balance)
    result = services.post_imported_entries(w.alm, user=w.p, kind="opening", apply=True)
    posted = result["posted"][0]
    assert posted.pk == new_imp.journal_entry_id and posted.lines.get(account__code="1210").debit == D(sub_balance)
    assert ledger.migration_opening_posted(w.alm, booked=False)
    assert ImportedEntry.objects.filter(entity=w.alm, kind="opening").count() == 2


def test_n1_a_second_reload_while_the_new_opening_entry_is_live_is_refused(w, open_items):
    old = _opening_with_1210(w)
    open_items["ALM"] = {("1200", ""): D("20000.00"), ("1210", "SUB"): D("20000.00"), ("2100", ""): D("-15000.00")}
    old = _post_opening(w, old)
    ledger.reverse_entry(old, access.require(w.a2, w.alm, "ledger.reverse_entry", document=old))
    assert _reload(w).applied
    again = _reload(w)  # the same balances: skipped, as for any live opening import (R14)
    assert again.skipped == ["opening-2"] and not again.created
    other = _reload(w, "21000.00")
    assert not other.applied and [p.code for p in other.report.errors] == ["already_imported"]
    with pytest.raises(IntegrityError), transaction.atomic():  # the database keeps one live opening import
        imp = ImportedEntry.objects.get(legacy_key="opening-2")
        ImportedEntry.objects.create(entity=w.alm, source_system="falcon", kind="opening", legacy_key="opening-3",
                                     journal_entry=JournalEntry.objects.create(entity=w.alm, date=YEAR_END,
                                                                               description="x"),
                                     account_map=imp.account_map, file_sha256="0" * 64, lines_hash="0" * 64,
                                     created_by=w.a)
    assert ImportedEntry.objects.filter(entity=w.alm, kind="opening", superseded_at__isnull=True).count() == 1


def test_n1_a_posted_opening_entry_that_is_not_reversed_is_never_reloaded(w, open_items):
    old = _opening_with_1210(w)
    open_items["ALM"] = {("1200", ""): D("20000.00"), ("1210", "SUB"): D("20000.00"), ("2100", ""): D("-15000.00")}
    _post_opening(w, old)
    plan = _reload(w, "25000.00")
    assert not plan.applied and [p.code for p in plan.report.errors] == ["already_imported"]
    assert not ImportedEntry.objects.filter(superseded_at__isnull=False).exists()
