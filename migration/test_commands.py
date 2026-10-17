"""migration.md R16 (commands: dry run unless --apply, a named human user; AT-15). Synthetic files in tmp_path."""
import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from ledger.models import JournalEntry
from migration import testing as t
from migration.conftest import BALANCES, journal_rows
from migration.models import AccountMap, ImportedEntry


def cmd(name, *args):
    out = StringIO()
    call_command(name, *args, stdout=out)
    return out.getvalue()


@pytest.fixture
def files(tmp_path):
    from migration.conftest import CHART
    paths = {"map": tmp_path / "alm-account-map.csv", "journal": tmp_path / "journal.xlsx", "tb": tmp_path / "tb.csv",
             "cols": tmp_path / "tb-columns.json", "report": tmp_path / "report.csv"}
    paths["map"].write_bytes(t.table_csv(["legacy_code", "legacy_name", "erp_code", "counterparty", "note"],
                                         [[c, n, erp, "", ""] for c, (n, erp) in CHART.items()]))
    paths["journal"].write_bytes(t.falcon_xlsx(journal_rows()))
    paths["tb"].write_bytes(t.table_csv(t.TB_HEADER, t.tb_rows(BALANCES)))
    paths["cols"].write_text(json.dumps({"columns": t.TB_COLUMNS}, ensure_ascii=False), encoding="utf-8")
    return {k: str(v) for k, v in paths.items()}


def test_at15_full_command_flow_dry_run_by_default(w, files):
    base = ["--entity", "ALM"]
    assert "dry run" in cmd("migration_map", *base, "--user", "mig.a", "--import", files["map"])
    assert not AccountMap.objects.exists()
    cmd("migration_map", *base, "--user", "mig.a", "--import", files["map"], "--apply")
    with pytest.raises(CommandError):  # the preparer cannot approve
        cmd("migration_map", *base, "--user", "mig.a", "--approve", "1", "--apply")
    assert "approved v1" in cmd("migration_map", *base, "--user", "mig.p", "--approve", "1", "--apply")

    tb = ["--tb", files["tb"], "--tb-columns", files["cols"]]
    out = cmd("migration_validate", *base, "--user", "mig.a", "--journal", files["journal"], *tb, "--cutover",
              "2025-12-31", "--basis", "opening", "--report", files["report"])
    assert "errors / أخطاء: 0" in out
    with open(files["report"], encoding="utf-8-sig") as fh:
        report = fh.read()
    assert "tie_out" in report and "مستخلص" in report  # Arabic survives in the CSV

    load = [*base, "--user", "mig.a", "--cutover", "2025-12-31", *tb, "--journal", files["journal"]]
    assert "would create (dry run)" in cmd("migration_load", *load)
    assert not JournalEntry.objects.exists()
    cmd("migration_load", *load, "--apply")
    assert ImportedEntry.objects.count() == 5 and JournalEntry.objects.filter(status="draft").count() == 5
    cmd("migration_load", *load, "--apply")  # re-run: nothing duplicated
    assert ImportedEntry.objects.count() == 5

    assert "dry run" in cmd("migration_post", *base, "--user", "mig.p", "--kind", "opening")
    with pytest.raises(CommandError):
        cmd("migration_post", *base, "--user", "mig.a", "--kind", "opening", "--apply")
    cmd("migration_post", *base, "--user", "mig.p", "--kind", "opening", "--apply")
    cmd("migration_post", *base, "--user", "mig.p", "--kind", "journal", "--apply")
    assert JournalEntry.objects.filter(status="posted").count() == 5
    assert "ties out" in cmd("migration_tieout", *base, "--user", "mig.p", *tb, "--as-of", "2026-12-31", "--posted-only")


def test_validate_fails_on_errors_and_unknown_user(w, files):
    with pytest.raises(CommandError):  # no approved map
        cmd("migration_validate", "--entity", "ALM", "--user", "mig.a", "--journal", files["journal"])
    with pytest.raises(CommandError):
        cmd("migration_validate", "--entity", "ALM", "--user", "nobody", "--journal", files["journal"])
    with pytest.raises(CommandError):
        cmd("migration_load", "--entity", "ALM", "--user", "mig.a", "--cutover", "2025-12-31", "--journal",
            files["journal"], "--apply")
    assert not JournalEntry.objects.exists()


def test_tie_out_command_reports_differences(w, mapped, files):
    with pytest.raises(CommandError) as exc:
        cmd("migration_tieout", "--entity", "ALM", "--user", "mig.p", "--tb", files["tb"], "--tb-columns", files["cols"],
            "--as-of", "2026-12-31")
    assert "difference" in str(exc.value)


def test_discard_command(w, mapped, files):
    load = ["--entity", "ALM", "--user", "mig.a", "--cutover", "2025-12-31", "--journal", files["journal"]]
    cmd("migration_load", *load, "--apply")
    assert "would discard" in cmd("migration_discard", "--entity", "ALM", "--user", "mig.a", "--kind", "journal")
    cmd("migration_discard", "--entity", "ALM", "--user", "mig.a", "--kind", "journal", "--apply")
    assert not ImportedEntry.objects.exists() and not JournalEntry.objects.exists()
