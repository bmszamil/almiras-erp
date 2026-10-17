"""migration.md R1 to R3, R10 (readers; AT-1, AT-2). Synthetic files only."""
from datetime import date
from decimal import Decimal

import pytest

from migration import readers
from migration import testing as t
from migration.conftest import journal_rows

D = Decimal


def test_normalise_header_folds_spacing_and_letter_variants():
    assert readers.normalise_header("  نوع   الحركة ") == readers.normalise_header("نوع الحركه")
    assert readers.normalise_header("مركز التكلفه") == readers.normalise_header("مركز التكلفة")
    assert readers.normalise_header("رقم الـقـيد") == readers.normalise_header("رقم القيد")  # tatweel
    assert readers.normalise_header("الحركة") != readers.normalise_header("رقم الحركة")
    assert readers.normalise_header("Code") == "code"


def test_at1_reads_falcon_journal_with_title_rows_variants_and_reordered_columns():
    order = list(reversed(t.KEYS))
    data = t.falcon_xlsx(
        journal_rows(), order=order, title_rows=[["شركة تجريبية - دفتر اليومية العامة"], []],
        headers={"journal_type": "نوع الحركة", "entry_no": "  رقم   القيد ", "cost_centre": "مركز التكلفه"},
        extra_sheets=["ملخص"],
    )
    parsed = readers.read_falcon_journal(data)
    assert len(parsed.rows) == 9
    first = parsed.rows[0]
    assert first.entry_no == "26000001" and first.account_code == "1201" and first.date == date(2026, 1, 15)
    assert first.debit == D("11500.00") and isinstance(first.debit, Decimal) and first.credit == D("0")
    assert first.account_name == "عملاء تجريبيون" and first.source == "مستخلص عميل" and first.source_no == "501"
    assert parsed.rows[5].cost_centre == "الإدارة"
    assert first.row == 4  # two title rows, then the header
    assert [p.code for p in parsed.problems] == ["other_sheets"]
    assert len(parsed.sha256) == 64


def test_at2_missing_required_column_is_refused_in_both_languages():
    order = [k for k in t.KEYS if k != "account_code"]
    with pytest.raises(readers.ReaderError) as exc:
        readers.read_falcon_journal(t.falcon_xlsx(journal_rows(), order=order))
    assert "كود الحساب" in str(exc.value) and "Account code" in str(exc.value)


def test_unreadable_cells_are_row_problems_not_dropped_rows():
    rows = journal_rows()
    rows[0]["debit"] = "abc"
    rows[1]["account_code"] = 12  # 2 digits
    rows[2]["date"] = "not a date"
    parsed = readers.read_falcon_journal(t.falcon_xlsx(rows))
    assert len(parsed.rows) == 9
    assert {(p.code, p.row) for p in parsed.problems} == {("unreadable_cell", 2), ("unreadable_cell", 3),
                                                         ("unreadable_cell", 4)}


def test_amount_cells_text_arabic_digits_and_float_artifacts():
    stats = {}
    assert readers.to_amount("١٬٢٣٤٫٥٠") == D("1234.50")
    assert readers.to_amount("1,234.56") == D("1234.56")
    assert readers.to_amount("(10.00)") == D("-10.00")
    assert readers.to_amount(None) == 0 and readers.to_amount("  ") == 0
    assert readers.to_amount(0.1 + 0.2, stats) == D("0.30") and stats["float_artifacts"] == 1
    assert readers.to_amount(10.005) == D("10.005")  # a real third decimal is kept and reported by validation
    with pytest.raises(readers.CellError):
        readers.to_amount(True)
    assert readers.to_code(1101.0) == "1101" and readers.to_code("١١٠١") == "1101"


def test_blank_rows_are_counted_and_skipped():
    rows = journal_rows()
    rows.insert(2, {k: None for k in t.KEYS})
    parsed = readers.read_falcon_journal(t.falcon_xlsx(rows))
    assert len(parsed.rows) == 9 and parsed.stats["blank_rows"] == 1


