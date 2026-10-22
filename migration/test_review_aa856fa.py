"""Review of aa856fa (migration sub-ledger loaders): items 1 and 5 to 10, with the reviewer's repros turned into tests.
Synthetic workbooks (``migration.testing``), fictional names and numbers only; nothing external."""
from datetime import date
from decimal import Decimal

import pytest

from banking import services as banking
from banking.models import BankAccount
from core.access import AccessDenied
from core.access.testing import make_agent, member
from invoicing import migration_port
from invoicing import services as invoicing
from invoicing.models import InvoicingError, MigratedOpenItem
from ledger import services as ledger
from ledger.models import JournalEntry, LedgerError
from migration import services, subledgers, workbook
from migration import testing as t
from migration.conftest import CUTOVER, OPEN_ITEMS, map_rows, tb_file
from migration.models import ImportedEntry
from migration.test_engine_routing import governed  # noqa: F401  (fixture)
from migration.test_subledgers import book, find, people  # noqa: F401  (fixture)
from payroll import services as payroll

pytestmark = pytest.mark.django_db
D = Decimal
CUT = t.WB_CUTOVER


def _tie_out(w, wb):
    t.approve_map(services.create_account_map(w.alm, user=w.a, rows=workbook.account_map_rows_for(wb, "ALM")),
                  approver=w.p)
    services.load_opening_balances(w.alm, workbook.trial_balance_for(wb, "ALM"), user=w.a, cutover=CUT, apply=True)
    assert subledgers.load_subledgers(w.alm, wb, ["receivables", "payables"], user=w.a, apply=True).applied
    assert subledgers.load_subledgers(w.alm, wb, ["employees"], user=w.h, apply=True).applied
    assert subledgers.load_subledgers(w.alm, wb, ["eosb"], user=w.po, apply=True).applied
    t.load_fixed_assets(w.alm, wb, w.a)  # fixed-assets R41: the opening entry's 1520 / 1590 tie to the register


def _post_opening(w, wb):
    _tie_out(w, wb)
    assert services.post_imported_entries(w.alm, user=w.p, kind="opening", apply=True)["posted"]


def _late_customer(w):
    customer, _ = migration_port.ensure_customer(w.alm, user=w.a, code="LATE", name_ar="عميل متأخر",
                                                 customer_kind="business", currency="SAR", country="SA",
                                                 group_entity=None)
    return customer


# --- item 1: R25 in every R22-source write service, and the loader locks the opening entry ---------------------------

def test_item1_r25_a_direct_port_call_after_the_opening_is_posted_is_refused(people):
    w, wb = people, book()
    _post_opening(w, wb)
    customer = _late_customer(w)
    with pytest.raises(InvoicingError) as exc:  # the reviewer's repro: straight to the port, around the loader
        migration_port.load_open_item(
            w.alm, user=w.a, side="receivable", party=customer, doc_type="invoice", legacy_number="LATE-1",
            document_date=date(2026, 12, 1), currency="SAR", amount=D("999.00"), amount_sar=D("999.00"), cutover=CUT)
    assert exc.value.code == "opening_posted" and "R25" in str(exc.value) and "مرحّل" in str(exc.value)
    assert not MigratedOpenItem.objects.filter(legacy_number="LATE-1").exists()
    rows = invoicing.reconcile_subledger(w.alm, CUT, user=w.p)
    assert all(r["difference"] == 0 for r in rows)  # R39 still ties out
    from hr import services as hr

    t001 = hr.find_current_employee(w.alm, "1999999911", user=w.h)
    with pytest.raises(Exception) as exc:  # payroll's R22 sources refuse too
        payroll.record_opening_eosb_provision(t001, user=w.po, amount=D("5000.00"), on=CUT)
    assert getattr(exc.value, "code", "") == "opening_posted"


def test_item1_an_open_item_before_posting_still_loads(people):
    w = people
    customer = _late_customer(w)
    item, created = migration_port.load_open_item(
        w.alm, user=w.a, side="receivable", party=customer, doc_type="invoice", legacy_number="EARLY-1",
        document_date=date(2026, 12, 1), currency="SAR", amount=D("10.00"), amount_sar=D("10.00"), cutover=CUT)
    assert created and item.amount_sar == D("10.00")


