"""R129 (OQ-79; reviews of aa856fa item 11 and 4986344): migrated open items are settled after go-live by receipts,
payments, credit notes and other migrated items, partially or in full, as append-only allocation rows; a
foreign-currency item books its realised FX difference at settlement, approved in the engine
(``invoicing.migrated_settlement``); the sub-ledger keeps tying out to the ledger (R39).

Invoicing's anonymised world (``invoicing.conftest``) plus a real migration opening entry carrying the items, posted
through the migration services; fictional parties and numbers, invoicing's fake ZATCA, a fixed Riyadh clock."""
import importlib
import inspect
import threading
from datetime import date, datetime
from decimal import Decimal

import pytest
from django.db import connection, connections, transaction
from django.utils import timezone

from core.access import AccessDenied, engine, matrix
from core.access import services as access_services
from core.access.authority_models import ApprovalRequest
from core.access.models import AccessLog
from core.access.testing import make_user, member
from invoicing import migration_port
from invoicing import services as inv
from invoicing.conftest import final, invoice, usd_rate
from invoicing.models import (InvoicingError, MigratedOpenItem, MigratedSettlement, PayableAllocation,
                              ReceivableAllocation)
from ledger.models import JournalLine
from ledger.services_setup import create_monthly_periods
from migration import readers
from migration import services as migration
from migration import testing as t

pytestmark = pytest.mark.django_db
D = Decimal
CUT = date(2026, 12, 31)
DAY = datetime(2027, 1, 25, 10, 0, tzinfo=inv.RIYADH)
MAP = {"1201": ("عملاء", "1200"), "2101": ("موردون", "2100"), "2601": ("رأس المال", "3100")}
APPROVE = "invoicing.approve_migrated_settlement"
postgres_only = pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL triggers and locks")


def _migrate(w, items, tb):
    """Loads ``items`` [(name, side, party, type, number, currency, amount, sar)] and posts the opening entry."""
    create_monthly_periods(w.alm, 2026)
    load = dict(user=w.accountant, document_date=date(2026, 12, 1), cutover=CUT)
    for name, side, party, doc_type, number, currency, amount, sar in items:
        row, _ = migration_port.load_open_item(w.alm, side=side, party=party, doc_type=doc_type, legacy_number=number,
                                               currency=currency, amount=D(amount), amount_sar=D(sar), **load)
        setattr(w, name, row)
    amap = migration.create_account_map(w.alm, user=w.accountant, rows=[
        {"legacy_code": k, "legacy_name": n, "erp_code": e} for k, (n, e) in MAP.items()])
    t.approve_map(amap, approver=w.cfo)
    rows = t.tb_rows({k: (MAP[k][0], v, "0", "0") for k, v in tb.items()})
    parsed = readers.read_trial_balance(t.table_csv(t.TB_HEADER, rows), {"columns": t.TB_COLUMNS})
    assert migration.load_opening_balances(w.alm, parsed, user=w.accountant, cutover=CUT, apply=True).applied
    result = migration.post_imported_entries(w.alm, user=w.cfo, kind="opening", apply=True)
    assert result["posted"], result["details"]
    return w


@pytest.fixture
def m(w, monkeypatch):
    """ALM migrated at 2026-12-31: C001 invoice 1,000.00 and credit note 200.00 in SAR, an invoice of USD 1,000.00
    carried at SAR 3,750.00 and a credit note of USD 400.00 carried at SAR 1,520.00; V001's bill of 500.00 and a
    bill of USD 1,000.00 carried at SAR 3,750.00 and a vendor credit note of 100.00. The accountant loads, the CFO
    posts."""
    monkeypatch.setattr(inv, "_riyadh_now", lambda: DAY)
    c, v = w.customer, w.vendor
    return _migrate(w, [
        ("r1", "receivable", c, "invoice", "MIG-INV-1", "SAR", "1000.00", "1000.00"),
        ("r2", "receivable", c, "credit_note", "MIG-CN-1", "SAR", "200.00", "200.00"),
        ("r3", "receivable", c, "invoice", "MIG-USD-1", "USD", "1000.00", "3750.00"),
        ("r4", "receivable", c, "credit_note", "MIG-USD-CN", "USD", "400.00", "1520.00"),
        ("p1", "payable", v, "bill", "MIG-B-1", "SAR", "500.00", "500.00"),
        ("p2", "payable", v, "bill", "MIG-USD-B", "USD", "1000.00", "3750.00"),
        ("p3", "payable", v, "vendor_credit_note", "MIG-VCN-1", "SAR", "100.00", "100.00"),
    ], {"1201": "3030.00", "2101": "-4150.00", "2601": "1120.00"})  # 1000 - 200 + 3750 - 1520; -500 - 3750 + 100


