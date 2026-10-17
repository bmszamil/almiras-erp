"""Receipts, payments, allocation and tie-out (invoicing.md R33 to R39, R59; AT-28 to AT-31, AT-97;
ledger-fx R18 settlement in a foreign currency)."""
from datetime import date

import pytest

from core import access
from invoicing import services
from invoicing.conftest import ADDRESS, D, final, invoice, usd_rate
from invoicing.models import InvoicingError, ReceivableAllocation
from invoicing.test_purchases import bill
from ledger import services as ledger
from ledger.models import JournalEntry

pytestmark = pytest.mark.django_db


def receipt(w, amount="1150.00", *, applications=(), user=None, **fields):
    user = user or w.clerk
    r = services.create_receipt(w.alm, user=user, customer=fields.pop("customer", w.customer), date=fields.pop(
        "date", date(2027, 1, 20)), amount=amount, **fields)
    for target, value in applications:
        services.add_receipt_application(r, target, value, user=user)
    return r


def je(doc):
    doc.refresh_from_db()
    return sorted((l.account.code, l.debit, l.credit, l.line_kind) for l in doc.journal_entry.lines.select_related("account"))


def test_at97_receipt_is_posted_by_its_recorder_refund_by_a_second_person(w):
    """D26 / access R46 (replaces AT-70): the clerk who records a receipt posts it; a refund keeps segregation."""
    for segregation in (True, False):
        services.update_settings(w.alm, user=w.cfo, segregation_required=segregation)
        inv = final(w, invoice(w))
        r = receipt(w, applications=[(inv, "1150.00")])
        posted = services.post_receipt(r, user=w.clerk)  # K records R, K posts R
        assert (posted.status, posted.posted_by) == ("posted", w.clerk)
        assert je(posted) == [("1100", D("1150.00"), D("0"), "normal"), ("1200", D("0"), D("1150.00"), "normal")]
        assert posted.journal_entry.posted_by == w.clerk
        assert services.payment_state(inv, user=w.clerk) == "paid"
        refund = receipt(w, "100.00", kind="refund")
        with pytest.raises(access.AccessDenied) as exc:  # K records F, K posts F: refused (access OQ-26)
            services.post_receipt(refund, user=w.clerk)
        assert exc.value.code == "self_approval"
        refund.refresh_from_db()
        assert (refund.status, refund.number) == ("draft", "")
        assert je(services.post_receipt(refund, user=w.clerk2)) == [  # L posts F (E6)
            ("1100", D("0"), D("100.00"), "normal"), ("1200", D("100.00"), D("0"), "normal")]


def test_e5_bank_charge_booked_gross(w):
    inv = final(w, invoice(w, [("rent_commercial", "1", "10000.00")]))
    posted = services.post_receipt(receipt(w, "11500.00", bank_charge="25.00", applications=[(inv, "11500.00")]),
                                   user=w.clerk2)
    assert je(posted) == [("1100", D("11475.00"), D("0"), "normal"), ("1200", D("0"), D("11500.00"), "normal"),
                          ("5700", D("25.00"), D("0"), "normal")]


def test_r38_unallocated_receipt_stays_as_customer_credit_then_allocates(w):
    inv = final(w, invoice(w))
    posted = services.post_receipt(receipt(w, "2000.00"), user=w.clerk2)
    assert services.open_amount(posted, user=w.clerk) == D("2000.00")
    alloc = services.allocate(posted, inv, "1150.00", user=w.clerk)
    assert alloc.amount == D("1150.00") and services.open_amount(inv, user=w.clerk) == D("0.00")
    assert services.open_amount(posted, user=w.clerk) == D("850.00")
    assert JournalEntry.objects.count() == 2  # R35: allocation has no GL effect


