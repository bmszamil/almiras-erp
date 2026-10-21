"""The opening-balance dry run (docs/migration/dry-run-2026-10-21.md) on the FICTIONAL fixture set
``migration/fixtures/dry_run.py``: workbook check, mapping and approval, validation, R11 journal-to-TB tie-out,
loading, the R22 open-item posting gate, posting through ``ledger.services.post_entry`` by a second person, and the
post-load tie-out to the halala; with the restricted-account, Sharia, entity-separation and rounding cases.

Everything runs through the management commands and ``migration.services`` exactly as the runbook (§10) does, on
files written to ``tmp_path`` (never git, R20).
"""
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from core.access import AccessDenied
from core.access.testing import make_user, member
from ledger import services as ledger
from ledger.models import Account, JournalEntry, JournalLine, LedgerError
from migration import readers, services, workbook, workbook_checks
from migration import testing as t
from migration.conftest import make_entity
from migration.fixtures import dry_run as f
from migration.models import ImportedEntry, MigrationError

pytestmark = pytest.mark.django_db
D = Decimal
H, S = f.HOLDING, f.SUBSIDIARY


def cmd(name, *args):
    out = StringIO()
    call_command(name, *args, stdout=out)
    return out.getvalue()


@pytest.fixture
def dry(db, tmp_path):
    holding = make_entity(H, "parent")
    subsidiary = make_entity(S, "full", holding, sharia_enforced=False)
    users = {}
    for name, roles in (("acc", [(holding, "accountant"), (subsidiary, "accountant")]),  # R24: one grant per entity
                        ("apr", [(holding, "approver"), (subsidiary, "approver")]),
                        ("subacc", [(subsidiary, "accountant")])):
        users[name] = make_user(f"dry.{name}")
        for entity, role in roles:
            member(users[name], entity, role)
    files = f.write_files(tmp_path / "exports")
    return type("Dry", (), {"h": holding, "s": subsidiary, **users, "files": files,
                            "book": workbook.read_workbook(files["workbook"])})


@pytest.fixture
def r23_loader(dry):
    """Stands in for the invoicing open-item loader (R23, not built): the open items of the workbook, read back from
    the xlsx, as they would be loaded per control account and counterparty. ``adjust`` simulates a wrong load."""
    adjust = {}

    def source(entity):
        items = f.open_items_by_control(dry.book, entity.code)
        for key, delta in adjust.get(entity.code, {}).items():
            items[key] = items.get(key, D("0")) + delta
        return items

    services.register_open_items_source(source)
    yield adjust
    services.unregister_open_items_source(source)


def erp_balances(entity, user, as_of=f.CUTOVER):
    return {r["code"]: r["debit"] - r["credit"] for r in ledger.trial_balance(entity, as_of, user=user)
            if r["debit"] - r["credit"]}