def receipt(w, amount, currency="SAR", on=date(2027, 1, 20), user=None, customer=None, entity=None):
    r = inv.create_receipt(entity or w.alm, user=user or w.clerk, customer=customer or w.customer, date=on,
                           amount=amount, currency=currency)
    return inv.post_receipt(r, user=user or w.clerk)


def payment(w, amount, currency="SAR", on=date(2027, 1, 20)):
    pay = inv.create_vendor_payment(w.alm, user=w.ap, vendor=w.vendor, date=on, amount=amount, currency=currency)
    return inv.approve_vendor_payment(inv.submit_vendor_payment(pay, user=w.ap), user=w.cfo)


def ties(w):
    rows = inv.reconcile_subledger(w.alm, date(2027, 1, 31), user=w.accountant)
    assert all(r["difference"] == 0 for r in rows), rows
    return rows


def opened(w, item):
    return inv.migrated_open_amount(item, user=w.viewer)


def balance(w, code, currency=None):
    lines = JournalLine.objects.filter(entry__entity=w.alm, entry__status__in=["posted", "reversed"],
                                       account__code=code)
    if currency:
        lines = lines.filter(currency_id=currency)
    return sum((l.debit - l.credit for l in lines), D("0"))


def approve(w, row, user=None):
    """The engine inbox decides a pending settlement (access R61); its approval posts the entry."""
    engine.decide(row, user or w.cfo, permission=APPROVE)
    return MigratedSettlement.objects.get(pk=row.pk)


# --- SAR items: no GL ---------------------------------------------------------------------------------------------

def test_r129_a_sar_item_is_settled_partly_then_fully_by_receipts_and_credit_notes_without_gl(m):
    w = m
    ties(w)
    first = receipt(w, "600.00")
    row = inv.allocate_migrated_item(w.r1, first, "400.00", user=w.clerk)
    assert (row.status, row.journal_entry_id, row.item_sar, row.amount) == ("posted", None, D("400.00"), D("400.000"))
    assert opened(w, w.r1) == (D("600.00"), D("600.00"))
    assert inv.open_amount(first, user=w.viewer) == D("200.00")
    assert not ApprovalRequest.objects.filter(document_type="invoicing.migratedsettlement").exists()  # none in SAR
    with pytest.raises(InvoicingError) as exc:  # the receipt has only 200.00 left
        inv.allocate_migrated_item(w.r1, first, "300.00", user=w.clerk)
    assert exc.value.code == "allocation_invalid" and "more than is open" in str(exc.value)
    inv.allocate_migrated_item(w.r1, first, "200.00", user=w.clerk)
    inv.allocate_migrated_item(w.r1, w.r2, "200.00", user=w.clerk)  # the migrated credit note nets the rest
    assert opened(w, w.r1) == (D("200.00"), D("200.00")) and opened(w, w.r2) == (D("0.00"), D("0.00"))
    inv.allocate_migrated_item(w.r1, receipt(w, "200.00"), "200.00", user=w.clerk)
    assert opened(w, w.r1) == (D("0.00"), D("0.00"))
    ties(w)  # R39: no GL effect in SAR, the sub-ledger still equals 1200
    assert balance(w, "1200", "SAR") == D("3030.00") - D("800.00")  # the receipts credited 1200 (600 + 200)


def test_r129_p7_a_migrated_credit_note_credits_an_erp_invoice(m):
    w = m
    doc = final(w, invoice(w, lines=(("rent_commercial", "1", "100.00"),)))  # 115.00
    row = inv.allocate_migrated_item(w.r2, doc, "115.00", user=w.clerk)
    assert row.sales_document_id == doc.pk
    assert inv.open_amount(doc, user=w.viewer) == D("0.00")
    assert inv.payment_state(doc, user=w.viewer) == "credited"  # review of 4986344, item 4
    other = final(w, invoice(w))  # 1,150.00
    inv.allocate_migrated_item(w.r2, other, "85.00", user=w.clerk)
    assert inv.payment_state(other, user=w.viewer) == "partial"
    inv.allocate(receipt(w, "1065.00"), other, "1065.00", user=w.clerk)
    assert inv.payment_state(other, user=w.viewer) == "paid"
    ties(w)


def test_r129_unallocation_is_a_new_negative_row_and_blocks_reversal_while_live(m):
    w = m
    r = receipt(w, "300.00")
    row = inv.allocate_migrated_item(w.r1, r, "300.00", user=w.clerk)
    with pytest.raises(InvoicingError) as exc:  # R25 / R33: un-allocate first
        inv.reverse_document(r, user=w.accountant)
    assert exc.value.code == "has_allocations"
    from ledger import services as ledger

    grant = access_services.require(w.accountant, w.alm, "invoicing.reverse_document", document=r)
    with pytest.raises(Exception):  # item 7: not behind invoicing's back either (registered reverse_guard)
        ledger.reverse_entry(r.journal_entry, grant)
    undo = inv.unallocate_migrated_item(row, user=w.clerk)
    assert (undo.amount, undo.item_sar, undo.reverses_id) == (D("-300.000"), D("-300.00"), row.pk)
    assert opened(w, w.r1) == (D("1000.00"), D("1000.00"))
    with pytest.raises(InvoicingError) as exc:
        inv.unallocate_migrated_item(row, user=w.clerk)
    assert exc.value.code == "unallocate_invalid"
    assert inv.reverse_document(r, user=w.accountant).status == "reversed"
    ties(w)


