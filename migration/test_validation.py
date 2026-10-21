"""migration.md R4 to R11 (validation report; AT-3, AT-4, AT-8, AT-12)."""
from datetime import date
from decimal import Decimal

import pytest

from core.access import AccessDenied
from ledger.models import FiscalPeriod
from migration import services
from migration import testing as t
from migration.conftest import BALANCES, CUTOVER, journal_file, journal_rows, map_rows, tb_file

D = Decimal


def codes(report, severity=None):
    return sorted({p.code for p in report.problems if severity is None or p.severity == severity})


def test_clean_journal_with_trial_balance_ties_out(w, mapped):
    report = services.validate_journal(w.alm, journal_file(), user=w.a, trial_balance=tb_file(), cutover=CUTOVER)
    assert report.ok, [p.message for p in report.errors]
    assert report.summary["entries"] == 4 and report.summary["rows"] == 9
    assert report.summary["debit"] == report.summary["credit"] == D("23000.00")
    assert report.summary["by_source"] == {"مستخلص عميل": 1, "Journal": 3}
    assert codes(report, "error") == []
    assert codes(report) == ["customer_statements"]  # info only: the journal touches no control account
    assert all(t["difference"] == 0 for t in report.tie_out) and len(report.tie_out) == 9
    totals = {t["code"]: t for t in report.account_totals}
    assert totals["1102"]["net"] == D("3000.00") and totals["1102"]["erp_code"] == "1100"
    erp = {t["erp_code"]: t for t in report.erp_totals}
    assert erp["1100"]["legacy_codes"] == "1101,1102" and erp["1100"]["net"] == D("500.00")


def test_at3_unbalanced_entry_both_sides_decimals_one_line_several_dates(w, mapped):
    rows = journal_rows()
    rows[1]["credit"] = 9000.0  # entry 26000001 no longer balances
    rows[3]["credit"] = 5.0  # debit and credit on one line
    rows[5]["debit"] = 8000.005  # three decimals
    rows[8]["date"] = rows[8]["date"].replace(day=11)  # entry 26000004 on two dates
    rows.append(t.line(26000005, date(2026, 3, 1), "5201", "مصروف إيجار", debit="10.00"))
    report = services.validate_journal(w.alm, journal_file(rows), user=w.a)
    assert {"unbalanced", "both_sides", "decimals", "several_dates", "one_line"} <= set(codes(report, "error"))
    unbalanced = [p for p in report.errors if p.code == "unbalanced"]
    assert {p.entry_no for p in unbalanced} >= {"26000001"}
    assert all(" / " in p.message for p in report.problems)


def test_zero_and_negative_lines(w, mapped):
    rows = journal_rows()
    rows[0]["debit"] = None
    rows[1]["credit"] = -10000.0
    report = services.validate_journal(w.alm, journal_file(rows), user=w.a)
    assert {"no_amount", "negative"} <= set(codes(report, "error"))


def test_at4_unmapped_code_is_listed_with_totals_and_blocks_loading(w):
    amap = services.create_account_map(w.alm, user=w.a, rows=[r for r in map_rows() if r["legacy_code"] != "5201"])
    t.approve_map(amap, approver=w.p)
    report = services.validate_journal(w.alm, journal_file(), user=w.a)
    assert codes(report, "error") == ["unmapped"]
    assert report.unmapped == [{"code": "5201", "name": "مصروف إيجار", "rows": 1, "debit": D("3000.00"), "credit": D("0")}]
    plan = services.load_journal_entries(w.alm, journal_file(), user=w.a, apply=True)
    assert not plan.applied and not plan.created


def test_draft_map_is_reported_as_not_approved(w):
    services.create_account_map(w.alm, user=w.a, rows=map_rows())
    report = services.validate_journal(w.alm, journal_file(), user=w.a)
    assert codes(report, "error") == ["map_not_approved"] and report.map_status == "draft"
    assert codes(services.validate_journal(w.sub, journal_file(), user=w.s), "error") == ["no_map", "unmapped"]


def test_at12_closed_period_missing_period_and_cutover(w, mapped):
    FiscalPeriod.objects.filter(entity=w.alm, name="2026-02").update(status="closed")
    rows = journal_rows()
    rows += [t.line(28000001, date(2028, 1, 5), "5201", "مصروف إيجار", debit="1.00"),
             t.line(28000001, date(2028, 1, 5), "1101", "الصندوق الرئيسي", credit="1.00")]
    report = services.validate_journal(w.alm, journal_file(rows), user=w.a, cutover=date(2026, 1, 15))
    errors = {(p.code, p.entry_no) for p in report.errors}
    assert ("period_closed", "26000003") in errors and ("period_closed", "26000004") in errors
    assert ("no_period", "28000001") in errors
    assert ("before_cutover", "26000001") in errors and ("before_cutover", "26000002") not in errors


def test_numbering_gap_year_prefix_and_duplicate_rows_are_warnings(w, mapped):
    rows = [r for r in journal_rows() if r["entry_no"] != 26000002]  # gap
    rows += [dict(rows[-1]), dict(rows[-2])]  # 26000004 duplicated (still balances)
    for r in rows:
        if r["entry_no"] == 26000003:
            r["date"] = r["date"].replace(year=2027)  # prefix 26, dated 2027
    report = services.validate_journal(w.alm, journal_file(rows), user=w.a)
    assert {"numbering_gap", "number_year", "duplicate_row"} <= set(codes(report, "warning"))
    gap = next(p for p in report.problems if p.code == "numbering_gap")
    assert "26000002" in gap.message


