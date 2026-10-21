"""R129 (OQ-79; review of aa856fa, item 11): migrated open items are settled after go-live by receipts, payments,
credit notes and other migrated items, partially or in full, as append-only allocation rows; a foreign-currency item
books its realised FX difference at settlement; the sub-ledger keeps tying out to the ledger (R39).

Invoicing's anonymised world (``invoicing.conftest``) plus a real migration opening entry carrying the items, posted
through the migration services; fictional parties and numbers, invoicing's fake ZATCA, a fixed Riyadh clock."""
from datetime import date, datetime
from decimal import Decimal

import pytest

from core.access import AccessDenied
from invoicing import migration_port
from invoicing import services as inv
from invoicing.conftest import final, invoice, usd_rate
from invoicing.models import InvoicingError, MigratedOpenItem, MigratedSettlement
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
TB = {"1201": "4550.00", "2101": "-500.00", "2601": "-4050.00"}


@pytest.fixture
def m(w, monkeypatch):
    """ALM migrated at 2026-12-31: C001 invoice 1,000.00 and credit note 200.00 in SAR, an invoice of USD 1,000.00
    carried at SAR 3,750.00, and V001's bill of 500.00; the opening entry posted (accountant loads, CFO posts)."""
    monkeypatch.setattr(inv, "_riyadh_now", lambda: DAY)
    create_monthly_periods(w.alm, 2026)
    load = dict(user=w.accountant, document_date=date(2026, 12, 1), cutover=CUT)
    w.r1, _ = migration_port.load_open_item(w.alm, side="receivable", party=w.customer, doc_type="invoice",
                                            legacy_number="MIG-INV-1", currency="SAR", amount=D("1000.00"),
                                            amount_sar=D("1000.00"), **load)
    w.r2, _ = migration_port.load_open_item(w.alm, side="receivable", party=w.customer, doc_type="credit_note",
                                            legacy_number="MIG-CN-1", currency="SAR", amount=D("200.00"),
                                            amount_sar=D("200.00"), **load)
    w.r3, _ = migration_port.load_open_item(w.alm, side="receivable", party=w.customer, doc_type="invoice",
                                            legacy_number="MIG-USD-1", currency="USD", amount=D("1000.00"),
                                            amount_sar=D("3750.00"), **load)
    w.p1, _ = migration_port.load_open_item(w.alm, side="payable", party=w.vendor, doc_type="bill",
                                            legacy_number="MIG-B-1", currency="SAR", amount=D("500.00"),
                                            amount_sar=D("500.00"), **load)
    amap = migration.create_account_map(w.alm, user=w.accountant, rows=[
        {"legacy_code": k, "legacy_name": n, "erp_code": e} for k, (n, e) in MAP.items()])
    t.approve_map(amap, approver=w.cfo)
    rows = t.tb_rows({k: (MAP[k][0], v, "0", "0") for k, v in TB.items()})
    tb = readers.read_trial_balance(t.table_csv(t.TB_HEADER, rows), {"columns": t.TB_COLUMNS})
    assert migration.load_opening_balances(w.alm, tb, user=w.accountant, cutover=CUT, apply=True).applied
    result = migration.post_imported_entries(w.alm, user=w.cfo, kind="opening", apply=True)
    assert result["posted"], result["details"]
    return w


def receipt(w, amount, currency="SAR", on=date(2027, 1, 20), user=None):
    r = inv.create_receipt(w.alm, user=user or w.clerk, customer=w.customer, date=on, amount=amount, currency=currency)
    return inv.post_receipt(r, user=user or w.clerk)


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