def test_r129_a_migrated_bill_is_paid_by_a_vendor_payment(m):
    w = m
    pay = payment(w, "500.00")
    inv.allocate_migrated_item(w.p1, pay, "500.00", user=w.ap)
    assert opened(w, w.p1) == (D("0.00"), D("0.00")) and inv.open_amount(pay, user=w.viewer) == D("0.00")
    with pytest.raises(InvoicingError) as exc:
        inv.reverse_document(pay, user=w.accountant)
    assert exc.value.code == "has_allocations"
    ties(w)


# --- foreign items: the engine approves, the approval posts the FX difference -------------------------------------

def test_r129_a_foreign_receivable_books_the_realised_fx_difference_on_approval(m):
    w = m
    usd_rate(w, "3.80", on=date(2027, 1, 20))
    first = receipt(w, "600.00", currency="USD")  # Dr bank 2,280.00 / Cr 1200 USD 600.00 at 3.80
    row = inv.allocate_migrated_item(w.r3, first, "600.00", user=w.clerk)
    assert (row.status, row.item_sar, row.counter_sar, row.fx_difference) == ("pending", D("2250.00"), D("2280.00"),
                                                                              D("-30.00"))  # expected, a gain
    assert opened(w, w.r3) == (D("400.00"), D("1500.00"))  # reserved while pending
    req = ApprovalRequest.objects.get(document_type="invoicing.migratedsettlement", document_id=row.pk)
    assert (req.status, req.activity.code, req.rule_source, req.amount) == ("open", "invoicing.migrated_settlement",
                                                                            "default", D("2280.000"))
    for user in (w.clerk2, w.accountant):  # only the CFO's approval right posts it
        with pytest.raises(AccessDenied):
            inv.post_migrated_settlement(row, user=user)
    row = approve(w, row)
    assert row.status == "posted" and row.journal_entry.status == "posted" and row.journal_entry.date == DAY.date()
    assert (row.counter_sar, row.fx_difference) == (D("2280.00"), D("-30.00"))
    assert balance(w, "4700") == D("-30.00") and balance(w, "1200", "USD") == D("0")
    req.refresh_from_db()
    assert req.status == "approved"
    ties(w)
    usd_rate(w, "3.70", on=date(2027, 1, 22))
    second = receipt(w, "400.00", currency="USD", on=date(2027, 1, 22))  # 1,480.00
    last = approve(w, inv.allocate_migrated_item(w.r3, second, "400.00", user=w.clerk))
    assert (last.item_sar, last.counter_sar, last.fx_difference) == (D("1500.00"), D("1480.00"), D("20.00"))
    assert balance(w, "5750") == D("20.00") and opened(w, w.r3) == (D("0.00"), D("0.00"))
    ties(w)
    undo = inv.unallocate_migrated_item(last, user=w.accountant)  # its entry is reversed with it
    assert undo.journal_entry.reversal_of_id == last.journal_entry_id and balance(w, "5750") == D("0.00")
    assert opened(w, w.r3) == (D("400.00"), D("1500.00"))
    ties(w)


def test_r129_p1_the_preparer_never_approves_a_settlement(m):
    w = m
    member(w.clerk, w.alm, "approver")  # the clerk now also holds the approval right
    usd_rate(w, "3.80", on=date(2027, 1, 20))
    row = inv.allocate_migrated_item(w.r3, receipt(w, "100.00", currency="USD"), "100.00", user=w.clerk)
    with pytest.raises(AccessDenied) as exc:
        inv.post_migrated_settlement(row, user=w.clerk)
    assert exc.value.code == "self_approval"
    with pytest.raises(AccessDenied) as exc:
        engine.decide(row, w.clerk, permission=APPROVE)
    assert exc.value.code == "self_approval"
    assert MigratedSettlement.objects.get(pk=row.pk).status == "pending"


def test_r129_p2_a_foreign_payable_is_settled_by_a_foreign_vendor_payment(m):
    w = m
    usd_rate(w, "3.80", on=date(2027, 1, 20))
    pay = payment(w, "1000.00", currency="USD")  # Dr 2100 USD 1,000.00 at 3.80 = 3,800.00
    row = inv.allocate_migrated_item(w.p2, pay, "1000.00", user=w.ap)
    assert (row.status, row.item_sar, row.counter_sar, row.fx_difference) == ("pending", D("3750.00"), D("3800.00"),
                                                                              D("50.00"))  # paid more SAR: a loss
    row = approve(w, row)
    assert (row.counter_sar, row.fx_difference) == (D("3800.00"), D("50.00"))
    lines = {(l.account.code, l.currency_id, l.debit, l.credit) for l in row.journal_entry.lines.select_related("account")}
    assert lines == {("2100", "SAR", D("3750.00"), D("0")), ("2100", "USD", D("0"), D("3800.00")),
                     ("5750", "SAR", D("50.00"), D("0"))}
    assert balance(w, "2100", "USD") == D("0") and opened(w, w.p2) == (D("0.00"), D("0.00"))
    ties(w)