def test_item1_agents_never_run_a_section(people, mapped):
    w = people
    agent = make_agent("hr_loader")
    with pytest.raises(AccessDenied):
        subledgers.load_subledgers(w.alm, book(), ["employees", "leave", "banks"], user=agent, apply=True)
    assert not BankAccount.objects.exists()


def test_item1_the_loader_takes_the_opening_lock_inside_its_transaction(people, monkeypatch):
    w, wb = people, book()
    seen = []
    real = ledger.migration_opening_posted

    def spy(entity, *, lock=False, booked=True):
        from django.db import connection

        seen.append((lock, len(connection.savepoint_ids) > base))  # inside an atomic block of its own
        return real(entity, lock=lock, booked=booked)

    from django.db import connection

    base = len(connection.savepoint_ids)  # the test's own transaction
    monkeypatch.setattr(ledger, "migration_opening_posted", spy)
    subledgers.load_subledgers(w.alm, wb, ["receivables"], user=w.a, apply=True)
    assert (True, True) in seen  # locked, in the load's own atomic block, before anything is written


# --- item 5: the post-check hook keeps the ledger's own refusal -----------------------------------------------------

def test_item5_a_ledger_refusal_keeps_its_code_when_the_import_gate_also_refuses(governed, mapped, open_items,
                                                                              monkeypatch):
    w = governed
    services.load_opening_balances(w.alm, tb_file(), user=w.a, cutover=CUTOVER, apply=True)
    entry = ImportedEntry.objects.get(kind="opening").journal_entry
    open_items["ALM"] = {**OPEN_ITEMS, ("1200", ""): OPEN_ITEMS[("1200", "")] - 1}  # the gate refuses
    real = ledger.validate_entry
    monkeypatch.setattr(ledger, "validate_entry", lambda e: real(e) + ["A ledger problem. / مشكلة في الدفتر."])
    from core.access import engine

    with pytest.raises(LedgerError) as exc:
        engine.decide(entry, w.p, permission="ledger.post_entry")
    assert getattr(exc.value, "code", None) != "import_not_ready" and "A ledger problem" in str(exc.value) and "1200" in str(exc.value)
    monkeypatch.setattr(ledger, "validate_entry", real)
    with pytest.raises(LedgerError) as exc:  # only the gate: its own code
        engine.decide(entry, w.p, permission="ledger.post_entry")
    assert exc.value.code == "import_not_ready"
    assert JournalEntry.objects.get(pk=entry.pk).status == "draft"


# --- item 6: a map is returned through the engine inbox only --------------------------------------------------------

def test_item6_return_account_map_outside_the_engine_is_refused(w):
    amap = services.create_account_map(w.alm, user=w.a, rows=map_rows())
    services.submit_account_map(amap, user=w.a)
    with pytest.raises(AccessDenied) as exc:
        services.return_account_map(amap, user=w.p, note="check")
    assert exc.value.code == "approval_request_required"
    returned = services.decide_account_map(amap, user=w.p, approve=False, note="check 1102")  # the inbox may
    assert returned.status == "draft"


# --- item 7: a re-run with changed bank data is refused ------------------------------------------------------------

def test_item7_a_rerun_with_changed_bank_values_is_refused_not_skipped(w, mapped):
    assert subledgers.load_subledgers(w.alm, book(), ["banks"], user=w.a, apply=True).applied
    again = subledgers.load_subledgers(w.alm, book(), ["banks"], user=w.a, apply=True)
    assert again.applied and again.sections["banks"].skipped == 1  # the same values: skipped
    rows = t.workbook_rows()
    find(rows, "bank_accounts", code="ALM-RJ-SAR")["statement_balance"] = D("50600.00")
    changed = subledgers.load_subledgers(w.alm, book(rows), ["banks"], user=w.a, apply=True)
    assert not changed.applied
    assert any("ALM-RJ-SAR" in p and "other values" in p and "بقيم أخرى" in p for p in changed.problems)
    assert BankAccount.objects.get(code="ALM-RJ-SAR").opening_statement_balance == D("50500.00")


