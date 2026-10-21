"""migration.md §11 W4 to W11 (workbook validation; AT-19, AT-20). Synthetic workbooks only, built in the test."""
from datetime import date
from decimal import Decimal

import pytest

from migration import testing as t
from migration import workbook, workbook_checks

D = Decimal


def check(rows=None, **kw):
    return workbook_checks.check_workbook(workbook.read_workbook(t.filled_workbook(rows)), **kw)


def codes(report):
    return {p.code for p in report.errors}


def find(rows, sheet, **match):
    return next(r for r in rows[sheet] if all(r.get(k) == v for k, v in match.items()))


def test_at19_coherent_workbook_has_no_problem_and_every_tie_out_is_zero():
    report = check()
    assert report.problems == [] and report.ok
    ties = {(x["entity"], x["check"], x["account"]): (x["subledger"], x["ledger"], x["difference"]) for x in report.ties}
    assert ties == {
        ("ALM", "open_receivables", "1200"): (D("23000.00"), D("23000.00"), 0),
        ("ALM", "open_receivables", "1210/SUB"): (D("5000.00"), D("5000.00"), 0),
        ("ALM", "open_payables", "2100"): (D("11500.00"), D("11500.00"), 0),
        ("ALM", "bank_accounts", "1101"): (D("50000.00"), D("50000.00"), 0),
        ("ALM", "bank_accounts", "1102"): (D("3750.00"), D("3750.00"), 0),
        ("ALM", "employees", "2500"): (D("8000.00"), D("8000.00"), 0),
        ("ALM", "fixed_assets_cost", "1501"): (D("120000.00"), D("120000.00"), 0),
        ("ALM", "fixed_assets_accumulated", "1591"): (D("30000.00"), D("30000.00"), 0),
        ("SUB", "open_payables", "2110/ALM"): (D("5000.00"), D("5000.00"), 0),
        ("SUB", "bank_accounts", "1101"): (D("55000.00"), D("55000.00"), 0),
        ("ALM", "intercompany_mirror", "ALM/SUB"): (D("5000.00"), D("5000.00"), 0),  # W17
    }
    assert report.summary["ALM"]["tb_debit"] == report.summary["ALM"]["tb_credit"] == D("209500.00")
    assert report.chart_source == {"ALM": "template", "SUB": "template"}


def _tb(rows, entity, code):
    return find(rows, "trial_balance", entity=entity, code=code)


def m_tb_unbalanced(rows):
    _tb(rows, "ALM", "2602")["credit"] = D("20000.01")
    return {"tb_unbalanced"}


def m_unmapped(rows):
    rows["account_map"] = [r for r in rows["account_map"] if r["legacy_code"] != "4101"]
    return {"unmapped"}


def m_ar_off(rows):
    find(rows, "open_receivables", document_no="INV-1001").update(amount=D("15000.01"), sar_amount=D("15000.01"))
    return {"tie_out"}


def m_ar_group_off(rows):
    find(rows, "customers", code="C-SUB")["group_entity"] = None  # its invoice then sits on 1200, not 1210/SUB
    return {"tie_out"}


def m_ap_off(rows):
    find(rows, "open_payables", document_no="B-77").update(amount=D("11499.99"), sar_amount=D("11499.99"))
    return {"tie_out"}


def m_bank_off(rows):
    find(rows, "bank_accounts", code="ALM-RJ-SAR").update(book_balance=D("50000.01"), book_balance_sar=D("50000.01"))
    return {"tie_out"}


def m_bank_sar_mismatch(rows):
    find(rows, "bank_accounts", code="ALM-RJ-SAR")["book_balance"] = D("49000.00")
    return {"sar_mismatch"}


def m_eosb_off(rows):
    find(rows, "employees", employee_no="T-002")["eosb_provision"] = D("3000.01")
    return {"tie_out"}


def m_asset_cost_off(rows):
    find(rows, "fixed_assets", asset_code="FA-0001")["cost"] = D("119999.99")
    return {"tie_out"}


def m_asset_accumulated_off(rows):
    find(rows, "fixed_assets", asset_code="FA-0001")["accumulated_depreciation"] = D("30000.01")
    return {"tie_out"}


def m_asset_register_missing(rows):
    rows["fixed_assets"] = []
    return {"tie_out"}