def test_r129_p5_two_foreign_migrated_items_net_their_sar_difference(m):
    w = m
    row = inv.allocate_migrated_item(w.r3, w.r4, "400.00", user=w.clerk)  # 1,500.00 against 1,520.00
    assert (row.status, row.item_sar, row.counter_sar, row.fx_difference) == ("pending", D("1500.00"), D("1520.00"),
                                                                              D("-20.00"))
    row = approve(w, row)
    lines = {(l.account.code, l.debit, l.credit) for l in row.journal_entry.lines.select_related("account")}
    assert lines == {("1200", D("0"), D("1500.00")), ("1200", D("1520.00"), D("0")), ("4700", D("0"), D("20.00"))}
    assert balance(w, "4700") == D("-20.00")
    assert opened(w, w.r3) == (D("600.00"), D("2250.00")) and opened(w, w.r4) == (D("0.00"), D("0.00"))
    ties(w)
    undo = inv.unallocate_migrated_item(row, user=w.accountant)
    assert (undo.item_sar, undo.counter_sar, undo.fx_difference) == (D("-1500.00"), D("-1520.00"), D("20.00"))
    assert balance(w, "4700") == D("0.00") and opened(w, w.r4) == (D("400.00"), D("1520.00"))
    ties(w)


def test_r129_p6_pro_rata_rounding_the_last_settlement_takes_the_rest(m):
    w = m
    usd_rate(w, "3.80", on=date(2027, 1, 20))
    rows = [approve(w, inv.allocate_migrated_item(w.r3, receipt(w, amount, currency="USD"), amount, user=w.clerk))
            for amount in ("333.33", "333.33", "333.34")]
    assert [r.item_sar for r in rows] == [D("1249.99"), D("1249.99"), D("1250.02")]  # 3,750.00 in all
    assert [r.counter_sar for r in rows] == [D("1266.65"), D("1266.65"), D("1266.69")]
    assert opened(w, w.r3) == (D("0.00"), D("0.00"))
    assert balance(w, "4700") == -sum((r.counter_sar - r.item_sar for r in rows), D("0"))
    ties(w)


def test_r129_a_pending_foreign_settlement_is_discarded_or_sent_back(m):
    w = m
    usd_rate(w, "3.80", on=date(2027, 1, 20))
    row = inv.allocate_migrated_item(w.r3, receipt(w, "100.00", currency="USD"), "100.00", user=w.clerk)
    assert inv.discard_migrated_settlement(row, user=w.clerk).status == "discarded"
    assert ApprovalRequest.objects.get(document_id=row.pk, document_type="invoicing.migratedsettlement").status \
        == "superseded"  # out of the inbox
    assert opened(w, w.r3) == (D("1000.00"), D("3750.00"))
    with pytest.raises(InvoicingError):
        inv.post_migrated_settlement(row, user=w.cfo)
    back = inv.allocate_migrated_item(w.r3, receipt(w, "100.00", currency="USD"), "100.00", user=w.clerk)
    engine.decide(back, w.cfo, approve=False, note="check the rate", permission=APPROVE)
    assert MigratedSettlement.objects.get(pk=back.pk).status == "discarded"
    assert opened(w, w.r3) == (D("1000.00"), D("3750.00"))


def test_r129_the_matrix_bands_route_a_settlement_by_its_sar_measure(m):
    w = m
    w.aa, w.mp = make_user("aa"), make_user("mp")
    member(w.aa, w.alm, "access_admin")
    member(w.mp, w.alm, "managing_partner")
    v = matrix.draft_version(w.alm, user=w.aa, effective_from=timezone.localdate(access_services.now()),
                             summary_en="Settlement bands", summary_ar="نطاقات التسويات", copy_current=False)
    matrix.add_rule(v, user=w.aa, activity="invoicing.migrated_settlement", approver_roles=["approver"],
                    amount_to="1000.00")
    matrix.add_rule(v, user=w.aa, activity="invoicing.migrated_settlement", approver_roles=["managing_partner"],
                    amount_from="1000.01")
    matrix.submit_version(v, user=w.aa)
    matrix.decide_version(v, user=w.mp, approve=True)
    usd_rate(w, "3.80", on=date(2027, 1, 20))
    small = inv.allocate_migrated_item(w.r3, receipt(w, "100.00", currency="USD"), "100.00", user=w.clerk)
    big = inv.allocate_migrated_item(w.r3, receipt(w, "500.00", currency="USD"), "500.00", user=w.clerk)
    assert inv.migrated_settlement_measure(small) == D("380.00")  # 375.00 cleared + 5.00 expected difference
    with pytest.raises(AccessDenied):  # 1,900.00: the managing partner's band
        approve(w, big)
    assert approve(w, small).status == "posted"
    assert approve(w, big, user=w.mp).status == "posted"
    assert ApprovalRequest.objects.get(document_id=big.pk, document_type="invoicing.migratedsettlement") \
        .rule_source == "matrix"
    ties(w)


