"""migration.md R22, R23, W10, W14 (OQ-12; dry run 2026-10-21 gaps 1, 4 and 5): the sub-ledger loaders of the
workbook, each through its own module's services and rights. Synthetic workbooks (``migration.testing``), fictional
names and numbers only."""
from datetime import date
from decimal import Decimal

import pytest

from banking.models import BankAccount
from core.access import AccessDenied
from core.access.testing import make_user, member
from hr import services as hr
from hr.models import Employee, LeaveAdjustment
from invoicing import migration_port
from invoicing import services as invoicing
from invoicing.models import Customer, DeferredSale, InvoicingError, MigratedOpenItem, SalesDocument, Vendor
from ledger.models import JournalEntry
from migration import services, subledgers, workbook
from migration import testing as t
from migration.models import ImportedEntry
from payroll import services as payroll
from payroll.models import DeferredSaleContract, EosbProvisionEntry

pytestmark = pytest.mark.django_db
D = Decimal
CUT = t.WB_CUTOVER


def book(rows=None):
    return workbook.read_workbook(t.filled_workbook(rows))


def find(rows, sheet, **match):
    return next(r for r in rows[sheet] if all(r.get(k) == v for k, v in match.items()))


@pytest.fixture
def people(w):
    """HR set up in ALM; its hr officer ``h`` and payroll officer ``po``; SUB's approver for bank accounts."""
    hr.setup_entity(w.alm)
    for name, role in (("h", "hr_officer"), ("po", "payroll_officer")):
        user = make_user(f"mig.{name}")
        member(user, w.alm, role)
        setattr(w, name, user)
    return w


def test_r23_open_items_load_into_invoicing_as_migrated_documents(w):
    wb = book()
    plan = subledgers.load_subledgers(w.alm, wb, ["receivables", "payables"], user=w.a)  # dry run
    assert plan.ok and not plan.applied
    assert (plan.sections["receivables"].created, plan.sections["payables"].created) == (7, 2)  # 3 + 4; 1 + 1
    assert not Customer.objects.exists() and not MigratedOpenItem.objects.exists()  # nothing kept
    plan = subledgers.load_subledgers(w.alm, wb, ["receivables", "payables"], user=w.a, apply=True)
    assert plan.applied
    items = {i.legacy_number: i for i in MigratedOpenItem.objects.select_related("customer", "vendor", "counterparty")}
    assert set(items) == {"INV-1001", "CN-0001", "INV-1002", "INV-1003", "B-77"}
    usd = items["INV-1002"]
    assert (usd.currency_id, usd.amount, usd.amount_sar, usd.signed_sar) == ("USD", D("2400.000"), D("9000.00"),
                                                                             D("9000.00"))  # OQ-13: both kept
    assert usd.customer.customer_kind == "individual" and usd.customer.entity == w.alm
    assert (items["INV-1003"].control_account, items["INV-1003"].counterparty) == ("1210", w.sub)
    assert (items["CN-0001"].doc_type, items["CN-0001"].signed_sar) == ("credit_note", D("-1000.00"))
    assert (items["B-77"].side, items["B-77"].control_account, items["B-77"].signed_sar) == ("payable", "2100",
                                                                                            D("-11500.00"))
    assert all(i.pre_go_live and i.source == "import" and i.cutover_date == CUT for i in items.values())
    assert migration_port.open_item_balances(w.alm) == {("1200", ""): D("23000.00"), ("1210", "SUB"): D("5000.00"),
                                                        ("2100", ""): D("-11500.00")}
    assert not JournalEntry.objects.exists() and not SalesDocument.objects.exists()  # no entry, never ZATCA (R115)
    again = subledgers.load_subledgers(w.alm, wb, ["receivables", "payables"], user=w.a, apply=True)
    assert again.applied and again.sections["receivables"].created == 0 and again.sections["receivables"].skipped == 7
    assert MigratedOpenItem.objects.count() == 5  # never loaded twice (R23)
    sub = subledgers.load_subledgers(w.sub, wb, ["payables"], user=w.s, apply=True)
    assert sub.applied and migration_port.open_item_balances(w.sub) == {("2110", "ALM"): D("-5000.00")}
    assert [i.legacy_number for i in migration_port.open_items(w.s)] == ["INV-1003"]  # scoped to SUB (R3)


