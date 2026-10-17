"""Withholding tax on payments to non-resident vendors (invoicing D14, R61 to R64, E20 to E22; AT-54 to AT-58, AT-75).

Rates are the OQ-8 reference rows loaded by migration ``invoicing.0015`` (unverified; tax-adviser sign-off
pending), e.g. technical and consulting services 5%. All parties and amounts are made up.
"""
from datetime import date
from decimal import Decimal

import pytest

from core import access
from core.access.models import Membership, Role, RolePermission
from core.access.testing import make_user, member
from invoicing import services
from invoicing.conftest import D, make_entity, usd_rate
from invoicing.models import InvoicingError, TreatyCertificate, WhtCategory, WhtDeduction
from invoicing.test_purchases import bill
from ledger.models import Account

pytestmark = pytest.mark.django_db

SERVICES = {"supply_kind": "services"}


def nr_vendor(w, code="NR1", category="technical_services", entity=None, user=None, **fields):
    return services.create_vendor(entity or w.alm, user=user or w.ap, vendor_kind="business", code=code,
                                  name_ar="مورد أجنبي تجريبي", is_resident=False, residence_country="GB",
                                  wht_category=category, **fields)


def nr_bill(w, vendor, amount="10000.00", number="NR-1", on=date(2027, 1, 15), lines=None, **kw):
    lines = lines or (("5300", "1", amount, SERVICES),)
    return services.approve_purchase_document(bill(w, lines, vendor=vendor, number=number, on=on, **kw), user=w.cfo)


def pay(w, vendor, cash, applications, on=date(2027, 1, 25), currency=None, approve=True, user=None):
    user = user or w.ap
    p = services.create_vendor_payment(vendor.entity, user=user, vendor=vendor, date=on, amount=cash,
                                       **({"currency": currency} if currency else {}))
    for target, amount in applications:
        services.add_payment_application(p, target, amount, user=user)
    p = services.submit_vendor_payment(p, user=user)
    return services.approve_vendor_payment(p, user=w.cfo) if approve else p


def je(doc):
    doc.refresh_from_db()
    return sorted((l.account.code, l.debit, l.credit) for l in doc.journal_entry.lines.select_related("account"))


def test_reference_rates_are_loaded_unverified():
    rows = {r.code: r for r in WhtCategory.objects.all()}
    assert rows["technical_services"].rate == D("5.00") and rows["management_fees"].rate == D("20.00")
    assert rows["royalties"].rate == D("15.00") and rows["other_payments"].rate == D("15.00")
    assert not any(r.is_verified for r in rows.values())  # OQ-8: tax-adviser sign-off pending
    assert rows["loan_charges"].sharia_prohibited and len(rows) == 11
    assert all(r.name_ar for r in rows.values())


def test_wht_accounts_are_in_the_chart_as_proposed(w):
    new = make_entity("NEW", "full", w.alm, vat="333333333300003", cr="1010000009")
    for e in (w.alm, new):
        assert Account.objects.get(entity=e, code="2220").type == "liability"
        assert Account.objects.get(entity=e, code="5920").type == "expense"
    from importlib import import_module

    from django.apps import apps

    Account.objects.filter(entity=new, code__in=["2220", "5920"]).delete()
    import_module("ledger.migrations.0016_wht_accounts").forwards(apps, None)  # existing charts get them too
    assert Account.objects.filter(entity=new, code__in=["2220", "5920"]).count() == 2


def test_at54_e20_vendor_bears_the_tax(w):
    nr = nr_vendor(w)
    b = nr_bill(w, nr)
    assert je(b) == [("1310", D("1500.00"), D("0")), ("2100", D("0"), D("10000.00")), ("2200", D("0"), D("1500.00")),
                     ("5300", D("10000.00"), D("0"))]  # E9 reverse charge, AP holds net
    p = pay(w, nr, "9500.00", [(b, "10000.00")])
    assert je(p) == [("1100", D("0"), D("9500.00")), ("2100", D("10000.00"), D("0")), ("2220", D("0"), D("500.00"))]
    (row,) = WhtDeduction.objects.filter(payment=p)
    assert (row.base_amount, row.rate_applied, row.wht_amount, row.wht_amount_sar, row.return_month) == (
        D("10000.000"), D("5.00"), D("500.000"), D("500.00"), date(2027, 1, 1))
    assert (p.wht_amount, p.wht_gross_up) == (D("500.000"), False)
    assert services.payment_state(b, user=w.ap) == "paid"
    assert all(r["difference"] == 0 for r in services.reconcile_subledger(w.alm, date(2027, 1, 31), user=w.accountant))