# --- refusals ----------------------------------------------------------------------------------------------------

def _regroup(party, entity):
    """A party whose group entity changed while it had open items (legacy data, or before review 07da401 item 8):
    written past the service on purpose, to prove every settlement path still refuses to move the balance."""
    type(party).objects.filter(pk=party.pk).update(group_entity=entity)
    party.refresh_from_db()
    return party


def test_r129_item8_a_party_with_open_items_keeps_its_group_entity(m):
    w = m
    with pytest.raises(InvoicingError) as exc:  # MIG-INV-1 is open on 1200
        inv.update_customer(w.customer, user=w.clerk, group_entity=w.sub)
    assert exc.value.code == "party_has_open_items" and "سوّها" in str(exc.value)
    with pytest.raises(InvoicingError) as exc:
        inv.update_vendor(w.vendor, user=w.ap, group_entity=w.sub)
    assert exc.value.code == "party_has_open_items"
    other = inv.create_customer(w.alm, user=w.clerk, code="C-NEW", name_ar="عميل جديد", customer_kind="business",
                                country="SA")
    assert inv.update_customer(other, user=w.clerk, group_entity=w.sub).group_entity == w.sub  # nothing open


def test_r129_p3_a_settlement_never_moves_a_balance_between_control_accounts(m):
    w = m
    _regroup(w.customer, w.sub)  # C001 now a group entity: 1210 (SUB)
    r = receipt(w, "300.00")  # booked Cr 1210 (SUB)
    with pytest.raises(InvoicingError) as exc:  # the item sits on 1200
        inv.allocate_migrated_item(w.r1, r, "300.00", user=w.clerk)
    assert exc.value.code == "allocation_invalid" and "between control accounts" in str(exc.value)
    assert "حسابات المراقبة" in str(exc.value)
    ties(w)


def test_r129_erp_allocation_across_control_accounts_is_refused(w):
    old = final(w, invoice(w))  # Dr 1200
    _regroup(w.customer, w.sub)
    r = inv.post_receipt(inv.create_receipt(w.alm, user=w.clerk, customer=w.customer, date=date(2027, 1, 20),
                                            amount="100.00"), user=w.clerk)  # Cr 1210 (SUB)
    with pytest.raises(InvoicingError) as exc:
        inv.allocate(r, old, "100.00", user=w.clerk)
    assert "different control accounts" in str(exc.value)


def test_r129_q1_a_receipt_application_across_control_accounts_is_refused(w):
    old = final(w, invoice(w))  # Dr 1200
    _regroup(w.customer, w.sub)
    r = inv.create_receipt(w.alm, user=w.clerk, customer=w.customer, date=date(2027, 1, 20), amount="100.00")
    with pytest.raises(InvoicingError) as exc:  # would post Cr 1210 (SUB) and settle a 1200 invoice
        inv.add_receipt_application(r, old, "100.00", user=w.clerk)
    assert "different control accounts" in str(exc.value)
    assert not ReceivableAllocation.objects.exists()


def test_r129_a_payment_application_across_control_accounts_is_refused(w):
    from invoicing.test_purchases import bill

    old = inv.approve_purchase_document(bill(w), user=w.cfo)  # Cr 2100
    _regroup(w.vendor, w.sub)
    pay = inv.create_vendor_payment(w.alm, user=w.ap, vendor=w.vendor, date=date(2027, 1, 20), amount="100.00")
    with pytest.raises(InvoicingError) as exc:
        inv.add_payment_application(pay, old, "100.00", user=w.ap)
    assert "different control accounts" in str(exc.value)


def test_r129_r37_a_credit_note_on_another_control_account_is_not_auto_allocated(w):
    from invoicing.test_sales import note

    original = final(w, invoice(w, [("rent_commercial", "1", "10000.00")]))  # 11,500.00 on 1200
    from invoicing.conftest import approve as approve_sales

    issued = approve_sales(w, note(w, original))  # issued while C001 is not a group entity
    _regroup(w.customer, w.sub)  # then regrouped before ZATCA reports it: it posts on 1210 (SUB)
    cn = w.zatca.clear(issued) if issued.zatca_profile == "standard" else w.zatca.report(issued)
    assert not ReceivableAllocation.objects.filter(credit_note=cn).exists()
    assert inv.open_amount(original, user=w.clerk) == D("11500.00")
    assert inv.open_amount(cn, user=w.clerk) == D("4600.00")  # stays open on its own account