def test_at29_allocation_checks_party_entity_and_open_amount(w):
    other = services.create_customer(w.alm, user=w.clerk, code="B2", name_ar="عميل ب", is_vat_registered=True,
                                     vat_number="310000000000003", **ADDRESS)
    inv_b = final(w, invoice(w, customer=other))
    r = services.post_receipt(receipt(w, "5000.00"), user=w.clerk2)
    with pytest.raises(InvoicingError) as exc:
        services.allocate(r, inv_b, "100.00", user=w.clerk)
    assert "different parties" in " ".join(exc.value.messages)
    sub_customer = services.create_customer(w.sub, user=w.sub_clerk, code="S1", name_ar="عميل التابعة",
                                            is_vat_registered=True, vat_number="330000000000003", **ADDRESS)
    sub_inv = invoice(w, customer=sub_customer, user=w.sub_clerk, entity=w.sub)
    sub_inv = final(w, sub_inv, w.sub_cfo)
    with pytest.raises(access.AccessDenied):  # ALM clerk has no right in SUB
        services.allocate(r, sub_inv, "100.00", user=w.clerk)
    inv_a = final(w, invoice(w))
    with pytest.raises(InvoicingError) as exc2:
        services.allocate(r, inv_a, "1150.01", user=w.clerk)
    assert "more than is open" in " ".join(exc2.value.messages)
    with pytest.raises(InvoicingError):
        services.add_receipt_application(receipt(w, "100.00"), inv_b, "50.00", user=w.clerk)


def test_at30_allocations_are_append_only(w):
    inv = final(w, invoice(w))
    r = services.post_receipt(receipt(w, applications=[(inv, "1150.00")]), user=w.clerk2)
    alloc = ReceivableAllocation.objects.get(receipt=r)
    alloc.amount = D("1")
    with pytest.raises(InvoicingError):
        alloc.save()
    with pytest.raises(InvoicingError):
        alloc.delete()
    with pytest.raises(InvoicingError):
        ReceivableAllocation.objects.filter(pk=alloc.pk).update(amount=D("1"))
    with pytest.raises(InvoicingError):
        ReceivableAllocation.objects.filter(pk=alloc.pk).delete()
    undo = services.unallocate(alloc, user=w.clerk)
    assert (undo.amount, undo.reverses) == (D("-1150.00"), ReceivableAllocation.objects.get(pk=alloc.pk))
    assert services.open_amount(inv, user=w.clerk) == D("1150.00")
    with pytest.raises(InvoicingError):
        services.unallocate(alloc, user=w.clerk)  # once only


def test_at28_receipt_reversal_needs_unallocation(w):
    inv = final(w, invoice(w))
    r = services.post_receipt(receipt(w, applications=[(inv, "1150.00")]), user=w.clerk2)
    with pytest.raises(InvoicingError) as exc:
        services.reverse_document(r, user=w.accountant)
    assert exc.value.code == "has_allocations"
    services.unallocate(ReceivableAllocation.objects.get(receipt=r), user=w.clerk)
    reversed_ = services.reverse_document(r, user=w.accountant, reason="Bounced")
    assert reversed_.status == "reversed" and reversed_.reversal_entry.reversal_of == r.journal_entry
    with pytest.raises(InvoicingError):  # one booking per document: never posted again
        services.post_receipt(r, user=w.clerk2)


def test_r67_advance_and_deposit_receipts_need_a_contract(w):
    for purpose in ("advance", "deposit"):
        with pytest.raises(InvoicingError) as exc:
            receipt(w, purpose=purpose)
        assert exc.value.code == "purpose" and "عقد" in " ".join(exc.value.messages)


def test_e6_refund_to_customer(w):
    r = services.post_receipt(receipt(w, "100.00", kind="refund"), user=w.clerk2)
    assert je(r) == [("1100", D("0"), D("100.00"), "normal"), ("1200", D("100.00"), D("0"), "normal")]