# --- item 8: R22 per legacy system ---------------------------------------------------------------------------------

def test_item8_the_gate_reads_the_open_items_of_the_entrys_own_source_system(w):
    customer = _late_customer(w)
    for system, number in (("falcon", "F-1"), ("other", "O-1")):
        migration_port.load_open_item(
            w.alm, user=w.a, side="receivable", party=customer, doc_type="invoice", legacy_number=number,
            document_date=date(2025, 12, 1), currency="SAR", amount=D("100.00"), amount_sar=D("100.00"),
            cutover=CUTOVER, source_system=system)
    assert migration_port.open_item_balances(w.alm) == {("1200", ""): D("200.00")}
    assert migration_port.open_item_balances(w.alm, "falcon") == {("1200", ""): D("100.00")}
    assert services._open_item_balances(w.alm, "falcon")[("1200", "")] == D("100.00")
    assert services._open_item_balances(w.alm, "other")[("1200", "")] == D("100.00")


# --- item 9: bank drafts are approved before the opening entry posts ------------------------------------------------

def test_item9_a_bank_draft_on_the_opening_accounts_blocks_the_posting_until_approved(people):
    w, wb = people, book()
    _tie_out(w, wb)
    assert subledgers.load_subledgers(w.alm, wb, ["banks"], user=w.a, apply=True).applied
    result = services.post_imported_entries(w.alm, user=w.p, kind="opening", apply=True)
    assert not result["posted"]
    text = " ".join(result["details"]["opening"])
    assert "ALM-RJ-SAR" in text and "approve them in banking" in text and "اعتمدها" in text
    member(w.v, w.alm, "approver")  # a second person approves the bank account in banking (banking R7)
    banking.approve_bank_account(BankAccount.objects.get(code="ALM-RJ-SAR"), user=w.v)
    result = services.post_imported_entries(w.alm, user=w.p, kind="opening", apply=True)
    assert [e.reference for e in result["posted"]] == ["FALCON-OPENING"]


# --- item 10: create_deferred_sale never touches a migrated contract ------------------------------------------------

def test_item10_create_deferred_sale_refuses_a_migrated_contract(people):
    w = people
    subledgers.load_subledgers(w.alm, book(), ["employees"], user=w.h, apply=True)
    from hr import services as hr

    emp = hr.find_current_employee(w.alm, "1999999911", user=w.h)
    c = payroll.load_migrated_deferred_sale(
        emp, user=w.po, legacy_number="BAJ-R-1", asset_description_ar="سيارة", sale_date=date(2026, 1, 10),
        sale_price=D("1200.00"), profit_amount=D("120.00"), instalments=12, paid_to_date=D("100.00"),
        next_period="2027-01", cutover=CUT, consent_ref="SIGNED-R-1")
    with pytest.raises(InvoicingError) as exc:
        invoicing.create_deferred_sale(c, approver=w.po, employee=emp, purchase_bill=None, cost=c.cost,
                                       profit_amount=c.profit_amount, possession_date=date(2026, 1, 10),
                                       possession_evidence_ref="X", asset_description_ar="سيارة",
                                       asset_type="vehicle")
    assert exc.value.code == "migrated_contract"


def test_review_4986344_item3_a_direct_port_call_takes_the_opening_lock_in_its_own_transaction(w, monkeypatch):
    seen = []
    real = ledger.migration_opening_posted

    def spy(entity, *, lock=False, booked=True):
        from django.db import connection

        seen.append((lock, len(connection.savepoint_ids) > base))  # inside an atomic block of its own
        return real(entity, lock=lock, booked=booked)

    customer = _late_customer(w)
    from django.db import connection

    base = len(connection.savepoint_ids)
    monkeypatch.setattr(ledger, "migration_opening_posted", spy)
    migration_port.load_open_item(
        w.alm, user=w.a, side="receivable", party=customer, doc_type="invoice", legacy_number="LOCK-1",
        document_date=date(2025, 12, 1), currency="SAR", amount=D("1.00"), amount_sar=D("1.00"), cutover=CUTOVER)
    assert seen == [(True, True)]