def test_r23_a_changed_open_item_is_refused_and_nothing_is_loaded(w):
    subledgers.load_subledgers(w.alm, book(), ["receivables"], user=w.a, apply=True)
    rows = t.workbook_rows()
    find(rows, "open_receivables", document_no="INV-1001").update(amount=D("15000.50"), sar_amount=D("15000.50"))
    find(rows, "open_receivables", document_no="CN-0001").update(amount=D("1000.50"), sar_amount=D("1000.50"))
    plan = subledgers.load_subledgers(w.alm, book(rows), ["receivables"], user=w.a, apply=True)
    assert not plan.applied and not plan.report.errors  # still ties (15,000.50 - 1,000.50), but changed values
    assert any("INV-1001" in p and "other values" in p and "بقيم أخرى" in p for p in plan.problems)
    assert MigratedOpenItem.objects.get(legacy_number="INV-1001").amount == D("15000.000")


def test_r23_a_migrated_open_item_is_written_once():
    item = MigratedOpenItem()
    with pytest.raises(InvoicingError):
        item.save()
    with pytest.raises(InvoicingError):
        MigratedOpenItem.objects.all().update(description="x")
    with pytest.raises(InvoicingError):
        MigratedOpenItem.objects.all().delete()


def test_r23_open_item_rules_currency_decimals_dates_and_entity(w):
    customer, _ = migration_port.ensure_customer(w.alm, user=w.a, code="C9", name_ar="عميل", customer_kind="business",
                                                 currency="SAR", country="SA", group_entity=None)
    base = dict(user=w.a, side="receivable", party=customer, doc_type="invoice", legacy_number="X-1",
                document_date=date(2026, 12, 1), currency="USD", amount=D("100.00"), amount_sar=D("375.00"),
                cutover=CUT)
    for change, text in (({"amount": D("100.005")}, "decimals"), ({"amount_sar": D("375.001")}, "2 decimals"),
                         ({"currency": "SAR"}, "must equal"), ({"document_date": date(2027, 1, 1)}, "after the cut-over"),
                         ({"doc_type": "bill"}, "not a receivable"), ({"amount": D("0")}, "above zero")):
        with pytest.raises(InvoicingError) as exc:
            migration_port.load_open_item(w.alm, **{**base, **change})
        assert text in str(exc.value), change
    other, _ = migration_port.ensure_customer(w.sub, user=w.s, code="C9", name_ar="عميل", customer_kind="business",
                                              currency="SAR", country="SA", group_entity=None)
    with pytest.raises(InvoicingError) as exc:  # an ALM item for a SUB customer (rule 4)
        migration_port.load_open_item(w.alm, **{**base, "party": other})
    assert "another entity" in str(exc.value)
    with pytest.raises(AccessDenied):  # the loader's right in the entity (R24)
        migration_port.load_open_item(w.alm, **{**base, "user": w.s})
    item, created = migration_port.load_open_item(w.alm, **base)
    assert created and item.amount == D("100.000") and item.amount_sar == D("375.00")
    with pytest.raises(AccessDenied):  # the party right is checked for an existing party too
        migration_port.ensure_customer(w.alm, user=w.v, code="C9", name_ar="عميل", customer_kind="business",
                                       currency="SAR", country="SA", group_entity=None)


def test_r24_sections_need_the_users_own_rights_in_the_entity(w):
    plan = subledgers.load_subledgers(w.alm, book(), ["receivables"], user=w.v, apply=True)  # a viewer
    assert not plan.applied and plan.problems and not Customer.objects.exists()
    plan = subledgers.load_subledgers(w.alm, book(), ["payables"], user=w.s, apply=True)  # SUB only (no cascade)
    assert not plan.applied and not Vendor.objects.exists()


def test_workbook_errors_block_every_section(w):
    rows = t.workbook_rows()
    find(rows, "trial_balance", entity="ALM", code="2602")["credit"] = D("20000.01")
    plan = subledgers.load_subledgers(w.alm, book(rows), ["receivables"], user=w.a, apply=True)
    assert not plan.ok and "tb_unbalanced" in {p.code for p in plan.report.errors} and not plan.applied
    assert not Customer.objects.exists()