def m_intercompany_without_counterparty(rows):
    find(rows, "account_map", entity="ALM", legacy_code="1202")["counterparty"] = None
    return {"counterparty_missing", "tie_out", "intercompany_mirror"}  # SUB's 2110 (ALM) has no mirror left (W17)


def m_counterparty_on_ordinary_account(rows):
    find(rows, "account_map", entity="ALM", legacy_code="1201")["counterparty"] = "SUB"
    return {"counterparty_not_intercompany", "tie_out"}


def m_sharia_prohibited(rows):
    find(rows, "account_map", entity="ALM", legacy_code="2602")["erp_code"] = "2410"
    return {"sharia_prohibited"}


def m_class_mismatch(rows):
    find(rows, "account_map", entity="ALM", legacy_code="4101")["erp_code"] = "1100"
    return {"class_mismatch"}


def m_header_erp_account(rows):
    find(rows, "account_map", entity="ALM", legacy_code="5201")["erp_code"] = "5000"
    return {"not_postable"}


def m_unknown_erp_account(rows):
    find(rows, "account_map", entity="ALM", legacy_code="5201")["erp_code"] = "5999"
    return {"unknown_account"}


def m_tb_not_in_chart(rows):
    rows["trial_balance"].append({"entity": "ALM", "code": "5301", "debit": D("1.00")})
    _tb(rows, "ALM", "5101")["debit"] = D("7749.00")
    return {"not_in_chart", "unmapped"}


def m_tb_header_row(rows):
    rows["trial_balance"].append({"entity": "ALM", "code": "110", "debit": D("0.00")})
    return {"not_postable", "tb_header"}


def m_both_sides(rows):
    _tb(rows, "ALM", "5101").update(debit=D("7760.00"), credit=D("10.00"))
    return {"both_sides"}


def m_sar_three_decimals(rows):
    _tb(rows, "ALM", "5101")["debit"] = D("7750.001")
    return {"unreadable_cell", "tb_unbalanced"}


def m_foreign_decimals(rows):
    find(rows, "open_receivables", document_no="INV-1002")["amount"] = D("2400.005")
    return {"currency_decimals"}


def m_sar_item_mismatch(rows):
    find(rows, "open_receivables", document_no="INV-1001")["amount"] = D("15000.50")
    return {"sar_mismatch"}


def m_after_cutover(rows):
    find(rows, "open_payables", document_no="B-77")["document_date"] = date(2027, 1, 2)
    return {"after_cutover"}


def m_unknown_party(rows):
    find(rows, "open_receivables", document_no="CN-0001")["customer"] = "C999"
    return {"unknown_party"}  # still counted on the control account: the total ties


def m_duplicate_customer(rows):
    rows["customers"].append(dict(find(rows, "customers", code="C001")))
    return {"duplicate"}


def m_unknown_entity(rows):
    rows["vendors"].append({**find(rows, "vendors", code="V001"), "entity": "ZZZ"})
    return {"unknown_entity"}


def m_required_blank(rows):
    find(rows, "customers", code="C001")["name_ar"] = None
    return {"required"}


def m_bad_vat(rows):
    find(rows, "customers", code="C001")["vat_number"] = "300000000000001"
    return {"unreadable_cell"}


def m_bad_iban(rows):
    iban = find(rows, "bank_accounts", code="SUB-RJ-SAR")["iban"]
    find(rows, "bank_accounts", code="SUB-RJ-SAR")["iban"] = iban[:-1] + ("1" if iban[-1] != "1" else "2")
    return {"iban"}


def m_non_resident_without_country(rows):
    find(rows, "vendors", code="V001")["resident"] = "no"
    return {"residence_country"}


def m_two_purification_accounts(rows):
    for r in rows["bank_accounts"][:2]:
        r["is_purification"] = "yes"
    return {"purification_twice", "purification_currency"}


def m_asset_accumulated_above_cost(rows):
    find(rows, "fixed_assets", asset_code="FA-0001").update(cost=D("1000.00"), accumulated_depreciation=D("2000.00"))
    return {"accumulated_above_cost", "tie_out"}


def m_parent_is_self(rows):
    find(rows, "entities", entity_code="SUB")["parent_entity"] = "SUB"
    return {"parent_self"}


def m_functional_currency(rows):
    find(rows, "entities", entity_code="SUB")["functional_currency"] = "KWD"
    return {"functional_currency"}