def test_at55_e21_gross_up_borne_by_the_entity(w):
    nr = nr_vendor(w, wht_gross_up=True)
    b = nr_bill(w, nr)
    p = pay(w, nr, "10000.00", [(b, "10000.00")])
    assert je(p) == [("1100", D("0"), D("10000.00")), ("2100", D("10000.00"), D("0")),
                     ("2220", D("0"), D("526.32")), ("5920", D("526.32"), D("0"))]  # 10,000 x 5/95
    assert WhtDeduction.objects.get(payment=p).gross_up and p.wht_gross_up
    assert services.payment_state(b, user=w.ap) == "paid"


def test_cash_must_be_settled_amount_less_wht(w):
    nr = nr_vendor(w)
    b = nr_bill(w, nr)
    with pytest.raises(InvoicingError) as exc:
        pay(w, nr, "10000.00", [(b, "10000.00")], approve=False)
    assert exc.value.code == "wht_invalid" and "9500.00" in str(exc.value) and "المبلغ المدفوع" in str(exc.value)
    preview = services.wht_preview(services.create_vendor_payment(
        w.alm, user=w.ap, vendor=nr, date=date(2027, 1, 25), amount="1.00"), user=w.ap)
    assert preview["problems"]  # no application: refused (R62)


def test_at56_verified_treaty_certificate_until_its_last_day(w):
    nr = nr_vendor(w)
    cert = services.add_treaty_certificate(nr, user=w.ap, country="GB", treaty_rate="0.00", valid_from=date(2027, 1, 1),
                                           valid_to=date(2027, 6, 30), received_on=date(2027, 1, 2))
    b1, b2, b3 = (nr_bill(w, nr, "1000.00", number=f"NR-{i}") for i in (1, 2, 3))
    p0 = pay(w, nr, "950.00", [(b1, "1000.00")], on=date(2027, 6, 30))  # not verified yet: statutory rate
    assert WhtDeduction.objects.get(payment=p0).treaty_certificate is None
    with pytest.raises(access.AccessDenied):  # the person who entered it never verifies it
        services.verify_treaty_certificate(cert, user=w.ap)
    cert = services.verify_treaty_certificate(cert, user=w.accountant)
    p1 = pay(w, nr, "1000.00", [(b2, "1000.00")], on=date(2027, 6, 30))
    row = WhtDeduction.objects.get(payment=p1)
    assert (row.wht_amount, row.rate_applied, row.treaty_certificate) == (D("0.000"), D("0.00"), cert)
    assert je(p1) == [("1100", D("0"), D("1000.00")), ("2100", D("1000.00"), D("0"))]  # no 2220 line
    p2 = pay(w, nr, "950.00", [(b3, "1000.00")], on=date(2027, 7, 1))
    row = WhtDeduction.objects.get(payment=p2)
    assert (row.wht_amount, row.treaty_certificate) == (D("50.000"), None)
    cert.treaty_rate = D("1.00")
    with pytest.raises(InvoicingError):  # verified: never edited
        cert.save()
    with pytest.raises(InvoicingError):
        cert.delete()


def test_at57_bill_line_without_category_cannot_be_paid(w):
    nr = services.create_vendor(w.alm, user=w.ap, vendor_kind="business", code="NR2", name_ar="مورد أجنبي",
                                is_resident=False, residence_country="DE")
    b = nr_bill(w, nr)  # booked without a category (R30 bills stay allowed)
    with pytest.raises(InvoicingError) as exc:  # AT-75: no category on the vendor, no payment
        services.create_vendor_payment(w.alm, user=w.ap, vendor=nr, date=date(2027, 1, 25), amount="100.00")
    assert exc.value.code == "wht_category_required" and "ضريبة الاستقطاع" in str(exc.value)
    services.update_vendor(nr, user=w.ap, wht_category="technical_services")
    with pytest.raises(InvoicingError) as exc:
        pay(w, nr, "9500.00", [(b, "10000.00")], approve=False)
    assert exc.value.code == "wht_invalid" and "R61" in str(exc.value)
    refund = services.create_vendor_payment(w.alm, user=w.ap, vendor=nr, date=date(2027, 1, 25), amount="10.00",
                                            kind="refund_received")
    assert refund.status == "draft"  # refunds received from non-residents stay allowed (AT-75)