def test_r59_vendor_payment_needs_cfo_approval(w):
    posted_bill = services.approve_purchase_document(bill(w), user=w.cfo)
    p = services.create_vendor_payment(w.alm, user=w.ap, vendor=w.vendor, date=date(2027, 1, 25), amount="1150.00")
    services.add_payment_application(p, posted_bill, "1150.00", user=w.ap)
    with pytest.raises(InvoicingError):  # not submitted yet
        services.approve_vendor_payment(p, user=w.cfo)
    services.submit_vendor_payment(p, user=w.ap)
    with pytest.raises(access.AccessDenied):
        services.approve_vendor_payment(p, user=w.ap)
    posted = services.approve_vendor_payment(p, user=w.cfo)
    assert (posted.number, posted.approved_by) == ("ALM-PAY-2027-000001", w.cfo)
    assert je(posted) == [("1100", D("0"), D("1150.00"), "normal"), ("2100", D("1150.00"), D("0"), "normal")]
    assert services.payment_state(posted_bill, user=w.ap) == "paid"


def test_r59_vendor_payment_in_closed_period_rolls_back(w):
    from ledger.models import FiscalPeriod

    posted_bill = services.approve_purchase_document(bill(w), user=w.cfo)
    p = services.create_vendor_payment(w.alm, user=w.ap, vendor=w.vendor, date=date(2027, 1, 25), amount="1150.00")
    services.add_payment_application(p, posted_bill, "1150.00", user=w.ap)
    services.submit_vendor_payment(p, user=w.ap)
    FiscalPeriod.objects.filter(entity=w.alm, name="2027-01").update(status="closed")
    with pytest.raises(InvoicingError):
        services.approve_vendor_payment(p, user=w.cfo)
    p.refresh_from_db()
    assert (p.status, p.number) == ("submitted", "")


def test_r18_foreign_currency_receipt_settles_at_carrying_rate_with_realised_fx(w):
    usd_rate(w, "3.75", on=date(2027, 1, 10))
    inv = final(w, invoice(w, [("rent_commercial", "1", "1000.00")], currency="USD"))  # USD 1,150 = SAR 4,312.50
    usd_rate(w, "3.80", on=date(2027, 2, 10))
    r = receipt(w, "1150.00", currency="USD", date=date(2027, 2, 10), applications=[(inv, "1150.00")])
    posted = services.post_receipt(r, user=w.clerk2)
    assert je(posted) == [
        ("1100", D("4370.00"), D("0"), "normal"),
        ("1200", D("0"), D("4312.50"), "settlement"),
        ("4700", D("0"), D("57.50"), "realised_fx"),
    ]
    settle = posted.journal_entry.lines.get(line_kind="settlement")
    assert settle.settles_line == inv.journal_entry.lines.get(account__code="1200")
    alloc = ReceivableAllocation.objects.get(receipt=posted)
    assert alloc.via_settlement and services.open_amount(inv, user=w.clerk) == D("0.00")
    with pytest.raises(InvoicingError) as exc:  # settled in the ledger: reverse the receipt, not un-allocate
        services.unallocate(alloc, user=w.clerk)
    assert exc.value.code == "unallocate_settled"
    services.reverse_document(posted, user=w.accountant)
    assert services.open_amount(inv, user=w.clerk) == D("1150.00")
    rows = {r_["account"]: r_ for r_ in services.reconcile_subledger(w.alm, date(2027, 12, 31), user=w.accountant)}
    assert rows["1200"]["difference"] == D("0")


def test_foreign_credit_note_settles_its_invoice(w):
    usd_rate(w, "3.75", on=date(2027, 1, 10))
    inv = final(w, invoice(w, [("rent_commercial", "1", "1000.00")], currency="USD"))
    usd_rate(w, "3.70", on=date(2027, 1, 20))
    cn = invoice(w, [("rent_commercial", "1", "400.00")], doc_type="credit_note", original_document=inv,
                 issue_date=date(2027, 1, 20), payment_means_code="30", reason_ar="تخفيض")
    cn = final(w, cn)
    lines = je(cn)
    assert ("1200", D("0"), D("1725.00"), "settlement") in lines  # USD 460 at the carrying 3.75
    assert ("4100", D("1480.00"), D("0"), "normal") in lines and ("2200", D("222.00"), D("0"), "normal") in lines
    assert ("5750", D("23.00"), D("0"), "realised_fx") in lines  # AR cleared at 3.75, income reduced at 3.70
    assert services.open_amount(inv, user=w.clerk) == D("690.00")
    assert cn.total_vat_sar == D("222.00")