def test_dry_run_end_to_end_ties_out_to_the_halala(dry, r23_loader, monkeypatch, tmp_path):
    wb = ["--workbook", dry.files["workbook"]]

    # 1. Profile: the files keep their Arabic text; the workbook reads with no problem.
    assert not dry.book.problems
    tb_csv = readers.read_trial_balance(dry.files["tb"], dry.files["tb_columns"])
    assert tb_csv.rows[0].name == "بنك وهمي جاري" and len(tb_csv.rows) == 20

    # 2. Workbook check (W4 to W11): no error; every sub-ledger tie-out zero, per entity.
    assert "no errors" in cmd("migration_workbook_check", *wb, "--user", "dry.acc")
    charts = {e.code: workbook_checks.entity_chart(e) for e in (dry.h, dry.s)}
    report = workbook_checks.check_workbook(dry.book, charts=charts)
    assert not report.errors, [p.message for p in report.errors]
    assert {p.code for p in report.warnings} == {"bai_ajel_contracts"}  # the CFO decision on the contracts
    ties = {(r["entity"], r["check"], r["account"]): (r["subledger"], r["ledger"], r["difference"]) for r in report.ties}
    assert ties == {
        (H, "open_receivables", "1200"): (D("44250.50"), D("44250.50"), 0),
        (H, "open_receivables", "1210/DRYS"): (D("12500.00"), D("12500.00"), 0),
        (H, "open_payables", "2100"): (D("27000.00"), D("27000.00"), 0),
        (H, "bank_accounts", "1101"): (D("182345.67"), D("182345.67"), 0),
        (H, "bank_accounts", "1102"): (D("18750.00"), D("18750.00"), 0),
        (H, "bank_accounts", "1103"): (D("1234.56"), D("1234.56"), 0),
        (H, "employees", "2500"): (D("58000.00"), D("58000.00"), 0),
        (H, "fixed_assets_cost", "1501"): (D("240000.00"), D("240000.00"), 0),
        (H, "fixed_assets_cost", "1502"): (D("36000.00"), D("36000.00"), 0),
        (H, "fixed_assets_accumulated", "1591"): (D("101500.00"), D("101500.00"), 0),
        (S, "open_receivables", "1200"): (D("9200.10"), D("9200.10"), 0),
        (S, "open_payables", "2110/DRYH"): (D("12500.00"), D("12500.00"), 0),
        (S, "bank_accounts", "1101"): (D("61000.00"), D("61000.00"), 0),
    }
    assert report.summary[H]["tb_debit"] == report.summary[H]["tb_credit"] == f.EXPECTED_TOTAL_DEBIT[H]
    assert report.summary[S]["tb_debit"] == report.summary[S]["tb_credit"] == f.EXPECTED_TOTAL_DEBIT[S]

    # 3. Mapping: drafted by the accountant from the workbook (equal to the CSV map), approved by a second person.
    for entity in (H, S):
        csv_rows = readers.read_account_map_csv(dry.files[f"map_{entity}"])
        sheet_rows = workbook.account_map_rows_for(dry.book, entity)
        assert [(r["legacy_code"], r["erp_code"], r["counterparty"] or None) for r in csv_rows] == \
               [(r["legacy_code"], r["erp_code"], r["counterparty"]) for r in sheet_rows]
        assert f"draft v1: {len(f.CHART[entity])} rows" in cmd("migration_map", "--entity", entity, *wb, "--user",
                                                                "dry.acc", "--apply")
        with pytest.raises(CommandError):  # the preparer never approves
            cmd("migration_map", "--entity", entity, "--user", "dry.acc", "--approve", "1", "--apply")
        cmd("migration_map", "--entity", entity, "--user", "dry.apr", "--approve", "1", "--apply")

    # 4. R11: the Falcon journal of December + the TB opening = the TB closing, per code, to the halala. The journal
    # is evidence only (closing TB loaded, OQ-4): replaying it is refused because it touches control accounts (R8).
    journal = readers.read_falcon_journal(dry.files["journal"])
    jr = services.validate_journal(dry.h, journal, user=dry.acc, trial_balance=tb_csv)
    assert jr.summary["entries"] == 6 and jr.summary["debit"] == jr.summary["credit"] == D("209932.11")
    assert len(jr.tie_out) == 20 and all(r["difference"] == 0 for r in jr.tie_out)
    assert {p.code for p in jr.errors} == {"control_account"}
    assert sorted(p.entry_no for p in jr.errors) == ["26004101", "26004102", "26004104", "26004105"]
    tbr = services.validate_trial_balance(dry.h, tb_csv, user=dry.acc, cutover=f.CUTOVER)
    assert tbr.ok and not tbr.warnings
    sheet_tb = workbook.trial_balance_for(dry.book, H)
    assert {r.code: r.closing for r in tb_csv.rows} == {r.code: r.closing for r in sheet_tb.rows}  # CSV = workbook

    # 5. Validate and load: dry run writes nothing; apply drafts one balanced opening entry per entity.
    for entity in (H, S):
        assert "errors / أخطاء: 0" in cmd("migration_validate", "--entity", entity, *wb, "--user", "dry.acc")
        assert "would create (dry run)" in cmd("migration_load", "--entity", entity, *wb, "--user", "dry.acc")
    assert not JournalEntry.objects.exists()
    for entity in (H, S):
        cmd("migration_load", "--entity", entity, *wb, "--user", "dry.acc", "--apply")
        cmd("migration_load", "--entity", entity, *wb, "--user", "dry.acc", "--apply")  # re-run: skipped
    assert ImportedEntry.objects.count() == 2
    for entity, model in ((H, dry.h), (S, dry.s)):
        entry = ImportedEntry.objects.get(entity=model, kind="opening").journal_entry
        lines = list(entry.lines.select_related("account", "counterparty_entity"))
        assert (entry.status, entry.source, entry.reference, entry.date) == ("draft", "import", "FALCON-OPENING",
                                                                            f.CUTOVER)
        assert len(lines) == len(f.CLOSING[entity])  # one line per Falcon code; the TB balances: no 3900 line
        assert entry.total_debit == entry.total_credit == f.EXPECTED_TOTAL_DEBIT[entity]
        for line in lines:
            assert line.account.entity_id == model.pk  # entity separation (rule 4)
            assert (line.debit > 0) != (line.credit > 0)  # one-sided (rule 2)
            assert line.debit == line.debit.quantize(D("0.01")) and line.credit == line.credit.quantize(D("0.01"))
    opening_h = ImportedEntry.objects.get(entity=dry.h, kind="opening").journal_entry
    by_code = {l.account.code: l for l in opening_h.lines.select_related("account", "counterparty_entity")
               if l.account.code in ("1140", "1240", "1241", "4610", "1210")}
    assert by_code["1140"].debit == D("1234.56")  # restricted: allowed on the opening entry only
    assert (by_code["1240"].debit, by_code["1241"].credit, by_code["4610"].credit) == (D("34500.00"), D("2875.00"),
                                                                                       D("1125.00"))
    assert by_code["1210"].counterparty_entity == dry.s  # intercompany line carries its counterparty (rule 6)

    # 6. R22: the posting gate. A load that is 0.01 short on 1200 refuses the posting; nothing is posted.
    r23_loader[H] = {("1200", ""): D("-0.01")}
    with pytest.raises(CommandError):
        cmd("migration_post", "--entity", H, "--user", "dry.apr", "--kind", "opening", "--apply")
    result = services.post_imported_entries(dry.h, user=dry.apr, kind="opening")
    assert result["details"]["opening"] == [
        "opening: Control account 1200: opening entry 44250.50, open items loaded 44250.49, difference 0.01. / "
        "حساب المراقبة 1200: القيد الافتتاحي 44250.50، البنود المفتوحة المحمّلة 44250.49، الفرق 0.01."]
    assert not JournalEntry.objects.filter(status="posted").exists()
    r23_loader.clear()

    # 7. Post: by the approver (never the preparer), through ledger.services.post_entry only.
    calls = []
    real_post = ledger.post_entry
    monkeypatch.setattr(ledger, "post_entry", lambda entry, grant=None, **kw: calls.append(entry.pk) or real_post(
        entry, grant, **kw))
    with pytest.raises(CommandError):
        cmd("migration_post", "--entity", H, "--user", "dry.acc", "--kind", "opening", "--apply")
    for entity in (H, S):
        cmd("migration_post", "--entity", entity, "--user", "dry.apr", "--kind", "opening", "--apply")
    posted = JournalEntry.objects.filter(status="posted").order_by("entity__code")
    assert [(e.entity.code, e.posted_by.username) for e in posted] == [(H, "dry.apr"), (S, "dry.apr")]
    assert sorted(calls) == sorted(e.pk for e in posted)

    # 8. Tie-out: the ERP trial balance = the legacy TB per ERP account, to the halala (hand-worked targets too).
    for entity, model in ((H, dry.h), (S, dry.s)):
        report_csv = tmp_path / f"tieout-{entity}.csv"
        out = cmd("migration_tieout", "--entity", entity, *wb, "--user", "dry.apr", "--as-of", str(f.CUTOVER),
                  "--posted-only", "--report", str(report_csv))
        assert "ties out to the halala" in out
        assert erp_balances(model, dry.apr) == {k: D(v) for k, v in f.EXPECTED_ERP[entity].items()}
        assert report_csv.read_text(encoding="utf-8-sig").startswith("erp_code,expected,actual,difference")
    mirrors = JournalLine.objects.filter(account__code__in=("1210", "2110"), entry__status="posted")
    assert {(l.entry.entity.code, l.counterparty_entity.code) for l in mirrors} == {(H, S), (S, H)}
    assert sum((l.debit - l.credit for l in mirrors), D("0")) == 0  # intercompany balances mirror each other

    # 9. After the opening, the bai' ajel and purification accounts take no manual journal (restricted).
    for code in ("1140", "1240", "1241", "4610"):
        with pytest.raises(LedgerError) as exc:
            ledger.create_entry(dry.h, f.CUTOVER, "manual", [{"account": code, "debit": D("1.00")},
                                                              {"account": "3900", "credit": D("1.00")}], user=dry.acc)
        assert exc.value.code == "restricted_account"