def test_r61_category_follows_the_vendor_and_is_refused_on_residents(w):
    nr = nr_vendor(w, category="royalties")
    doc = bill(w, (("5300", "1", "100.00", SERVICES), ("5300", "1", "100.00", {**SERVICES,
                                                                                "wht_category": "management_fees"})),
               vendor=nr, submit=False)
    assert [l.wht_category.code for l in doc.lines.order_by("line_no")] == ["royalties", "management_fees"]
    with pytest.raises(InvoicingError):
        services.add_purchase_line(bill(w, vendor=w.vendor, number="R-1", submit=False), user=w.ap, account="5300",
                                   quantity="1", unit_price="1.00", description_ar="بند",
                                   wht_category="royalties")
    with pytest.raises(InvoicingError) as exc:
        services.update_vendor(w.vendor, user=w.ap, wht_category="royalties")
    assert exc.value.code == "party_invalid"


def test_two_categories_on_one_bill_split_pro_rata(w):
    nr = nr_vendor(w, category="technical_services")
    b = nr_bill(w, nr, lines=(("5300", "1", "6000.00", SERVICES),
                              ("5300", "1", "4000.00", {**SERVICES, "wht_category": "royalties"})))
    p = pay(w, nr, "4550.00", [(b, "5000.00")])  # half the bill: 3,000 at 5% + 2,000 at 15% = 150 + 300
    rows = {r.wht_category.code: r for r in WhtDeduction.objects.filter(payment=p)}
    assert (rows["technical_services"].base_amount, rows["technical_services"].wht_amount) == (D("3000.000"), D("150.000"))
    assert (rows["royalties"].base_amount, rows["royalties"].wht_amount) == (D("2000.000"), D("300.000"))
    assert services.payment_state(b, user=w.ap) == "partial"


def test_loan_charges_never_in_a_sharia_entity(w):
    with pytest.raises(InvoicingError) as exc:
        nr_vendor(w, category="loan_charges")
    assert "Sharia" in str(exc.value)


def test_wht_fields_need_the_enter_wht_data_right(w):
    role = Role.objects.create(code="vendor_master_only", name_en="Vendor master", name_ar="بيانات الموردين",
                               scope="entity", kind="human")
    for codename in ("invoicing.manage_vendor", "invoicing.view"):
        RolePermission.objects.create(role=role, codename=codename)
    user = make_user("vendor.master")
    Membership.objects.create(user=user, entity=w.alm, role=role, granted_by=w.cfo, reason="test",
                              valid_from=access.services.now() - __import__("datetime").timedelta(hours=1))
    with pytest.raises(access.AccessDenied):
        nr_vendor(w, user=user)
    nr = services.create_vendor(w.alm, user=user, vendor_kind="business", code="NR5", name_ar="مورد",
                                is_resident=False, residence_country="GB")
    with pytest.raises(access.AccessDenied):
        services.update_vendor(nr, user=user, wht_gross_up=True)


def test_at58_monthly_return_data_ties_to_2220_and_excludes_other_entities(w):
    nr_a = nr_vendor(w, code="NRA", category="technical_services")
    nr_b = nr_vendor(w, code="NRB", category="management_fees")
    a = nr_bill(w, nr_a, "1000.00", number="A-1", on=date(2027, 3, 2))
    b = nr_bill(w, nr_b, "2000.00", number="B-1", on=date(2027, 3, 2))
    pay(w, nr_a, "950.00", [(a, "1000.00")], on=date(2027, 3, 10))
    pb = pay(w, nr_b, "1600.00", [(b, "2000.00")], on=date(2027, 3, 12))
    sub_ap = make_user("sub.ap")
    member(sub_ap, w.sub, "payables_clerk")
    member(w.cfo, w.sub, "approver")
    nr_s = nr_vendor(w, code="NRS", entity=w.sub, user=sub_ap)
    s_bill = services.approve_purchase_document(
        bill_in(w, w.sub, nr_s, sub_ap), user=w.cfo)
    pay(w, nr_s, "950.00", [(s_bill, "1000.00")], on=date(2027, 3, 10), user=sub_ap)
    data = services.wht_return_data(w.alm, date(2027, 3, 31), user=w.accountant)
    assert [(r["category"], r["vendor"], r["wht_sar"], r["country"]) for r in data["rows"]] == [
        ("management_fees", "NRB", D("400.00"), "GB"), ("technical_services", "NRA", D("50.00"), "GB")]
    assert data["total_wht_sar"] == data["ledger_2220_sar"] == D("450.00") and data["difference"] == 0
    assert services.wht_return_data(w.alm, date(2027, 2, 1), user=w.accountant)["rows"] == []
    services.unallocate(pb.allocations.get(), user=w.accountant)  # R25: un-allocate before reversing
    reversed_ = services.reverse_document(pb, user=w.accountant, date=date(2027, 4, 2))
    april = services.wht_return_data(w.alm, date(2027, 4, 1), user=w.accountant)
    assert [(r["vendor"], r["wht_sar"]) for r in april["rows"]] == [("NRB", D("-400.00"))]
    assert april["difference"] == 0 and reversed_.status == "reversed"
    assert WhtDeduction.objects.filter(payment=pb).count() == 1  # kept; the reversal shows in April