def test_r129_r37_a_vendor_credit_note_on_another_control_account_is_not_auto_allocated(w):
    from invoicing.test_purchases import bill

    original = inv.approve_purchase_document(bill(w), user=w.cfo)  # 1,150.00 on 2100
    _regroup(w.vendor, w.sub)
    vcn = inv.approve_purchase_document(
        bill(w, [("5300", "1", "400")], number="CN-1b", doc_type="vendor_credit_note", original_document=original),
        user=w.cfo)
    assert not PayableAllocation.objects.filter(vendor_credit_note=vcn).exists()
    assert inv.open_amount(original, user=w.ap) == D("1150.00")


def test_r129_q3_who_may_discard_and_the_audit_trail(m):
    w = m
    usd_rate(w, "3.80", on=date(2027, 1, 20))
    row = inv.allocate_migrated_item(w.r3, receipt(w, "100.00", currency="USD"), "100.00", user=w.clerk)
    for user in (w.clerk2, w.sub_clerk):  # another clerk, another entity: neither preparer, approver nor admin
        with pytest.raises(AccessDenied):
            inv.discard_migrated_settlement(row, user=user)
    assert MigratedSettlement.objects.get(pk=row.pk).status == "pending"
    row = inv.discard_migrated_settlement(row, user=w.cfo)  # an approver of settlements
    assert (row.status, row.discarded_by, row.discarded_at is not None) == ("discarded", w.cfo, True)
    req = ApprovalRequest.objects.get(document_id=row.pk, document_type="invoicing.migratedsettlement")
    assert req.status == "superseded"
    assert AccessLog.objects.filter(event="approval_withdrawn", actor=w.cfo, details__request=req.pk).exists()
    own = inv.allocate_migrated_item(w.r3, receipt(w, "50.00", currency="USD"), "50.00", user=w.clerk)
    assert inv.discard_migrated_settlement(own, user=w.clerk).discarded_by == w.clerk  # the preparer


def test_r129_q7_splitting_a_settlement_never_lowers_its_band(m):
    w = m
    w.aa, w.mp = make_user("aa"), make_user("mp")
    member(w.aa, w.alm, "access_admin")
    member(w.mp, w.alm, "managing_partner")
    v = matrix.draft_version(w.alm, user=w.aa, effective_from=timezone.localdate(access_services.now()),
                             summary_en="Settlement bands", summary_ar="نطاقات التسويات", copy_current=False)
    matrix.add_rule(v, user=w.aa, activity="invoicing.migrated_settlement", approver_roles=["approver"],
                    amount_to="1000.00")
    matrix.add_rule(v, user=w.aa, activity="invoicing.migrated_settlement", approver_roles=["managing_partner"],
                    amount_from="1000.01")
    matrix.submit_version(v, user=w.aa)
    matrix.decide_version(v, user=w.mp, approve=True)
    usd_rate(w, "3.80", on=date(2027, 1, 20))
    r = receipt(w, "1000.00", currency="USD")
    first = inv.allocate_migrated_item(w.r3, r, "250.00", user=w.clerk)  # 937.50 + 12.50 = 950.00: the CFO
    with pytest.raises(InvoicingError) as exc:  # one pending settlement per pair
        inv.allocate_migrated_item(w.r3, r, "250.00", user=w.clerk)
    assert exc.value.code == "settlement_pending"
    assert approve(w, first).status == "posted"
    for _ in range(3):  # each further split is measured with what the pair already settled: above 1,000.00
        row = inv.allocate_migrated_item(w.r3, r, "250.00", user=w.clerk)
        assert inv.migrated_settlement_measure(row) > D("1000.00")
        with pytest.raises(AccessDenied):
            approve(w, row)
        assert approve(w, row, user=w.mp).status == "posted"
    assert opened(w, w.r3) == (D("0.00"), D("0.00"))
    req = ApprovalRequest.objects.filter(document_type="invoicing.migratedsettlement").order_by("pk").last()
    assert "250.000" in req.document_version  # the version is what it settles, not just its key
    ties(w)


def test_r129_tax_c15_counts_effective_settlements_only(m):
    """Review of 07da401, item 6: the 12-month unpaid-bill list counts a migrated vendor credit note settled against
    the bill once the settlement is effective by the period end."""
    from types import SimpleNamespace

    from invoicing.test_purchases import bill
    from tax import services as tax

    w = m
    doc = inv.approve_purchase_document(bill(w), user=w.cfo)  # 1,150.00, dated 2027-01-15
    ret = SimpleNamespace(entity=w.alm, period_start=date(2028, 1, 1), period_end=date(2028, 1, 31))
    assert [f["document"] for f in tax._unpaid_bills(ret, {})] == [doc.pk]
    inv.allocate_migrated_item(w.p3, doc, "100.00", user=w.ap)  # the migrated vendor credit note, effective
    inv.allocate(payment(w, "1050.00"), doc, "1050.00", user=w.ap)
    assert tax._unpaid_bills(ret, {}) == []  # 1,050.00 paid + 100.00 settled


