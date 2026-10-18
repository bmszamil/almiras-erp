"""migration.md §11 W12, W13 (commands with the workbook; AT-21, AT-22). Synthetic workbooks in tmp_path."""
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from ledger.models import JournalEntry
from migration import testing as t
from migration import workbook
from migration.models import AccountMap, ImportedEntry


def cmd(name, *args):
    out = StringIO()
    call_command(name, *args, stdout=out)
    return out.getvalue()


@pytest.fixture
def wb_file(tmp_path):
    path = tmp_path / "workbook.xlsx"
    path.write_bytes(t.filled_workbook())
    return str(path)


def test_at21_map_load_post_and_tie_out_from_the_workbook(w, wb_file):
    base = ["--entity", "ALM", "--workbook", wb_file]
    assert "no errors" in cmd("migration_workbook_check", "--workbook", wb_file, "--user", "mig.a", "--entity", "ALM")

    assert "dry run" in cmd("migration_map", *base, "--user", "mig.a")
    assert not AccountMap.objects.exists()
    assert "draft v1: 13 rows" in cmd("migration_map", *base, "--user", "mig.a", "--apply")
    row = AccountMap.objects.get().rows.get(legacy_code="1202")
    assert (row.account.code, row.counterparty.code, row.legacy_name) == ("1210", "SUB", "مستحق من الشركة التابعة")
    cmd("migration_map", "--entity", "ALM", "--user", "mig.p", "--approve", "1", "--apply")

    out = cmd("migration_validate", *base, "--user", "mig.a")
    assert "errors / أخطاء: 0" in out
    with pytest.raises(CommandError):  # the workbook's date is the cut-over date
        cmd("migration_load", *base, "--user", "mig.a", "--cutover", "2026-11-30")
    assert "would create (dry run)" in cmd("migration_load", *base, "--user", "mig.a")
    assert not JournalEntry.objects.exists()
    cmd("migration_load", *base, "--user", "mig.a", "--apply")
    entry = ImportedEntry.objects.get(kind="opening").journal_entry
    assert entry.status == "draft" and entry.source == "import" and str(entry.date) == "2026-12-31"
    assert entry.lines.count() == 12  # one per Falcon code with a balance; the TB balances, 3900 not needed
    cmd("migration_load", *base, "--user", "mig.a", "--apply")  # re-run: skipped, not duplicated
    assert ImportedEntry.objects.count() == 1

    cmd("migration_post", "--entity", "ALM", "--user", "mig.p", "--kind", "opening", "--apply")
    assert "ties out" in cmd("migration_tieout", *base, "--user", "mig.p", "--as-of", "2026-12-31", "--posted-only")


def test_at22_workbook_check_fails_on_errors_and_writes_a_report_without_personal_data(w, tmp_path):
    rows = t.workbook_rows()
    rows["trial_balance"][0]["debit"] = rows["trial_balance"][0]["debit"] + 1
    path, report = tmp_path / "bad.xlsx", tmp_path / "report.csv"
    path.write_bytes(t.filled_workbook(rows))
    with pytest.raises(CommandError) as exc:
        cmd("migration_workbook_check", "--workbook", str(path), "--report", str(report))
    assert "error" in str(exc.value) and "خطأ" in str(exc.value)
    text = report.read_text(encoding="utf-8-sig")
    assert "tb_unbalanced" in text and "tie_out" in text and "ميزان المراجعة" in text  # Arabic survives
    for employee in rows["employees"]:
        assert employee["full_name_ar"] not in text and employee["id_number"] not in text


def test_workbook_check_with_a_user_needs_the_right_in_each_entity(w, wb_file):
    with pytest.raises(CommandError) as exc:  # S is an accountant in SUB only
        cmd("migration_workbook_check", "--workbook", wb_file, "--user", "mig.s")
    assert "ALM" in str(exc.value) or "permission" in str(exc.value).lower()
    out = cmd("migration_workbook_check", "--workbook", wb_file, "--user", "mig.s", "--entity", "SUB")
    assert "SUB (ERP chart: erp)" in out and "no errors" in out


def test_entities_not_in_the_erp_are_checked_against_the_template(db, wb_file):
    from core.access.testing import make_user
    make_user("mig.x")
    out = cmd("migration_workbook_check", "--workbook", wb_file, "--user", "mig.x")
    assert "not set up in the ERP yet" in out and "ALM (ERP chart: template)" in out


def test_template_command_writes_a_readable_template(tmp_path):
    path = tmp_path / "out" / "template.xlsx"
    assert "template written" in cmd("migration_template", "--output", str(path))
    book = workbook.read_workbook(path)
    assert {p.code for p in book.problems} == {"example_row"}


def test_commands_need_a_source(w, wb_file):
    with pytest.raises(CommandError):
        cmd("migration_tieout", "--entity", "ALM", "--user", "mig.p", "--as-of", "2026-12-31")
    with pytest.raises(CommandError):
        cmd("migration_load", "--entity", "ALM", "--user", "mig.a", "--tb", wb_file, "--workbook", wb_file)
    with pytest.raises(CommandError):
        cmd("migration_load", "--entity", "ALM", "--user", "mig.a", "--workbook", wb_file, "--basis", "opening")