def test_opening_entry_waits_for_the_r23_loader(dry):
    """Without a registered open-item source (invoicing R23 not built) the opening entry with control-account
    balances is drafted but never posted (R22)."""
    services.approve_account_map(services.create_account_map(dry.h, user=dry.acc, rows=f.map_rows(H)), user=dry.apr)
    plan = services.load_opening_balances(dry.h, workbook.trial_balance_for(dry.book, H), user=dry.acc,
                                          cutover=f.CUTOVER, apply=True)
    assert plan.applied
    info = next(p for p in plan.report.problems if p.code == "control_account_open_items")
    assert "1201→1200" in info.message and "1202→1210 (DRYS)" in info.message and "2101→2100" in info.message
    result = services.post_imported_entries(dry.h, user=dry.apr, kind="opening", apply=True)
    assert not result["posted"] and "no open items are loaded" in result["details"]["opening"][0]


def test_sharia_prohibited_balance_in_a_sharia_entity_needs_a_human_decision(dry):
    """2410 / 5610 are fine in DRYS (Sharia not enforced) but block DRYH's opening (OQ-10): workbook and loader."""
    rows = f.workbook_rows()
    rows["falcon_chart"].append({"entity": H, "code": "2401", "name_ar": "قرض تقليدي وهمي", "name_en": "Loan",
                                 "type": "liability", "parent": None, "postable": "yes"})
    rows["account_map"].append({"entity": H, "legacy_code": "2401", "legacy_name": "قرض تقليدي وهمي",
                                "erp_code": "2410", "counterparty": None, "note": None})
    rows["trial_balance"].append({"entity": H, "code": "2401", "name": "قرض تقليدي وهمي", "debit": None,
                                  "credit": D("10000.00")})
    next(r for r in rows["trial_balance"] if r["entity"] == H and r["code"] == "1101")["debit"] += D("10000.00")
    next(r for r in rows["bank_accounts"] if r["code"] == "DRYH-RJ-SAR").update(
        book_balance=D("192345.67"), book_balance_sar=D("192345.67"), statement_balance=D("192345.67"))
    book = workbook.read_workbook(t.filled_workbook(rows))
    report = workbook_checks.check_workbook(book, charts={e.code: workbook_checks.entity_chart(e)
                                                          for e in (dry.h, dry.s)})
    assert [(p.entity, p.code, p.account_code) for p in report.errors] == [(H, "sharia_prohibited", "2401")]
    services.approve_account_map(services.create_account_map(
        dry.h, user=dry.acc, rows=workbook.account_map_rows_for(book, H)), user=dry.apr)
    plan = services.load_opening_balances(dry.h, workbook.trial_balance_for(book, H), user=dry.acc,
                                          cutover=f.CUTOVER, apply=True)
    assert [p.code for p in plan.report.errors] == ["sharia_prohibited"] and not plan.applied
    assert not JournalEntry.objects.exists()