def test_banks_load_as_drafts_from_the_approved_map(w, mapped):
    wb = book()
    plan = subledgers.load_subledgers(w.alm, wb, ["banks"], user=w.a, apply=True)
    assert plan.applied, plan.problems
    account = BankAccount.objects.get(entity=w.alm)
    assert (account.code, account.status, account.gl_account.code, account.opening_statement_balance,
            account.opening_statement_date) == ("ALM-RJ-SAR", "draft", "1100", D("50500.00"), CUT)
    assert any("ALM-SNB-USD" in m and "OQ-13" in m for m in plan.sections["banks"].warnings)  # FX: set up apart
    plan = subledgers.load_subledgers(w.sub, wb, ["banks"], user=w.s, apply=True)
    assert not plan.applied and "approved account map" in plan.problems[0]  # SUB has no approved map yet


def test_employees_eosb_and_leave_openings(people):
    w, wb = people, book()
    plan = subledgers.load_subledgers(w.alm, wb, ["employees"], user=w.a, apply=True)
    assert not plan.applied  # the accountant holds no hr right
    plan = subledgers.load_subledgers(w.alm, wb, ["eosb"], user=w.po, apply=True)
    assert not plan.applied and "load the employees first" in plan.problems[0]
    assert subledgers.load_subledgers(w.alm, wb, ["employees", "leave"], user=w.h, apply=True).applied
    assert Employee.objects.filter(entity=w.alm, status="draft").count() == 2
    t001 = hr.find_current_employee(w.alm, "1999999911", user=w.h)
    opening = LeaveAdjustment.objects.get(employee=t001)
    assert (opening.kind, opening.status, opening.days, opening.date, opening.source) == ("opening", "draft",
                                                                                        D("10.00"), CUT, "import")
    assert subledgers.load_subledgers(w.alm, wb, ["eosb"], user=w.po, apply=True).applied
    rows = {e.employee.employee_code: e.amount for e in EosbProvisionEntry.objects.filter(kind="opening")}
    assert sorted(rows.values()) == [D("3000.00"), D("5000.00")]
    assert payroll.opening_eosb_balances(w.alm) == {("2500", ""): D("-8000.00")}
    again = subledgers.load_subledgers(w.alm, wb, ["employees", "leave", "eosb"], user=w.h, apply=True)
    assert not again.applied  # eosb needs the payroll right
    again = subledgers.load_subledgers(w.alm, wb, ["employees", "leave"], user=w.h, apply=True)
    assert again.applied and again.sections["employees"].created == 0 and again.sections["leave"].skipped == 1
    assert subledgers.load_subledgers(w.alm, wb, ["eosb"], user=w.po, apply=True).sections["eosb"].skipped == 2
    with pytest.raises(Exception) as exc:  # another amount for the same opening is refused
        payroll.record_opening_eosb_provision(t001, user=w.po, amount=D("5000.01"), on=CUT)
    assert getattr(exc.value, "code", "") == "eosb_opening_changed"