def test_at8_tie_out_difference_of_one_halala(w, mapped):
    rows = journal_rows()
    rows[3]["debit"], rows[4]["credit"] = 500.01, 500.01  # still balanced, but 1101 and 1102 move by 0.01
    report = services.validate_journal(w.alm, journal_file(rows), user=w.a, trial_balance=tb_file())
    diffs = {p.account_code: p for p in report.errors if p.code in ("tie_out", "movement_difference")}
    assert set(diffs) == {"1101", "1102"}
    line = next(t for t in report.tie_out if t["code"] == "1102")
    assert line["difference"] == D("0.01")


def test_tie_out_codes_on_one_side_only(w, mapped):
    balances = dict(BALANCES)
    del balances["5201"]
    balances["1101"] = ("الصندوق الرئيسي", "13000.00", "500.00", "3000.00")  # keeps the TB balanced
    report = services.validate_journal(w.alm, journal_file(), user=w.a, trial_balance=tb_file(balances))
    assert ("not_in_tb", "5201") in {(p.code, p.account_code) for p in report.errors}


def test_trial_balance_checks(w, mapped):
    balances = dict(BALANCES)
    balances["2101"] = ("موردون تجريبيون", "-15000.10", "0", "0")
    extra = [["110", "النقدية", 0, 0, 0, 0, "1.00", 0],  # header with a wrong subtotal
             ["1201", "عملاء مكرر", 0, 0, 0, 0, 0, 0],   # duplicate code
             ["5301", "مصروف غير مربوط", 0, 0, "5.00", 0, "4.00", 0]]  # 0 + 5 != 4, and not mapped
    report = services.validate_trial_balance(w.alm, tb_file(balances, extra), user=w.a, cutover=CUTOVER)
    errors = set(codes(report, "error"))
    assert {"duplicate_code", "row_inconsistent", "tb_unbalanced_closing", "tb_unbalanced_opening", "unmapped"} <= errors
    assert "header_subtotal" in codes(report, "warning") and "headers_excluded" in codes(report, "info")


def test_trial_balance_basis_opening_needs_opening_columns(w, mapped):
    data = t.table_csv(["code", "closing"], [["1101", "10"], ["2101", "-10"]])
    from migration import readers
    tb = readers.read_trial_balance(data, {"code": "code", "closing": "closing"})
    report = services.validate_trial_balance(w.alm, tb, user=w.a, basis="opening")
    assert "no_opening_column" in codes(report, "error")


def test_sharia_prohibited_target_needs_a_human_decision(w):
    rows = map_rows() + [{"legacy_code": "2401", "legacy_name": "قرض بنكي", "erp_code": "2410"}]
    t.approve_map(services.create_account_map(w.alm, user=w.a, rows=rows), approver=w.p)
    balances = dict(BALANCES)
    balances["2401"] = ("قرض بنكي", "-1000.00", "0", "0")
    balances["1101"] = ("الصندوق الرئيسي", "11000.00", "500.00", "3000.00")
    report = services.validate_trial_balance(w.alm, tb_file(balances), user=w.a, cutover=CUTOVER)
    assert codes(report, "error") == ["sharia_prohibited"]


def test_validation_needs_the_import_right_in_that_entity(w, mapped):
    with pytest.raises(AccessDenied):
        services.validate_journal(w.alm, journal_file(), user=w.s)
    with pytest.raises(AccessDenied):
        services.validate_journal(w.alm, journal_file(), user=w.p)


def test_at23_control_accounts_allowed_in_the_opening_entry_refused_in_replayed_journals(w, mapped):
    """R8 as amended (M6, M7): opening balances on control accounts are allowed (no warning, gated by R22); a replayed
    journal line on one is an error naming the entry and the account, and blocks loading."""
    report = services.validate_trial_balance(w.alm, tb_file(), user=w.a, cutover=CUTOVER)
    assert report.ok and not report.warnings
    info = next(p for p in report.problems if p.code == "control_account_open_items")
    assert "1201→1200" in info.message and "2101→2100" in info.message and "R22" in info.message
    assert "حسابات المراقبة" in info.message

    rows = journal_rows() + [t.line(26000005, date(2026, 3, 1), "1201", "عملاء تجريبيون", debit="1150.00",
                                    source="مستخلص عميل", source_no=502),
                             t.line(26000005, date(2026, 3, 1), "4101", "إيرادات إيجار", credit="1150.00",
                                    source="مستخلص عميل", source_no=502)]
    report = services.validate_journal(w.alm, journal_file(rows), user=w.a, cutover=CUTOVER)
    errors = [p for p in report.errors if p.code == "control_account"]
    assert [(p.entry_no, p.account_code) for p in errors] == [("26000005", "1201")]
    assert "replayed journals cannot post to control accounts" in errors[0].message and "1201→1200" in errors[0].message
    assert "حسابات المراقبة" in errors[0].message
    plan = services.load_journal_entries(w.alm, journal_file(rows), user=w.a, cutover=CUTOVER, apply=True)
    assert not plan.applied and not plan.created  # the whole run is refused, the clean entries too