def test_rounding_sar_two_decimals_and_currency_minor_units(dry):
    rows = f.workbook_rows()
    next(r for r in rows["trial_balance"] if r["entity"] == H and r["code"] == "1101")["debit"] += D("0.005")
    next(r for r in rows["open_receivables"] if r["currency"] == "USD")["amount"] = D("2000.005")
    report = workbook_checks.check_workbook(workbook.read_workbook(t.filled_workbook(rows)))
    codes = {(p.sheet, p.code) for p in report.errors}
    assert ("Open receivables", "currency_decimals") in codes  # USD has 2 minor units
    assert ("Trial balance", "unreadable_cell") in codes  # 3 decimals on a SAR amount: refused, never rounded
    assert any("182345.675" in p.message and "(SAR)" in p.message for p in report.errors)
    header, tb = f.tb_table(H)
    tb[0][6] = D("182345.675")
    parsed = readers.read_trial_balance(t.table_csv(header, tb), {"columns": f.TB_COLUMNS})
    services.approve_account_map(services.create_account_map(dry.h, user=dry.acc, rows=f.map_rows(H)), user=dry.apr)
    plan = services.load_opening_balances(dry.h, parsed, user=dry.acc, cutover=f.CUTOVER, apply=True)
    assert "decimals" in {p.code for p in plan.report.errors} and not plan.applied


