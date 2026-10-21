"""One list of control accounts for every module (invoicing R97 policy A; migration R8, R22; dry run 2026-10-21
gap 7): read from the chart template, used by migration and invoicing alike."""
from ledger import control_accounts
from ledger.coa_template import TEMPLATE


def test_the_subledger_control_accounts_are_the_four_open_item_accounts_of_the_chart():
    assert control_accounts.SUBLEDGER_CONTROL_CODES == {"1200", "1210", "2100", "2110"}
    chart = {code for code, *_ in TEMPLATE}
    assert control_accounts.SUBLEDGER_CONTROL_CODES <= chart
    assert not {"2310", "2320"} & chart  # named in early drafts of policy A, never in the chart
    assert control_accounts.TAX_CONTROL_CODES == {"1310", "2200", "2220"}
    assert not control_accounts.SUBLEDGER_CONTROL_CODES & control_accounts.TAX_CONTROL_CODES


def test_migration_and_invoicing_use_the_same_list():
    from invoicing import services as invoicing
    from migration import services as migration
    from migration import workbook_checks

    assert migration.CONTROL_CODES is control_accounts.SUBLEDGER_CONTROL_CODES
    assert invoicing.CONTROL_ACCOUNTS == (control_accounts.SUBLEDGER_CONTROL_CODES | control_accounts.TAX_CONTROL_CODES
                                          | {invoicing.PURIFICATION_BANK})
    assert {workbook_checks.AR_CONTROL, workbook_checks.AR_GROUP, workbook_checks.AP_CONTROL,
            workbook_checks.AP_GROUP} == control_accounts.SUBLEDGER_CONTROL_CODES
    assert control_accounts.is_subledger_control("1210") and not control_accounts.is_subledger_control("2310")