def bill_in(w, entity, vendor, user):
    doc = services.create_purchase_document(entity, user=user, vendor=vendor, vendor_document_number="S-1",
                                            vendor_document_date=date(2027, 3, 2), accounting_date=date(2027, 3, 2))
    services.add_purchase_line(doc, user=user, account="5300", quantity="1", unit_price="1000.00",
                               description_ar="خدمات", supply_kind="services")
    services.update_purchase_document(doc, user=user, vendor_total_vat="0", vendor_total_gross="1000.00")
    return services.submit_purchase_document(doc, user=user)


def test_wht_on_a_foreign_currency_payment_ties_in_sar(w):
    usd_rate(w, "3.75", on=date(2027, 1, 15))
    usd_rate(w, "3.76", on=date(2027, 1, 25))
    nr = nr_vendor(w, currency="USD")
    b = nr_bill(w, nr, "1000.00", currency="USD")
    p = pay(w, nr, "950.00", [(b, "1000.00")], currency="USD")
    row = WhtDeduction.objects.get(payment=p)
    line = p.journal_entry.lines.get(account__code="2220")
    assert (row.wht_amount, row.wht_amount_sar, line.credit, line.txn_amount) == (
        D("50.000"), D("188.00"), D("188.00"), D("50.000"))  # 50 USD at the payment's pinned 3.76
    assert services.payment_state(b, user=w.ap) == "paid"
    data = services.wht_return_data(w.alm, date(2027, 1, 1), user=w.accountant)
    assert data["difference"] == 0 and data["total_wht_sar"] == D("188.00")


def test_deductions_are_immutable(w):
    nr = nr_vendor(w)
    p = pay(w, nr, "9500.00", [(nr_bill(w, nr), "10000.00")])
    row = WhtDeduction.objects.get(payment=p)
    row.wht_amount = Decimal("1")
    for attempt in (row.save, row.delete, lambda: WhtDeduction.objects.filter(pk=row.pk).update(wht_amount=1),
                    lambda: WhtDeduction.objects.filter(pk=row.pk).delete()):
        with pytest.raises(InvoicingError):
            attempt()
    assert TreatyCertificate.objects.count() == 0


def test_r64_wht_needs_a_sar_functional_currency(w):
    """Review finding 7: an entity keeping its books in another currency gets no WHT computation or return data."""
    usd = make_entity("USE", "full", w.alm, vat="333333333300003", cr="1010000009", base_currency_id="USD")
    ap, accountant = make_user("use.ap"), make_user("use.accountant")
    member(ap, usd, "payables_clerk")
    member(accountant, usd, "accountant")
    with pytest.raises(InvoicingError) as exc:
        services.wht_return_data(usd, date(2027, 1, 1), user=accountant)
    assert exc.value.code == "wht_requires_sar_functional" and "الريال" in str(exc.value)
    nr = nr_vendor(w, code="NRU", entity=usd, user=ap, currency="USD")
    with pytest.raises(InvoicingError) as exc:
        services.create_vendor_payment(usd, user=ap, vendor=nr, date=date(2027, 1, 25), amount="100.00")
    assert exc.value.code == "wht_requires_sar_functional"
    assert services.wht_return_data(w.alm, date(2027, 1, 1), user=w.accountant)["rows"] == []  # SAR: allowed