def test_trial_balance_with_column_map_csv_utf8_and_windows_1256():
    rows = t.tb_rows({"1101": ("الصندوق", "100.00", "50.00", "20.00"), "2101": ("موردون", "-100.00", "20.00", "50.00")})
    for encoding in ("utf-8-sig", "cp1256"):
        parsed = readers.read_trial_balance(t.table_csv(t.TB_HEADER, rows, encoding=encoding), {"columns": t.TB_COLUMNS})
        assert parsed.encoding == encoding
        r = parsed.rows[0]
        assert (r.code, r.name, r.opening, r.movement, r.closing) == ("1101", "الصندوق", D("100.00"), D("30.00"), D("130.00"))


def test_trial_balance_column_numbers_signed_columns_and_headers_by_prefix():
    data = t.table_xlsx(["x", "y", "z"], [["110", "أصول نقدية", "150"], ["1101", "صندوق", "100"],
                                          ["1102", "بنك", "50"], ["2101", "موردون", "-150"], ["", "الإجمالي", "0"]],
                        title_rows=[["ميزان مراجعة تجريبي"]])
    parsed = readers.read_trial_balance(data, {"columns": {"code": 1, "name": 2, "closing": 3}, "header_row": 2})
    assert [(r.code, r.is_header, r.closing) for r in parsed.rows] == [
        ("110", True, D("150")), ("1101", False, D("100")), ("1102", False, D("50")), ("2101", False, D("-150"))]
    assert parsed.stats["rows_without_code"] == 1


def test_trial_balance_needs_a_closing_column_and_known_keys():
    with pytest.raises(readers.ReaderError):
        readers.read_trial_balance(t.table_csv(["code"], [["1101"]]), {"columns": {"code": "code"}})
    with pytest.raises(readers.ReaderError) as exc:
        readers.read_trial_balance(t.table_csv(["code"], [["1101"]]), {"columns": {"code": "code", "closing": "c",
                                                                                   "colour": "x"}})
    assert "colour" in str(exc.value)


def test_column_map_from_json_text_and_chart_reader():
    spec = readers.load_column_map('{"code": "كود الحساب", "name": "اسم الحساب", "type": "نوع الحساب"}')
    data = t.table_csv(["كود الحساب", "اسم الحساب", "نوع الحساب"], [["1101", "الصندوق", "أصول"], ["4101", "إيرادات", "إيراد"]])
    parsed = readers.read_chart(data, spec)
    assert [(r.code, r.name, r.type) for r in parsed.rows] == [("1101", "الصندوق", "أصول"), ("4101", "إيرادات", "إيراد")]


def test_account_map_csv():
    data = t.table_csv(["legacy_code", "legacy_name", "erp_code", "counterparty", "note"],
                       [["1101", "الصندوق", "1100", "", ""], ["", "", "", "", ""]])
    assert readers.read_account_map_csv(data) == [{"legacy_code": "1101", "legacy_name": "الصندوق", "erp_code": "1100",
                                                   "counterparty": "", "note": "", "row": 2}]
    with pytest.raises(readers.ReaderError):
        readers.read_account_map_csv(t.table_csv(["code"], [["1"]]))


def test_not_an_xlsx_file():
    with pytest.raises(readers.ReaderError):
        readers.read_falcon_journal(b"PK\x03\x04 broken")


def test_committed_example_column_map_and_template_are_usable():
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    spec = readers.load_column_map(repo / "docs" / "migration" / "falcon-tb-columns.example.json")
    header = [spec["columns"]["code"][0], spec["columns"]["name"], *[spec["columns"][k] for k in (
        "opening_debit", "opening_credit", "movement_debit", "movement_credit", "closing_debit", "closing_credit")]]
    parsed = readers.read_trial_balance(t.table_csv(header, [["1101", "الصندوق", 0, 0, 5, 0, 5, 0]]), spec)
    assert parsed.rows[0].closing == D("5")
    template = (repo / "docs" / "migration" / "account-map-template.csv").read_bytes()
    assert readers.read_account_map_csv(template) == []