def test_entity_separation(dry):
    """R19, R24, rule 4: each entity is loaded with the user's own grant there; maps point to the entity's accounts."""
    with pytest.raises(AccessDenied):
        services.create_account_map(dry.h, user=dry.subacc, rows=f.map_rows(H))
    with pytest.raises(MigrationError) as exc:  # a DRYH map row pointing at DRYS's account
        services.create_account_map(dry.h, user=dry.acc, rows=[{"legacy_code": "1101", "legacy_name": "x",
                                                               "erp_code": Account.objects.get(entity=dry.s, code="1100")}])
    assert "another entity" in str(exc.value)
    services.approve_account_map(services.create_account_map(dry.s, user=dry.subacc, rows=f.map_rows(S)),
                                 user=dry.apr)
    with pytest.raises(AccessDenied):
        services.load_opening_balances(dry.h, workbook.trial_balance_for(dry.book, H), user=dry.subacc,
                                       cutover=f.CUTOVER, apply=True)
    plan = services.load_opening_balances(dry.s, workbook.trial_balance_for(dry.book, S), user=dry.subacc,
                                          cutover=f.CUTOVER, apply=True)
    assert plan.applied and list(services.imported_entries(dry.subacc, dry.h)) == []
    with pytest.raises(CommandError):  # the workbook check with a user needs the right in every entity it reads
        cmd("migration_workbook_check", "--workbook", dry.files["workbook"], "--user", "dry.subacc")


def test_bai_ajel_deferred_profit_larger_than_the_receivable_is_an_error(dry):
    rows = f.workbook_rows()
    tb = {r["code"]: r for r in rows["trial_balance"] if r["entity"] == H}
    tb["1252"]["credit"] = D("35000.00")  # deferred profit above the 34,500.00 receivable
    tb["2602"]["credit"] -= D("32125.00")  # keep the TB balanced
    report = workbook_checks.check_workbook(workbook.read_workbook(t.filled_workbook(rows)))
    assert [(p.code, p.account_code) for p in report.errors] == [("bai_ajel_contra", "1241")]
    assert "34500.00" in report.errors[0].message and "المؤجلة" in report.errors[0].message