def test_r33_receipts_need_the_receipt_right(w):
    with pytest.raises(access.AccessDenied):
        services.create_receipt(w.alm, user=w.ap, customer=w.customer, date=date(2027, 1, 20), amount="10")
    with pytest.raises(access.AccessDenied):
        services.create_receipt(w.alm, user=w.viewer, customer=w.customer, date=date(2027, 1, 20), amount="10")


def test_at31_subledger_ties_to_the_ledger_per_entity(w):
    from invoicing.conftest import make_entity  # noqa: F401

    group_customer = services.create_customer(w.alm, user=w.clerk, code="G-SUB", name_ar="التابعة", group_entity=w.sub,
                                              is_vat_registered=True, vat_number="311111111100003")
    inv = final(w, invoice(w, [("rent_commercial", "1", "10000.00")]))
    final(w, invoice(w, customer=group_customer))
    cn = invoice(w, [("rent_commercial", "1", "1000.00")], doc_type="credit_note", original_document=inv,
                 payment_means_code="30", reason_ar="تخفيض")
    final(w, cn)
    services.post_receipt(receipt(w, "5000.00", applications=[(inv, "5000.00")]), user=w.clerk2)
    services.approve_purchase_document(bill(w), user=w.cfo)
    sub_customer = services.create_customer(w.sub, user=w.sub_clerk, code="S1", name_ar="عميل التابعة",
                                            is_vat_registered=True, vat_number="330000000000003", **ADDRESS)
    final(w, invoice(w, customer=sub_customer, user=w.sub_clerk, entity=w.sub), w.sub_cfo)
    rows = services.reconcile_subledger(w.alm, date(2027, 12, 31), user=w.accountant)
    assert rows and all(r["difference"] == D("0") for r in rows)
    by = {(r["account"], r["counterparty"]): r["ledger"] for r in rows}
    assert by[("1200", None)] == D("11500.00") - D("1150.00") - D("5000.00")
    assert by[("1210", "SUB")] == D("1150.00") and by[("2100", None)] == D("-1150.00")
    with pytest.raises(access.AccessDenied):  # SUB is reconciled separately, by its own users
        services.reconcile_subledger(w.sub, date(2027, 12, 31), user=w.accountant)


def gbp_rate(w, rate, on):
    from core.access.testing import make_user, member

    treasurer = make_user(f"gbp{on.isoformat()}")
    member(treasurer, w.alm, "group_treasury")
    return ledger.create_rate(user=treasurer, from_currency="GBP", to_currency="SAR", on=on, rate=D(rate), group=w.alm)


def test_f5_foreign_bill_paid_later_books_a_realised_loss(w):
    gbp_rate(w, "4.70", date(2027, 1, 15))
    nr = services.create_vendor(w.alm, user=w.ap, vendor_kind="business", code="UK1", name_ar="مورد يفوتر بالجنيه", currency="GBP")
    posted_bill = services.approve_purchase_document(
        bill(w, [("5300", "1", "2000.00", {"vat_category": "O"})], vendor=nr, number="UK-1"), user=w.cfo)
    assert posted_bill.journal_entry.lines.get(account__code="2100").credit == D("9400.00")
    gbp_rate(w, "4.76", date(2027, 2, 15))
    p = services.create_vendor_payment(w.alm, user=w.ap, vendor=nr, date=date(2027, 2, 15), amount="2000.00")
    services.add_payment_application(p, posted_bill, "2000.00", user=w.ap)
    services.submit_vendor_payment(p, user=w.ap)
    paid = services.approve_vendor_payment(p, user=w.cfo)
    assert je(paid) == [("1100", D("0"), D("9520.00"), "normal"), ("2100", D("9400.00"), D("0"), "settlement"),
                        ("5750", D("120.00"), D("0"), "realised_fx")]