def test_r129_p4_refusals(m):
    w = m
    r = receipt(w, "600.00")
    inv.allocate_migrated_item(w.r1, r, "600.00", user=w.clerk)
    doc = final(w, invoice(w))
    with pytest.raises(InvoicingError):  # the receipt is used up by the migrated item
        inv.allocate(r, doc, "1.00", user=w.clerk)
    with pytest.raises(InvoicingError) as exc:
        inv.allocate_migrated_item(w.r1, receipt(w, "1.00"), "0.005", user=w.clerk)
    assert exc.value.code == "money_decimals"
    with pytest.raises(InvoicingError) as exc:
        inv.allocate_migrated_item(w.r1, receipt(w, "1.00"), "-1.00", user=w.clerk)
    assert exc.value.code == "money_sign"
    inv.allocate_migrated_item(w.r1, w.r2, "200.00", user=w.clerk)
    with pytest.raises(InvoicingError) as exc:  # the credit note is used up
        inv.allocate_migrated_item(w.r2, doc, "1.00", user=w.clerk)
    assert "more than is open" in str(exc.value)
    small = receipt(w, "100.00")
    cases = (
        (lambda: inv.allocate_migrated_item(w.r1, w.r1, "1.00", user=w.clerk), "itself"),
        (lambda: inv.allocate_migrated_item(w.r1, doc, "1.00", user=w.clerk), "same side"),
        (lambda: inv.allocate_migrated_item(w.r3, small, "1.00", user=w.clerk), "USD"),
        (lambda: inv.allocate_migrated_item(w.p1, small, "1.00", user=w.clerk), "receipts and sales documents"),
        (lambda: inv.allocate_migrated_item(w.r1, small, "100.01", user=w.clerk), "more than is open"),
    )
    for call, text in cases:
        with pytest.raises(InvoicingError) as exc:
            call()
        assert text in str(exc.value), text
    with pytest.raises(AccessDenied):
        inv.allocate_migrated_item(w.r1, small, "1.00", user=w.viewer)
    other = inv.create_customer(w.alm, user=w.clerk, code="C002", name_ar="عميل آخر", customer_kind="business",
                                country="SA")
    with pytest.raises(InvoicingError) as exc:
        inv.allocate_migrated_item(w.r1, receipt(w, "10.00", customer=other), "1.00", user=w.clerk)
    assert "different parties" in str(exc.value)
    sub_customer = inv.create_customer(w.sub, user=w.sub_clerk, code="C9", name_ar="عميل", customer_kind="business",
                                       country="SA")
    sub_receipt = receipt(w, "5.00", user=w.sub_clerk, customer=sub_customer, entity=w.sub)
    with pytest.raises(InvoicingError) as exc:  # rule 4: never across entities
        inv.allocate_migrated_item(w.r1, sub_receipt, "1.00", user=w.clerk)
    assert "different entities" in str(exc.value)
    ties(w)


def test_r129_settlement_rows_are_written_once(m):
    w = m
    row = inv.allocate_migrated_item(w.r1, receipt(w, "10.00"), "10.00", user=w.clerk)
    for call in (lambda: MigratedSettlement.objects.all().update(amount=1),
                 lambda: MigratedSettlement.objects.all().delete(), row.delete):
        with pytest.raises(InvoicingError):
            call()
    row.amount = D("5.00")
    row._service_write = True
    with pytest.raises(InvoicingError):  # an effective row never changes
        row.save()


def test_r129_nothing_is_settled_before_the_opening_entry_posts(w, monkeypatch):
    monkeypatch.setattr(inv, "_riyadh_now", lambda: DAY)
    create_monthly_periods(w.alm, 2026)
    item, _ = migration_port.load_open_item(w.alm, user=w.accountant, side="receivable", party=w.customer,
                                            doc_type="invoice", legacy_number="MIG-X", currency="SAR",
                                            amount=D("10.00"), amount_sar=D("10.00"), document_date=date(2026, 12, 1),
                                            cutover=CUT)
    with pytest.raises(InvoicingError) as exc:
        inv.allocate_migrated_item(item, receipt(w, "10.00"), "10.00", user=w.clerk)
    assert "not posted" in str(exc.value)
    assert MigratedOpenItem.objects.count() == 1 and not MigratedSettlement.objects.exists()