MUTATIONS = [m for name, m in sorted(globals().items()) if name.startswith("m_")]


@pytest.mark.parametrize("mutate", MUTATIONS, ids=lambda m: m.__name__[2:])
def test_at20_each_mistake_is_an_error(mutate):
    rows = t.workbook_rows()
    expected = mutate(rows)
    report = check(rows)
    assert codes(report) == expected, [p.message for p in report.errors]
    for p in report.errors:
        assert p.sheet and " / " in p.message  # sheet named; English / Arabic


def test_at20_difference_is_reported_with_its_amount_and_account():
    rows = t.workbook_rows()
    m_ar_off(rows)
    report = check(rows)
    (problem,) = report.errors
    assert problem.sheet == "Open receivables" and problem.entity == "ALM" and problem.account_code == "1200"
    assert "difference 0.01" in problem.message and "الفرق 0.01" in problem.message
    rows = t.workbook_rows()
    m_tb_unbalanced(rows)
    (problem,) = check(rows).errors
    assert "difference -0.01" in problem.message


def test_warnings_need_a_person_but_do_not_block():
    rows = t.workbook_rows()
    rows["account_map"] = [r for r in rows["account_map"] if r["legacy_code"] != "5201"]  # no balance: warning only
    find(rows, "employees", employee_no="T-001")["gosi_cohort"] = None
    find(rows, "bank_accounts", code="SUB-RJ-SAR")["statement_balance"] = None
    report = check(rows)
    assert report.ok and {p.code for p in report.warnings} == {"chart_unmapped", "gosi_cohort", "no_statement_balance"}


def test_personal_data_never_appears_in_messages_or_the_csv(tmp_path):
    rows = t.workbook_rows()
    rows["employees"].append({**rows["employees"][0], "employee_no": "T-003"})  # same ID number twice
    find(rows, "employees", employee_no="T-002")["iban"] = "SA0000000000000000000000"
    report = check(rows)
    assert {"duplicate_id", "iban"} <= codes(report)
    path = workbook_checks.write_csv(report, tmp_path / "report.csv")
    text = path.read_text(encoding="utf-8-sig") + "\n".join(workbook_checks.render(report))
    for employee in rows["employees"]:
        for key in ("full_name_ar", "full_name_en", "id_number", "iban"):
            assert str(employee[key]) not in text
        assert "1990-01-01" not in text and "10000.00" not in text


def test_entity_filter_and_unknown_entity():
    rows = t.workbook_rows()
    m_ap_off(rows)  # an ALM problem
    report = check(rows, entities=["SUB"])
    assert report.ok and set(report.summary) == {"SUB"}
    assert "unknown_entity" in codes(check(entities=["NOPE"]))


def test_an_entity_chart_replaces_the_template():
    chart = workbook_checks.template_chart()
    chart.pop("1520")
    report = check(charts={"ALM": chart})
    assert codes(report) == {"unknown_account"}  # 1501's ERP account is not in this entity's chart
    assert report.chart_source["ALM"] == "erp"


# --- W15 to W17 (dry run 2026-10-21 gaps 7 to 9) --------------------------------------------------------------------

def _add_code(rows, entity, code, name, typ, erp, debit=None, credit=None, counterparty=None):
    """A Falcon code with a balance, on the chart, TB and map; retained earnings (2602) keep the ALM TB balanced."""
    rows["falcon_chart"].append({"entity": entity, "code": code, "name_ar": name, "name_en": None, "type": typ,
                                 "parent": None, "postable": "yes"})
    rows["trial_balance"].append({"entity": entity, "code": code, "name": name, "debit": debit, "credit": credit})
    rows["account_map"].append({"entity": entity, "legacy_code": code, "legacy_name": name, "erp_code": erp,
                                "counterparty": counterparty, "note": None})
    if entity == "ALM":
        _tb(rows, "ALM", "2602")["credit"] += (debit or D("0")) - (credit or D("0"))


def _purification(rows, held, payable):
    _add_code(rows, "ALM", "1103", "حساب تطهير اختبار", "asset", "1140", debit=D(held))
    _add_code(rows, "ALM", "2701", "مستحق للتطهير", "liability", "2600", credit=D(payable))
    rows["bank_accounts"].append({**find(rows, "bank_accounts", code="ALM-RJ-SAR"), "code": "ALM-PUR",
                                  "iban": t._wb_iban("80", "100009"), "falcon_code": "1103", "book_balance": D(held),
                                  "book_balance_sar": D(held), "statement_balance": D(held), "is_purification": "yes"})