def test_r116_a_sar_item_is_settled_partly_then_fully_by_receipts_and_credit_notes_without_gl(m):
    w = m
    ties(w)
    first = receipt(w, "600.00")
    row = inv.allocate_migrated_item(w.r1, first, "400.00", user=w.clerk)
    assert (row.status, row.journal_entry_id, row.item_sar, row.amount) == ("posted", None, D("400.00"), D("400.000"))
    assert opened(w, w.r1) == (D("600.00"), D("600.00"))
    assert inv.open_amount(first, user=w.viewer) == D("200.00")
    with pytest.raises(InvoicingError) as exc:  # the receipt has only 200.00 left
        inv.allocate_migrated_item(w.r1, first, "300.00", user=w.clerk)
    assert exc.value.code == "allocation_invalid" and "more than is open" in str(exc.value)
    inv.allocate_migrated_item(w.r1, first, "200.00", user=w.clerk)
    inv.allocate_migrated_item(w.r1, w.r2, "200.00", user=w.clerk)  # the migrated credit note nets the rest
    assert opened(w, w.r1) == (D("200.00"), D("200.00")) and opened(w, w.r2) == (D("0.00"), D("0.00"))
    inv.allocate_migrated_item(w.r1, receipt(w, "200.00"), "200.00", user=w.clerk)
    assert opened(w, w.r1) == (D("0.00"), D("0.00"))
    ties(w)  # R39: no GL effect in SAR, the sub-ledger still equals 1200
    assert balance(w, "1200") == D("4550.00") - D("800.00")  # the receipts credited 1200 (600 + 200)


def test_r116_a_migrated_credit_note_nets_an_erp_invoice(m):
    w = m
    doc = final(w, invoice(w))  # 1,000.00 + VAT 150.00
    row = inv.allocate_migrated_item(w.r2, doc, "200.00", user=w.clerk)
    assert row.sales_document_id == doc.pk
    assert inv.open_amount(doc, user=w.viewer) == D("950.00")
    assert inv.payment_state(doc, user=w.viewer) == "partial"
    r = receipt(w, "950.00")
    inv.allocate(r, doc, "950.00", user=w.clerk)
    assert inv.payment_state(doc, user=w.viewer) == "paid"
    ties(w)


def test_r116_unallocation_is_a_new_negative_row_and_blocks_reversal_while_live(m):
    w = m
    r = receipt(w, "300.00")
    row = inv.allocate_migrated_item(w.r1, r, "300.00", user=w.clerk)
    with pytest.raises(InvoicingError) as exc:  # R25 / R33: un-allocate first
        inv.reverse_document(r, user=w.accountant)
    assert exc.value.code == "has_allocations"
    undo = inv.unallocate_migrated_item(row, user=w.clerk)
    assert (undo.amount, undo.item_sar, undo.reverses_id) == (D("-300.000"), D("-300.00"), row.pk)
    assert opened(w, w.r1) == (D("1000.00"), D("1000.00"))
    with pytest.raises(InvoicingError) as exc:
        inv.unallocate_migrated_item(row, user=w.clerk)
    assert exc.value.code == "unallocate_invalid"
    assert inv.reverse_document(r, user=w.accountant).status == "reversed"
    ties(w)


def test_r116_a_foreign_item_books_the_realised_fx_difference_when_settled(m):
    w = m
    usd_rate(w, "3.80", on=date(2027, 1, 20))
    first = receipt(w, "600.00", currency="USD")  # Dr bank 2,280.00 / Cr 1200 USD 600.00 at 3.80
    row = inv.allocate_migrated_item(w.r3, first, "600.00", user=w.clerk)
    assert (row.status, row.item_sar) == ("pending", D("2250.00"))  # 3,750.00 x 600 / 1,000
    assert opened(w, w.r3) == (D("400.00"), D("1500.00"))  # reserved while pending
    with pytest.raises(Exception):  # the creator never posts it (ledger R35)
        inv.post_migrated_settlement(row, user=w.clerk)
    with pytest.raises(AccessDenied):  # the side's posting right: invoicing.post_receipt
        inv.post_migrated_settlement(row, user=w.ap)
    row = inv.post_migrated_settlement(row, user=w.clerk2)
    assert row.status == "posted" and row.journal_entry.status == "posted"
    assert (row.counter_sar, row.fx_difference) == (D("2280.00"), D("-30.00"))  # a gain: received more SAR
    assert balance(w, "4700") == D("-30.00") and balance(w, "1200", "USD") == D("0")
    ties(w)
    usd_rate(w, "3.70", on=date(2027, 1, 22))
    second = receipt(w, "400.00", currency="USD", on=date(2027, 1, 22))  # 1,480.00
    last = inv.post_migrated_settlement(inv.allocate_migrated_item(w.r3, second, "400.00", user=w.clerk),
                                        user=w.clerk2)
    assert (last.item_sar, last.counter_sar, last.fx_difference) == (D("1500.00"), D("1480.00"), D("20.00"))
    assert balance(w, "5750") == D("20.00") and opened(w, w.r3) == (D("0.00"), D("0.00"))
    assert balance(w, "1200") == D("4550.00") - D("3750.00")  # the item cleared at its carrying amount
    ties(w)
    undo = inv.unallocate_migrated_item(last, user=w.accountant)  # its entry is reversed with it
    assert undo.journal_entry.reversal_of_id == last.journal_entry_id and balance(w, "5750") == D("0.00")
    assert opened(w, w.r3) == (D("400.00"), D("1500.00"))
    ties(w)