def test_r129_nothing_is_settled_once_the_opening_entry_is_reversed(m):
    """Review of 4986344, item 5: posted, not reversed."""
    from ledger import services as ledger
    from migration.models import ImportedEntry

    w = m
    entry = ImportedEntry.objects.get(kind="opening").journal_entry
    grant = access_services.require(w.accountant, w.alm, "ledger.reverse_entry", document=entry)
    ledger.reverse_entry(entry, grant)
    with pytest.raises(InvoicingError) as exc:
        inv.allocate_migrated_item(w.r1, receipt(w, "10.00"), "10.00", user=w.clerk)
    assert "reversed" in str(exc.value)


def test_r129_seed_v20_and_its_frozen_migration():
    """core.0035 applies ROLE_SEED_V20 from its own frozen copy: invoicing.approve_migrated_settlement for the
    roles that approve vendor payments; the activity's code default is the approver (CFO)."""
    from core.access import authority_defaults, catalogue
    from core.access.seed import ROLE_SEED, ROLE_SEED_V19, ROLE_SEED_V20

    assert ROLE_SEED is ROLE_SEED_V20
    assert catalogue.CATALOGUE[APPROVE].is_approval and catalogue.CATALOGUE[APPROVE].posts
    holders = {code for code, row in ROLE_SEED_V20.items() if APPROVE in row[5]}
    assert holders == {"approver", "managing_partner", "ceo"}
    assert authority_defaults.get("invoicing.migrated_settlement").default.roles == ("approver",)
    mod = importlib.import_module("core.migrations.0035_seed_access_v20_migrated_settlement")
    assert "import ROLE_SEED" not in inspect.getsource(mod)
    for code, row in ROLE_SEED_V20.items():
        expected = (*row[:5], tuple(sorted(set(row[5]))))
        if code in mod.ROLES_CHANGED:
            assert mod.ROLES_CHANGED[code] == expected, code
            old = ROLE_SEED_V19[code]
            assert mod.ROLES_V19[code] == (*old[:5], tuple(sorted(set(old[5])))), code
        else:
            assert (*ROLE_SEED_V19[code][:5], tuple(sorted(set(ROLE_SEED_V19[code][5])))) == expected, code


# --- PostgreSQL ----------------------------------------------------------------------------------------------------

@postgres_only
def test_r129_the_database_refuses_changes_to_settlement_rows(m):
    w = m
    row = inv.allocate_migrated_item(w.r1, receipt(w, "10.00"), "10.00", user=w.clerk)
    for sql in ("UPDATE invoicing_migratedsettlement SET amount = 1 WHERE id = %s",
                "UPDATE invoicing_migratedsettlement SET status = 'discarded' WHERE id = %s",
                "DELETE FROM invoicing_migratedsettlement WHERE id = %s"):
        with pytest.raises(Exception) as exc, transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(sql, [row.pk])
        assert "R129" in str(exc.value), sql
    usd_rate(w, "3.80", on=date(2027, 1, 20))
    pending = inv.allocate_migrated_item(w.r3, receipt(w, "10.00", currency="USD"), "10.00", user=w.clerk)
    with pytest.raises(Exception), transaction.atomic(), connection.cursor() as cursor:  # pending: amounts frozen too
        cursor.execute("UPDATE invoicing_migratedsettlement SET amount = 1 WHERE id = %s", [pending.pk])


@pytest.fixture
def seeded(transactional_db):
    """A transactional test starts from a flushed database: the seeds the migrations would have written."""
    from django.apps import apps
    from django.contrib.auth import get_user_model

    from core.access import authority_defaults
    from core.access.authority_models import ApprovalActivity
    from core.access.models import Role, RolePermission, UserProfile
    from core.access.seed import apply_seed, apply_system_users
    from core.currency_seed import apply_currency_seed
    from core.models import Currency
    from invoicing import reference_seed

    apply_seed(Role, RolePermission)
    apply_currency_seed(Currency)
    apply_system_users(get_user_model(), UserProfile)
    reference_seed.apply_all(apps)
    authority_defaults.sync(ApprovalActivity)


@postgres_only
@pytest.mark.django_db(transaction=True)
def test_r129_concurrent_settlements_never_use_one_receipt_twice(seeded, m):
    w = m
    r = receipt(w, "600.00")
    result = {}

    def other():
        try:
            result["row"] = inv.allocate_migrated_item(w.r1, r, "600.00", user=w.clerk2).pk
        except Exception as exc:  # noqa: BLE001 - reported in the assertion
            result["error"] = getattr(exc, "code", repr(exc))
        finally:
            connections.close_all()

    with transaction.atomic():
        inv.allocate_migrated_item(w.r1, r, "600.00", user=w.clerk)  # holds the item and receipt rows
        thread = threading.Thread(target=other)
        thread.start()
        thread.join(timeout=1.5)
        assert thread.is_alive(), "the second settlement waits for the row locks"
    thread.join(timeout=10)
    assert result == {"error": "allocation_invalid"}, result
    assert MigratedSettlement.objects.filter(receipt=r).count() == 1