def test_w16_purification_account_ties_to_the_charity_payable():
    rows = t.workbook_rows()
    _purification(rows, "1234.56", "1234.56")
    report = check(rows)
    assert report.ok and not {p.code for p in report.warnings} & {"purification_to_fund", "purification_over_funded"}
    tie = next(x for x in report.ties if x["check"] == "purification")
    assert (tie["account"], tie["subledger"], tie["ledger"], tie["difference"]) == ("1140/2600", D("1234.56"),
                                                                                    D("1234.56"), 0)


def test_w16_more_on_the_purification_account_than_owed_to_charity_is_an_error():
    rows = t.workbook_rows()
    _purification(rows, "1500.00", "1234.56")
    report = check(rows)
    (problem,) = [p for p in report.errors if p.code == "purification_over_funded"]
    assert problem.account_code == "1140" and "265.44" in problem.message and "التطهير" in problem.message


def test_w16_less_on_the_purification_account_is_a_warning_to_fund():
    rows = t.workbook_rows()
    _purification(rows, "1000.00", "1234.56")
    report = check(rows)
    assert report.ok
    (warning,) = [p for p in report.warnings if p.code == "purification_to_fund"]
    assert "234.56" in warning.message and " / " in warning.message


def test_w15_vat_and_withholding_tax_balances_are_agreed_to_the_last_returns():
    rows = t.workbook_rows()
    _add_code(rows, "ALM", "2201", "ضريبة القيمة المضافة", "liability", "2200", credit=D("1500.00"))
    _add_code(rows, "ALM", "2221", "ضريبة الاستقطاع", "liability", "2220", credit=D("300.00"))
    report = check(rows)
    assert report.ok
    assert sorted(p.account_code for p in report.warnings if p.code == "tax_balance_evidence") == ["2200", "2220"]


def test_w17_intercompany_balances_mirror_each_other_across_entities():
    report = check()
    tie = next(x for x in report.ties if x["check"] == "intercompany_mirror")
    assert (tie["account"], tie["subledger"], tie["ledger"], tie["difference"]) == ("ALM/SUB", D("5000.00"),
                                                                                    D("5000.00"), 0)
    rows = t.workbook_rows()
    _tb(rows, "ALM", "1202")["debit"] = D("5000.01")  # ALM says SUB owes 5,000.01; SUB says it owes 5,000.00
    _tb(rows, "ALM", "2602")["credit"] += D("0.01")
    find(rows, "open_receivables", document_no="INV-1003").update(amount=D("5000.01"), sar_amount=D("5000.01"))
    report = check(rows)
    (problem,) = report.errors
    assert problem.code == "intercompany_mirror" and "0.01" in problem.message and "بين الشركات" in problem.message
    report = check(rows, entities=["ALM"])  # SUB not in this run: not compared, and said so
    assert report.ok and "intercompany_mirror_not_checked" in {p.code for p in report.warnings}


def test_w17_the_erp_sharia_flag_governs_the_workbook_flag():
    rows = t.workbook_rows()
    _add_code(rows, "SUB", "2401", "قرض تقليدي", "liability", "2410", credit=D("1000.00"))
    find(rows, "trial_balance", entity="SUB", code="1101")["debit"] += D("1000.00")
    find(rows, "bank_accounts", code="SUB-RJ-SAR").update(book_balance=D("56000.00"), book_balance_sar=D("56000.00"))
    assert ("sharia_prohibited", "SUB") in {(p.code, p.entity) for p in check(rows).errors}  # the sheet says yes
    report = check(rows, erp_entities={"ALM": True, "SUB": False})  # the ERP says SUB does not enforce Sharia
    assert [(p.code, p.entity) for p in report.errors] == [("sharia_flag_mismatch", "SUB")]
    assert "the ERP setting governs" in report.errors[0].message
    rows["entities"][1]["sharia_enforced"] = "no"  # the workbook corrected: the loan is a legitimate SUB balance
    assert check(rows, erp_entities={"ALM": True, "SUB": False}).ok