def test_w14_bai_ajel_contract_loads_sold_with_its_remaining_instalments(people):
    w = people
    subledgers.load_subledgers(w.alm, book(), ["employees"], user=w.h, apply=True)
    emp = hr.find_current_employee(w.alm, "1999999911", user=w.h)
    args = dict(user=w.po, legacy_number="BAJ-T-1", asset_description_ar="سيارة اختبار", sale_date=date(2026, 1, 10),
                sale_price=D("12000.00"), profit_amount=D("1200.00"), vat_amount=D("1800.00"), instalments=12,
                paid_to_date=D("4600.00"), next_period="2027-01")
    contract = payroll.load_migrated_deferred_sale(emp, **args)
    # receivable 13,800.00; paid 4,600.00 (4 instalments of 1,150.00); open 9,200.00; recognised 400.00
    planned = list(contract.installments.order_by("period"))
    assert (contract.status, contract.pre_go_live, contract.legacy_number, len(planned)) == ("sold", True, "BAJ-T-1", 8)
    assert [i.amount for i in planned] == [D("1150.00")] * 8 and planned[0].period == "2027-01"
    sale = DeferredSale.objects.get(source_id=contract.pk)
    assert (sale.pre_go_live, sale.receivable, sale.profit_amount, sale.vat_amount, sale.reference) == (
        True, D("9200.00"), D("800.00"), D("0"), "MIG-BAJ-T-1")
    assert sale.is_posted and sale.sales_document_id is None and sale.purchase_bill_id is None
    assert invoicing.migrated_bai_ajel_balances(w.alm) == {("1240", ""): D("9200.00"), ("1241", ""): D("-800.00")}
    # each later collection releases its share of the deferred profit (R112): one instalment, 800 x 1,150 / 9,200
    assert invoicing._profit_figures(sale, D("1150.00"), D("0")) == D("100.00")
    assert invoicing._profit_figures(sale, D("9200.00"), D("700.00")) == D("100.00")  # the last releases the rest
    assert payroll.load_migrated_deferred_sale(emp, **args).pk == contract.pk  # a re-run: the same contract
    with pytest.raises(Exception) as exc:
        payroll.load_migrated_deferred_sale(emp, **{**args, "paid_to_date": D("5750.00")})
    assert getattr(exc.value, "code", "") == "migrated_contract_changed"
    assert payroll.load_migrated_deferred_sale(emp, **{**args, "legacy_number": "BAJ-T-2",
                                                       "paid_to_date": D("13800.00")}) is None  # settled: nothing
    with pytest.raises(AccessDenied):  # the payroll right in the entity (R24)
        payroll.load_migrated_deferred_sale(emp, **{**args, "user": w.a, "legacy_number": "BAJ-T-3"})
    assert DeferredSaleContract.objects.filter(pre_go_live=True).count() == 1


def test_r22_the_opening_entry_posts_once_the_real_subledgers_tie_out(people):
    w, wb = people, book()
    t.approve_map(services.create_account_map(w.alm, user=w.a, rows=workbook.account_map_rows_for(wb, "ALM")),
                  approver=w.p)
    tb = workbook.trial_balance_for(wb, "ALM")
    assert services.load_opening_balances(w.alm, tb, user=w.a, cutover=CUT, apply=True).applied
    result = services.post_imported_entries(w.alm, user=w.p, kind="opening", apply=True)
    assert not result["posted"] and "no open items are loaded" in result["details"]["opening"][0]
    subledgers.load_subledgers(w.alm, wb, ["receivables", "payables"], user=w.a, apply=True)
    result = services.post_imported_entries(w.alm, user=w.p, kind="opening", apply=True)
    assert not result["posted"]
    assert result["details"]["opening"] == [
        "opening: Sub-ledger account 2500: opening entry -8000.00, sub-ledger loaded 0.00, difference -8000.00. / "
        "حساب السجل الفرعي 2500: القيد الافتتاحي -8000.00، المحمّل في السجل الفرعي 0.00، الفرق -8000.00."]
    subledgers.load_subledgers(w.alm, wb, ["employees"], user=w.h, apply=True)
    subledgers.load_subledgers(w.alm, wb, ["eosb"], user=w.po, apply=True)
    result = services.post_imported_entries(w.alm, user=w.p, kind="opening", apply=True)
    assert [e.reference for e in result["posted"]] == ["FALCON-OPENING"]
    assert all(r["difference"] == 0 for r in result["open_items"]["opening"])
    subledger = invoicing.reconcile_subledger(w.alm, CUT, user=w.p)  # invoicing R39: open items = 1200 / 1210 / 2100
    assert {(r["account"], r["counterparty"], r["currency"]): r["difference"] for r in subledger} == {
        ("1200", None, "SAR"): 0, ("1210", "SUB", "SAR"): 0, ("2100", None, "SAR"): 0}
    assert payroll._eosb_tie_out(w.alm, CUT) == 0  # payroll R25: provisions = 2500
    late = subledgers.load_subledgers(w.alm, wb, ["receivables", "eosb"], user=w.a, apply=True)
    assert not late.applied and all("is posted" in late.sections[s].problems[0] for s in ("receivables", "eosb"))
    assert ImportedEntry.objects.get(kind="opening").journal_entry.status == "posted"