def test_r18_partial_then_final_settlement_leaves_no_residue(w):
    usd_rate(w, "3.7501", on=date(2027, 1, 10))
    inv = final(w, invoice(w, [("rent_commercial", "1", "1000.00")], currency="USD"))
    item = inv.journal_entry.lines.get(account__code="1200")
    usd_rate(w, "3.76", on=date(2027, 2, 10))
    for amount in ("333.33", "816.67"):
        services.post_receipt(receipt(w, amount, currency="USD", date=date(2027, 2, 10), applications=[(inv, amount)]),
                              user=w.clerk2)
    from ledger.models import JournalLine

    settled = JournalLine.objects.filter(settles_line=item)
    assert sum(l.txn_amount for l in settled) == item.txn_amount
    assert sum(l.credit for l in settled) == item.debit  # the final settlement clears exactly what is left
    assert services.payment_state(inv, user=w.clerk) == "paid"


def test_d14_no_payment_to_a_non_resident_vendor_until_wht_is_built(w):
    nr = services.create_vendor(w.alm, user=w.ap, vendor_kind="business", code="NR9", name_ar="مورد أجنبي", is_resident=False,
                                residence_country="GB")
    with pytest.raises(InvoicingError) as exc:
        services.create_vendor_payment(w.alm, user=w.ap, vendor=nr, date=date(2027, 1, 25), amount="100.00")
    assert exc.value.code == "wht_not_built" and "ضريبة الاستقطاع" in " ".join(exc.value.messages)


# --- reviewer round 6 (items 7, 8): minor units of open amounts; allocation checked on the locked target ------

def test_open_amounts_have_the_currency_minor_units(w):
    inv = final(w, invoice(w))  # SAR, gross 1,150.00 (column holds 3 decimals)
    assert str(services.open_amount(inv, user=w.clerk)) == "1150.00"
    posted = services.post_receipt(receipt(w, "2000.00"), user=w.clerk2)
    assert str(services.open_amount(posted, user=w.clerk)) == "2000.00"
    services.allocate(posted, inv, "1150.00", user=w.clerk)
    assert str(services.open_amount(inv, user=w.clerk)) == "0.00"
    assert str(services.open_amount(posted, user=w.clerk)) == "850.00"
    b = services.approve_purchase_document(bill(w), user=w.cfo)
    assert str(services.open_amount(b, user=w.ap)) == "1150.00"


def test_open_amount_of_a_kwd_invoice_has_three_decimals(w):
    from invoicing.test_owner_decisions import _rate

    _rate(w, "KWD", "12.2000")
    inv = final(w, invoice(w, [("rent_commercial", "2", "500.000")], currency="KWD"))
    assert str(services.open_amount(inv, user=w.clerk)) == "1150.000"


def test_allocation_amount_is_checked_against_the_locked_target_currency(w):
    """A stale in-memory target claiming a 3-decimal currency must not let 3 decimals into a SAR allocation."""
    inv = final(w, invoice(w))
    posted = services.post_receipt(receipt(w, "2000.00"), user=w.clerk2)
    inv.currency_id = "KWD"  # stale / tampered copy; the stored invoice is SAR
    with pytest.raises(InvoicingError) as exc:
        services.allocate(posted, inv, "10.005", user=w.clerk)
    assert exc.value.code == "money_decimals" and "2 decimals" in str(exc.value)
    assert not ReceivableAllocation.objects.filter(target_id=inv.pk).exists()
    assert services.allocate(posted, inv, "10.00", user=w.clerk).amount == D("10.00")