def test_r116_a_pending_foreign_settlement_may_be_discarded(m):
    w = m
    usd_rate(w, "3.80", on=date(2027, 1, 20))
    row = inv.allocate_migrated_item(w.r3, receipt(w, "100.00", currency="USD"), "100.00", user=w.clerk)
    assert inv.discard_migrated_settlement(row, user=w.clerk).status == "discarded"
    assert opened(w, w.r3) == (D("1000.00"), D("3750.00"))
    with pytest.raises(InvoicingError):
        inv.post_migrated_settlement(row, user=w.clerk2)


def test_r116_a_migrated_bill_is_paid_by_a_vendor_payment(m):
    w = m
    pay = inv.create_vendor_payment(w.alm, user=w.ap, vendor=w.vendor, date=date(2027, 1, 20), amount="500.00")
    pay = inv.approve_vendor_payment(inv.submit_vendor_payment(pay, user=w.ap), user=w.cfo)
    inv.allocate_migrated_item(w.p1, pay, "500.00", user=w.ap)
    assert opened(w, w.p1) == (D("0.00"), D("0.00")) and inv.open_amount(pay, user=w.viewer) == D("0.00")
    ties(w)


def test_r116_refusals(m):
    w = m
    r = receipt(w, "100.00")
    cases = (
        (lambda: inv.allocate_migrated_item(w.r1, w.r1, "1.00", user=w.clerk), "itself"),
        (lambda: inv.allocate_migrated_item(w.r1, final(w, invoice(w)), "1.00", user=w.clerk), "same side"),
        (lambda: inv.allocate_migrated_item(w.r3, r, "1.00", user=w.clerk), "USD"),
        (lambda: inv.allocate_migrated_item(w.p1, r, "1.00", user=w.clerk), "receipts and sales documents"),
        (lambda: inv.allocate_migrated_item(w.r1, r, "100.01", user=w.clerk), "more than is open"),
    )
    for call, text in cases:
        with pytest.raises(InvoicingError) as exc:
            call()
        assert text in str(exc.value), text
    with pytest.raises(AccessDenied):
        inv.allocate_migrated_item(w.r1, r, "1.00", user=w.viewer)
    other = inv.create_customer(w.alm, user=w.clerk, code="C002", name_ar="عميل آخر", customer_kind="business",
                                country="SA")
    r2 = inv.post_receipt(inv.create_receipt(w.alm, user=w.clerk, customer=other, date=date(2027, 1, 20),
                                             amount="10.00"), user=w.clerk)
    with pytest.raises(InvoicingError) as exc:
        inv.allocate_migrated_item(w.r1, r2, "1.00", user=w.clerk)
    assert "different parties" in str(exc.value)
    assert not MigratedSettlement.objects.exists()


def test_r116_settlement_rows_are_written_once(m):
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


def test_r116_nothing_is_settled_before_the_opening_entry_posts(w, monkeypatch):
    monkeypatch.setattr(inv, "_riyadh_now", lambda: DAY)
    create_monthly_periods(w.alm, 2026)
    item, _ = migration_port.load_open_item(w.alm, user=w.accountant, side="receivable", party=w.customer,
                                            doc_type="invoice", legacy_number="MIG-X", currency="SAR",
                                            amount=D("10.00"), amount_sar=D("10.00"), document_date=date(2026, 12, 1),
                                            cutover=CUT)
    with pytest.raises(InvoicingError) as exc:
        inv.allocate_migrated_item(item, receipt(w, "10.00"), "10.00", user=w.clerk)
    assert "not posted yet" in str(exc.value)
    assert MigratedOpenItem.objects.count() == 1 and not MigratedSettlement.objects.exists()
