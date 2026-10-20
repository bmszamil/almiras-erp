"""Bai' ajel sale to an employee (invoicing R112, E28; payroll R47, R48, AT-57, AT-66; owner decisions B8, E8).

Runs payroll's real sale approval against the real invoicing port (``invoicing.payroll_port``), with payroll's
fixtures (anonymised entities, users and employees) and invoicing's fake ZATCA port. The Riyadh clock is fixed;
nothing depends on the real date or calls an external service.
"""
from datetime import date, datetime
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection

from core import access
from core.access import catalogue
from core.access.models import Role
from core.access.seed import ROLE_SEED_V15, SYSTEM_USERS_V2
from core.access.testing import make_agent, make_user, member
from core.models import Entity
from invoicing import payroll_port as port_module
from invoicing import services as inv
from invoicing.conftest import SELLER
from invoicing.models import (
    Customer,
    DeferredProfitRelease,
    DeferredSale,
    IncomeType,
    InvoicingError,
    PurchaseDocument,
    SalesDocument,
)
from ledger.models import FiscalPeriod, JournalLine
from payroll import invoicing_port as payroll_port
from payroll import services as payroll
from payroll.conftest import asset_bill, clock, pclock, saudi, w  # noqa: F401  (payroll's fixtures)

D = Decimal
RIYADH_DAY = datetime(2027, 3, 20, 10, 0, tzinfo=inv.RIYADH)


@pytest.fixture
def invoicing():
    """Payroll's ``w`` asks for this fixture: here it installs the real port (payroll R47 -> invoicing R112)."""
    port = port_module.PayrollInvoicingPort()
    previous, payroll_port._override = payroll_port._override, port
    try:
        yield port
    finally:
        payroll_port._override = previous


@pytest.fixture
def bw(w, zatca, monkeypatch):
    """Payroll's world plus what an issuing Saudi entity needs: seller data, the bai' ajel and ZATCA queue system
    users, and a fixed Riyadh clock (2027-03-20)."""
    for entity, vat in ((w.alm, "300000000000003"), (w.sub, "311111111100003")):
        Entity.objects.filter(pk=entity.pk).update(
            vat_number=vat, cr_number=f"10100000{entity.pk:02d}"[:10], legal_name_en=f"{entity.code} Test Company",
            legal_name_ar=f"شركة {entity.code} للاختبار", **SELLER)
        entity.refresh_from_db()
    w.system = access.system_user("deferred_sale")
    w.queue = access.system_user("zatca_queue")
    for entity in (w.alm, w.sub):
        member(w.system, entity, "system_deferred_sale")
        member(w.queue, entity, "system_zatca_queue")
    w.zatca = zatca
    monkeypatch.setattr(inv, "_riyadh_now", lambda: RIYADH_DAY)
    return w


def bilingual(exc):
    text = " ".join(exc.messages) if hasattr(exc, "messages") else str(exc)
    assert " / " in text and any("؀" <= ch <= "ۿ" for ch in text), text
    return text


def contract(w, emp, cost="30000.00", profit="3600.00", term=24, acquire=True, bill=None):
    c = payroll.create_deferred_sale(emp, user=w.p, asset_description_ar="سيارة", asset_description_en="Car",
                                     cost=cost, profit_amount=profit, term_months=term, first_period="2027-04",
                                     consent_ref="SIGNED-BAJ-1")
    c = payroll.approve_deferred_sale_terms(payroll.submit_deferred_sale_terms(c, user=w.p), user=w.mp)
    if acquire:  # R47 step 3: bought in the company's name, then possessed
        c = payroll.record_asset_acquisition(c, user=w.p, purchase_bill=bill or asset_bill(w, cost),
                                             possession_date=date(2027, 2, 16), possession_evidence_ref="DELIVERY-1", asset_type="vehicle")
    return c


def approve(w, c, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):  # the ZATCA hand-off runs after commit (R40)
        return payroll.approve_deferred_sale(c, user=w.mp)


def sale_doc(c):
    return SalesDocument.objects.get(deferred_sale__source_id=c.pk, deferred_sale__active=True)


def entry_by_account(entry):
    out = {}
    for line in entry.lines.select_related("account"):
        out[line.account.code] = out.get(line.account.code, D("0")) + line.debit - line.credit
    return out


def balance(entity, code):
    lines = JournalLine.objects.filter(entry__entity=entity, entry__status__in=["posted", "reversed"],
                                       account__code=code)
    return sum((l.debit - l.credit for l in lines), D("0"))


def direct(w, c, **overrides):
    """create_deferred_sale as the port calls it, with some inputs changed (refusal tests)."""
    values = dict(approver=w.mp, employee=c.employee, purchase_bill=c.purchase_bill, cost=c.cost,
                  profit_amount=c.profit_amount, possession_date=c.possession_date,
                  possession_evidence_ref=c.possession_evidence_ref, asset_description_ar=c.asset_description_ar,
                  asset_type=c.asset_type or "vehicle")
    values.update(overrides)
    return inv.create_deferred_sale(c, **values)


@pytest.fixture
def sold(bw, django_capture_on_commit_callbacks):
    """AT-57: S1 buys a car (cost 30,000.00, profit 3,600.00, 24 months); the sale is approved and reported."""
    s1 = saudi(bw)
    c = approve(bw, contract(bw, s1), django_capture_on_commit_callbacks)
    doc = bw.zatca.report(sale_doc(c))
    return bw, s1, c, doc


# --- the sale invoice (R112, E28, AT-57) -------------------------------------------------------------------------

def test_at57_e28_the_sale_is_a_simplified_invoice_to_the_employee_posted_on_reporting(bw,
                                                                                     django_capture_on_commit_callbacks):
    s1 = saudi(bw)
    c = approve(bw, contract(bw, s1), django_capture_on_commit_callbacks)
    doc = sale_doc(c)
    # payroll got the invoice number and the VAT charged to the employee (P20(h)): 24 x 1,610.00 = 38,640.00
    assert (c.sale_document_ref, c.vat_amount, c.receivable) == (doc.number, D("5040.00"), D("38640.00"))
    assert sum(i.amount for i in c.installments.all()) == D("38640.00")
    assert doc.number.startswith("ALM-INV-2027-") and doc.status == "issued" and doc.issue_date == date(2027, 3, 20)
    assert doc.zatca_profile == "simplified" and doc.title_en == "Simplified Tax Invoice"
    assert doc.source == "system:deferred_sale" and doc.created_by == bw.system and doc.approved_by == bw.mp
    assert (doc.total_net, doc.total_vat, doc.total_gross) == (D("33600.00"), D("5040.00"), D("38640.00"))
    lines = list(doc.lines.select_related("income_account", "income_type").order_by("line_no"))
    assert [(l.income_type.code, l.income_account.code, l.vat_category, l.net_amount) for l in lines] == [
        ("deferred_sale", "1250", "S", D("30000.00")), ("deferred_sale", "1241", "S", D("3600.00"))]
    bill = PurchaseDocument.objects.get(pk=c.purchase_bill_id)
    assert bill.number in lines[0].description_ar and bill.number in lines[0].description_en
    assert doc.notes_ar and doc.notes_en and "never increases" in doc.notes_en
    # the employee is an individual customer of their own (R112), not VAT registered; the name is personal data
    customer = doc.customer
    assert (customer.customer_kind, customer.is_vat_registered, customer.employee_id) == ("individual", False, s1.pk)
    assert str(customer) == customer.code == f"EMP-{s1.employee_code}"
    assert bw.zatca.submitted == [(doc.pk, "simplified", bw.queue.pk)]
    assert doc.journal_entry_id is None  # nothing booked before ZATCA reports it (R41)

    final = bw.zatca.report(doc)
    assert final.status == "final"
    assert entry_by_account(final.journal_entry) == {
        "1240": D("38640.00"), "1250": D("-30000.00"), "1241": D("-3600.00"), "2200": D("-5040.00")}
    assert final.journal_entry.posted_by == bw.mp  # the approval of the sale in payroll is the recorded approval
    assert balance(bw.alm, "1250") == D("0.00")  # the asset left the company's books at its cost (E9 then E28)
    assert not JournalLine.objects.filter(entry=final.journal_entry, account__code="1200").exists()


def test_at66_b8_vat_on_cost_plus_margin_charged_to_the_employee(bw, django_capture_on_commit_callbacks):
    s1 = saudi(bw)
    c = approve(bw, contract(bw, s1, cost="100.00", profit="10.00", term=2), django_capture_on_commit_callbacks)
    doc = bw.zatca.report(sale_doc(c))
    assert (doc.total_net, doc.total_vat, doc.total_gross) == (D("110.00"), D("16.50"), D("126.50"))
    assert [i.amount for i in c.installments.order_by("period")] == [D("63.25"), D("63.25")]
    moves = entry_by_account(doc.journal_entry)
    assert moves == {"1240": D("126.50"), "1250": D("-100.00"), "1241": D("-10.00"), "2200": D("-16.50")}
    assert not [code for code in moves if code.startswith("5")]  # the VAT is never a company cost


def test_zero_profit_sale_has_only_the_cost_line(bw, django_capture_on_commit_callbacks):
    c = approve(bw, contract(bw, saudi(bw), cost="1000.00", profit="0.00", term=4), django_capture_on_commit_callbacks)
    doc = bw.zatca.report(sale_doc(c))
    assert list(doc.lines.values_list("income_account__code", flat=True)) == ["1250"]
    assert entry_by_account(doc.journal_entry) == {"1240": D("1150.00"), "1250": D("-1000.00"), "2200": D("-150.00")}


def test_the_port_is_the_configured_payroll_invoicing_port(settings):
    previous, payroll_port._override = payroll_port._override, None
    try:
        assert isinstance(payroll_port.get_port(), port_module.PayrollInvoicingPort)
    finally:
        payroll_port._override = previous


def test_a_second_call_returns_the_same_invoice_and_the_customer_is_reused(sold, django_capture_on_commit_callbacks):
    w, s1, c, doc = sold
    again = inv.create_deferred_sale(c, approver=w.mp, employee=s1, purchase_bill=c.purchase_bill, cost=c.cost,
                                     profit_amount=c.profit_amount, possession_date=c.possession_date,
                                     possession_evidence_ref=c.possession_evidence_ref, asset_description_ar="سيارة")
    assert again.pk == doc.pk and SalesDocument.objects.filter(deferred_sale__isnull=False).count() == 1
    second = approve(w, contract(w, s1, cost="500.00", profit="50.00", term=2), django_capture_on_commit_callbacks)
    assert sale_doc(second).customer_id == doc.customer_id
    assert Customer.objects.filter(employee=s1).count() == 1


def test_a_rejected_sale_invoice_is_replaced_by_a_new_one(bw, django_capture_on_commit_callbacks):
    c = approve(bw, contract(bw, saudi(bw)), django_capture_on_commit_callbacks)
    first = sale_doc(c)
    bw.zatca.reject(first)
    with django_capture_on_commit_callbacks(execute=True):
        result = port_module.PayrollInvoicingPort().create_bai_ajel_sale(c, user=bw.mp)
    new = sale_doc(c)
    assert new.pk != first.pk and result.document_ref == new.number != first.number
    assert SalesDocument.objects.get(pk=first.pk).status == "rejected"  # the number stays used (R20)
    assert DeferredSale.objects.get(sales_document=first).active is False
    assert bw.zatca.report(new).status == "final"


def test_sale_state_tells_payroll_when_the_invoice_is_posted(bw, django_capture_on_commit_callbacks):
    c = approve(bw, contract(bw, saudi(bw)), django_capture_on_commit_callbacks)
    port = port_module.PayrollInvoicingPort()
    state = port.sale_state(c)
    assert (state.status, state.posted, state.vat_amount, state.receivable) == (
        "issued", False, D("5040.00"), D("38640.00"))
    bw.zatca.report(sale_doc(c))
    assert port.sale_state(c).posted is True


# --- refusals (R112, payroll R47, owner decision E8) --------------------------------------------------------------

def test_refused_when_the_purchase_bill_is_not_posted(bw):
    s1 = saudi(bw)
    c = contract(bw, s1)
    draft = inv.create_purchase_document(bw.alm, user=bw.ap, vendor=bw.vendor, vendor_document_number="CAR-DRAFT",
                                         vendor_document_date=date(2027, 2, 15), accounting_date=date(2027, 2, 15))
    with pytest.raises(InvoicingError) as exc:
        direct(bw, c, purchase_bill=draft)
    assert exc.value.code == "purchase_bill_not_posted" and "posted" in bilingual(exc.value)
    with pytest.raises(InvoicingError) as exc:
        direct(bw, c, purchase_bill=None)
    assert exc.value.code == "purchase_bill_not_posted"
    assert not SalesDocument.objects.filter(customer__employee=s1).exists()


def test_refused_when_the_cost_is_not_the_posted_cost_on_1250(bw):
    c = contract(bw, saudi(bw))
    with pytest.raises(InvoicingError) as exc:
        direct(bw, c, cost="29999.99")
    assert exc.value.code == "cost_mismatch" and "30000.00" in bilingual(exc.value)


def test_refused_when_possession_is_not_recorded_or_after_the_sale(bw):
    c = contract(bw, saudi(bw))
    for overrides in ({"possession_date": None}, {"possession_evidence_ref": "  "},
                      {"possession_date": date(2027, 2, 1)}):  # before the purchase bill
        with pytest.raises(InvoicingError) as exc:
            direct(bw, c, **overrides)
        assert exc.value.code == "possession_not_recorded", overrides
        bilingual(exc.value)
    with pytest.raises(InvoicingError) as exc:  # possession only tomorrow: the sale (today) would come first
        direct(bw, c, possession_date=date(2027, 3, 21))
    assert exc.value.code == "sale_before_possession" and bilingual(exc.value)
    with pytest.raises(InvoicingError) as exc:
        direct(bw, c, sale_date=date(2027, 2, 15))
    assert exc.value.code == "sale_before_possession"


def test_refused_when_the_asset_is_already_sold(sold):
    w, s1, c, doc = sold
    other = contract(w, s1, acquire=False)
    with pytest.raises(InvoicingError) as exc:
        direct(w, other, purchase_bill=c.purchase_bill, possession_date=c.possession_date,
               possession_evidence_ref=c.possession_evidence_ref)
    assert exc.value.code == "asset_already_sold" and bilingual(exc.value)
    assert DeferredSale.objects.filter(purchase_bill=c.purchase_bill).count() == 1


def test_refused_in_a_closed_period_and_without_a_vat_number(bw):
    c = contract(bw, saudi(bw))
    FiscalPeriod.objects.filter(entity=bw.alm, name="2027-03").update(status="closed")
    with pytest.raises(InvoicingError) as exc:
        direct(bw, c)
    assert exc.value.code == "document_invalid" and "2027-03" in bilingual(exc.value)
    FiscalPeriod.objects.filter(entity=bw.alm, name="2027-03").update(status="open")
    Entity.objects.filter(pk=bw.alm.pk).update(vat_number="")
    with pytest.raises(InvoicingError) as exc:
        direct(bw, c)
    assert exc.value.code == "entity_not_vat_registered"
    assert not SalesDocument.objects.filter(deferred_sale__isnull=False).exists()


def test_only_the_sale_approver_with_a_current_right_creates_it(bw):
    c = contract(bw, saudi(bw))
    with pytest.raises(PermissionDenied):  # the payroll officer holds no sale approval
        direct(bw, c, approver=bw.p)
    with pytest.raises(PermissionDenied):
        direct(bw, c, approver=make_agent("sale_bot"))
    from core.access import services as access_services
    from core.access.models import Membership

    for m in Membership.objects.filter(user=bw.system, entity=bw.alm, role__code="system_deferred_sale"):
        access_services.revoke_membership(bw.mp, m, "test: no bai' ajel system user in ALM")
    with pytest.raises(PermissionDenied):  # no system-user membership in the entity: nothing is created
        direct(bw, c)


# --- entity separation ------------------------------------------------------------------------------------------

def sub_bill(w, cost="30000.00"):
    """A posted SUB bill debiting 1250 (test data): another entity's asset."""
    ap, cfo = make_user("sub_ap"), w.c2
    member(ap, w.sub, "payables_clerk")
    vendor = inv.create_vendor(w.sub, user=ap, vendor_kind="business", code="V-SUB", name_en="Sub Dealer",
                               name_ar="معرض الفرع", is_vat_registered=True, vat_number="320000000000003")
    doc = inv.create_purchase_document(w.sub, user=ap, vendor=vendor, vendor_document_number="SUB-CAR-1",
                                       vendor_document_date=date(2027, 2, 15), accounting_date=date(2027, 2, 15))
    inv.add_purchase_line(doc, user=ap, account="1250", quantity="1", unit_price=cost, description_ar="أصل")
    doc.refresh_from_db()
    _, net, charged, _, _, _, _ = inv._purchase_amounts(doc, list(doc.lines.select_related("vat_rate")))
    inv.update_purchase_document(doc, user=ap, vendor_total_vat=charged, vendor_total_gross=net + charged)
    return inv.approve_purchase_document(inv.submit_purchase_document(doc, user=ap), user=cfo)


def test_entity_separation_of_employee_bill_and_approver(bw):
    c = contract(bw, saudi(bw))
    sub_emp = saudi(bw, entity=bw.sub)
    with pytest.raises(InvoicingError) as exc:  # an ALM sale is never invoiced to a SUB employee
        direct(bw, c, employee=sub_emp)
    assert exc.value.code == "entity_mismatch" and bilingual(exc.value)
    with pytest.raises(InvoicingError) as exc:  # nor backed by a SUB purchase bill
        direct(bw, c, purchase_bill=sub_bill(bw))
    assert exc.value.code == "purchase_bill_not_posted"
    outsider = make_user("sub_mp")
    member(outsider, bw.sub, "approver")
    with pytest.raises(PermissionDenied):  # SUB's approver has no right over ALM's contract
        direct(bw, c, approver=outsider)
    assert not SalesDocument.objects.filter(deferred_sale__isnull=False).exists()


# --- no sale line, receipt or note by hand (R112; Sharia: no increase, payroll R48) ---------------------------------

def test_no_bai_ajel_line_or_account_by_hand(sold):
    w, s1, c, doc = sold
    clerk = make_user("ar_clerk")
    member(clerk, w.alm, "receivables_clerk")
    other = inv.create_customer(w.alm, user=clerk, code="C-IND", name_ar="فرد", customer_kind="individual",
                                currency="SAR")
    draft = inv.create_sales_document(w.alm, user=clerk, customer=other, issue_date=date(2027, 3, 20))
    with pytest.raises(InvoicingError) as exc:
        inv.add_sales_line(draft, user=clerk, income_type="deferred_sale", quantity="1", unit_price="10.00",
                           description_ar="بند", income_account="1250")
    assert exc.value.code == "deferred_sale_by_hand" and bilingual(exc.value)
    for account in ("4610", "1241", "1240"):
        with pytest.raises(InvoicingError) as exc:
            inv.add_sales_line(draft, user=clerk, income_type="other", quantity="1", unit_price="10.00",
                               description_ar="بند", income_account=account)
        assert account in bilingual(exc.value)
    bill = inv.create_purchase_document(w.alm, user=w.ap, vendor=w.vendor, vendor_document_number="ANY-1",
                                        vendor_document_date=date(2027, 3, 1), accounting_date=date(2027, 3, 1))
    for account in ("1240", "1241", "4610"):
        with pytest.raises(InvoicingError):
            inv.add_purchase_line(bill, user=w.ap, account=account, quantity="1", unit_price="10.00",
                                  description_ar="بند")
    # 1250 stays open to purchase bills: the company buys assets for deferred sale on it (E9)
    inv.add_purchase_line(bill, user=w.ap, account="1250", quantity="1", unit_price="10.00", description_ar="أصل")


def test_no_note_on_a_bai_ajel_invoice_the_price_never_increases(sold):
    w, s1, c, doc = sold
    clerk = make_user("ar_clerk2")
    member(clerk, w.alm, "receivables_clerk")
    for doc_type in ("debit_note", "credit_note"):
        with pytest.raises(InvoicingError) as exc:
            inv.create_sales_document(w.alm, user=clerk, customer=doc.customer, issue_date=date(2027, 3, 21),
                                      doc_type=doc_type, original_document=doc)
        assert exc.value.code == "deferred_sale_note" and "never increases" in bilingual(exc.value)


def test_no_late_payment_increase_field_or_input_exists():
    banned = ("rate", "interest", "penalty", "late", "fee", "per_annum")
    for model in (DeferredSale, DeferredProfitRelease):
        names = [f.name for f in model._meta.get_fields()]
        assert not [n for n in names if any(b in n for b in banned)], names
    import inspect

    for fn in (inv.create_deferred_sale, inv.recognise_deferred_profit, port_module.PayrollInvoicingPort.create_bai_ajel_sale,
               port_module.PayrollInvoicingPort.recognise_bai_ajel_profit):
        params = inspect.signature(fn).parameters
        assert not [p for p in params if any(b in p for b in banned)], params


def test_the_bai_ajel_receivable_is_collected_by_payroll_not_by_receipts(sold):
    w, s1, c, doc = sold
    clerk = make_user("ar_clerk3")
    member(clerk, w.alm, "receivables_clerk")
    receipt = inv.create_receipt(w.alm, user=clerk, customer=doc.customer, date=date(2027, 3, 20), amount="100.00")
    with pytest.raises(InvoicingError) as exc:
        inv.add_receipt_application(receipt, doc, "100.00", user=clerk)
    assert "1240" in bilingual(exc.value)
    assert inv._allocation_problems(receipt, doc, D("100.00"))
    accountant = make_user("acct")
    member(accountant, w.alm, "accountant")
    rows = inv.reconcile_subledger(w.alm, date(2027, 3, 31), user=accountant)
    assert all(r["difference"] == 0 for r in rows) and not [r for r in rows if r["subledger"] == D("38640.00")]


# --- profit recognised as instalments are collected (payroll E12, E13) -------------------------------------------

def collection_run(w):
    """Test data: one payroll run of ALM that the collections below are deducted by (a draft run row is enough: the
    marker and the installment rows are what an approval leaves; payroll/test_slice2_bai_ajel_real.py runs real
    approvals)."""
    if not hasattr(w, "collect_run"):
        w.collect_run = payroll.create_run(w.alm, user=w.p, period="2027-04")
    return w.collect_run


def next_installment(c):
    return c.installments.filter(status="planned").order_by("period", "pk").first()


def collect(w, c, amount, user=None, on=date(2027, 4, 30), inst=None):
    """What payroll's run approval does for one installment: deduct it (a part leaves a remainder, R18) and, inside
    the approval (payroll's collecting marker), report the collection to invoicing (review fix 4)."""
    from payroll.models import DeferredSaleInstallment

    user = user or w.h
    run = collection_run(w)
    inst = inst or next_installment(c)
    payroll._deduct_installment(inst, D(amount), run, DeferredSaleInstallment)
    with payroll_port.collecting(run, user):
        result = port_module.PayrollInvoicingPort().recognise_bai_ajel_profit(
            c, amount=D(amount), on=on, source_ref=payroll._collection_ref(inst), user=user)
    return result, inst


def test_profit_moves_from_1241_to_4610_as_instalments_are_collected(sold):
    w, s1, c, doc = sold
    port = port_module.PayrollInvoicingPort()
    first, inst = collect(w, c, "1610.00", on=date(2027, 4, 25))
    assert (first.profit_amount, first.collected_to_date) == (D("150.00"), D("1610.00")) and first.entry_ref
    ref = payroll._collection_ref(inst)
    release = DeferredProfitRelease.objects.get(source_ref=ref)
    assert release.created_by == w.h and release.journal_entry.posted_by == w.system
    assert entry_by_account(release.journal_entry) == {"1241": D("150.00"), "4610": D("-150.00")}
    with payroll_port.collecting(collection_run(w), w.h):  # idempotent per collection; a changed amount is refused
        assert port.recognise_bai_ajel_profit(c, amount=D("1610.00"), on=date(2027, 4, 25), source_ref=ref,
                                              user=w.h).entry_ref == first.entry_ref
        with pytest.raises(InvoicingError) as exc:
            port.recognise_bai_ajel_profit(c, amount=D("1000.00"), on=date(2027, 4, 25), source_ref=ref, user=w.h)
    assert exc.value.code == "collection_not_from_payroll"  # 1,000.00 is not what that installment collected
    for _ in range(2, 25):  # the rest of the term, some months late: no profit accrues for time (R48)
        collect(w, c, "1610.00", on=date(2027, 12, 1))
    assert balance(w.alm, "1241") == D("0.00") and balance(w.alm, "4610") == D("-3600.00")
    assert balance(w.alm, "1240") == D("38640.00")  # payroll credits 1240 itself when it deducts (E11)


def test_uneven_collections_round_once_and_the_last_releases_the_rest(bw, django_capture_on_commit_callbacks):
    c = approve(bw, contract(bw, saudi(bw), term=1), django_capture_on_commit_callbacks)  # one instalment 38,640.00
    bw.zatca.report(sale_doc(c))
    a, inst = collect(bw, c, "1000.00", on=date(2027, 4, 25))
    assert a.profit_amount == D("93.17")  # 3,600 x 1,000 / 38,640 = 93.167, half-up
    b, inst = collect(bw, c, "1000.00", on=date(2027, 5, 25))  # the remainder installment payroll carried (R18)
    assert b.profit_amount == D("93.17")  # 3,600 x 2,000 / 38,640 = 186.335 -> 186.34 to date
    rest, _ = collect(bw, c, "36640.00", on=date(2027, 6, 30))  # the whole balance (E13): the rest is recognised
    assert rest.profit_amount == D("3413.66") and balance(bw.alm, "1241") == D("0.00")
    from payroll.models import DeferredSaleInstallment

    extra = DeferredSaleInstallment(entity=bw.alm, contract=c, period="2028-01", amount=D("0.01"))
    extra._service_write = True
    extra.save()
    with pytest.raises(InvoicingError) as exc:  # test data: more than the receivable is never recognised
        collect(bw, c, "0.01", inst=extra)
    assert exc.value.code == "over_collected" and bilingual(exc.value)


def test_profit_waits_for_the_posted_invoice_and_for_the_collecting_approver(bw, django_capture_on_commit_callbacks):
    c = approve(bw, contract(bw, saudi(bw)), django_capture_on_commit_callbacks)
    with pytest.raises(InvoicingError) as exc:
        collect(bw, c, "1610.00")
    assert exc.value.code == "sale_not_posted" and bilingual(exc.value)
    bw.zatca.report(sale_doc(c))
    port = port_module.PayrollInvoicingPort()
    inst = next_installment(c)
    run = collection_run(bw)
    from payroll.models import DeferredSaleInstallment

    payroll._deduct_installment(inst, D("1610.00"), run, DeferredSaleInstallment)
    ref = payroll._collection_ref(inst)
    for user in (bw.p, bw.hs):  # another person than the approver whose marker is set
        with payroll_port.collecting(run, bw.h), pytest.raises(InvoicingError) as exc:
            port.recognise_bai_ajel_profit(c, amount=D("1610.00"), on=date(2027, 4, 25), source_ref=ref, user=user)
        assert exc.value.code == "collection_not_from_payroll"
    with payroll_port.collecting(run, bw.p), pytest.raises(PermissionDenied):  # P holds no run approval
        port.recognise_bai_ajel_profit(c, amount=D("1610.00"), on=date(2027, 4, 25), source_ref=ref, user=bw.p)
    agent = make_agent("profit_bot")
    with payroll_port.collecting(run, agent), pytest.raises(PermissionDenied):
        port.recognise_bai_ajel_profit(c, amount=D("1610.00"), on=date(2027, 4, 25), source_ref=ref, user=agent)
    with payroll_port.collecting(run, bw.h), pytest.raises(InvoicingError) as exc:  # not an installment reference
        port.recognise_bai_ajel_profit(c, amount=D("1610.00"), on=date(2027, 4, 25), source_ref="inst:1", user=bw.h)
    assert exc.value.code == "collection_not_from_payroll"
    assert not DeferredProfitRelease.objects.exists()
    other = contract(bw, saudi(bw), acquire=False)
    with payroll_port.collecting(run, bw.h), pytest.raises(InvoicingError) as exc:  # the installment is c's, not other's
        port.recognise_bai_ajel_profit(other, amount=D("1610.00"), on=date(2027, 4, 25), source_ref=ref, user=bw.h)
    assert exc.value.code == "collection_not_from_payroll"


def test_review4_the_cfo_calling_profit_recognition_directly_is_refused(sold):
    """Review fix 4: outside payroll's run or settlement approval nobody recognises profit, not even a person holding
    every collecting right (the CFO / approver C), with a plausible installment reference and amount."""
    w, s1, c, doc = sold
    from payroll.models import DeferredSaleInstallment

    inst = next_installment(c)
    payroll._deduct_installment(inst, D("1610.00"), collection_run(w), DeferredSaleInstallment)
    member(w.c, w.alm, "hr_officer")  # C now also holds payroll.approve_run
    for call in (lambda: inv.recognise_deferred_profit(c, user=w.c, collected_amount="1610.00",
                                                       collected_on=date(2027, 4, 25),
                                                       source_ref=payroll._collection_ref(inst)),
                 lambda: port_module.PayrollInvoicingPort().recognise_bai_ajel_profit(
                     c, amount=D("1610.00"), on=date(2027, 4, 25), source_ref=payroll._collection_ref(inst),
                     user=w.c)):
        with pytest.raises(InvoicingError) as exc:
            call()
        assert exc.value.code == "collection_not_from_payroll" and bilingual(exc.value)
    assert not DeferredProfitRelease.objects.exists() and balance(w.alm, "4610") == D("0.00")
    from core.access.models import AccessLog

    assert AccessLog.objects.filter(event="permission_denied", actor=w.c,
                                    details__reason="collection_not_from_payroll").exists()


def test_a_reversed_collection_reverses_its_profit_and_can_be_collected_again(sold):
    w, s1, c, doc = sold
    port = port_module.PayrollInvoicingPort()
    _, inst = collect(w, c, "1610.00", on=date(2027, 4, 25))
    ref, run = payroll._collection_ref(inst), collection_run(w)
    with pytest.raises(InvoicingError) as exc:  # outside a run reversal (review fix 4)
        port.reverse_bai_ajel_profit(c, source_ref=ref, user=w.c)
    assert exc.value.code == "collection_not_from_payroll"
    with payroll_port.collecting(run, w.p, mode="reverse"), pytest.raises(PermissionDenied):
        port.reverse_bai_ajel_profit(c, source_ref=ref, user=w.p)  # the payroll officer cannot reverse runs
    with payroll_port.collecting(run, w.c, mode="reverse"):
        result = port.reverse_bai_ajel_profit(c, source_ref=ref, user=w.c, reason="run reversed")
    release = DeferredProfitRelease.objects.get(source_ref=ref)
    assert result.reversed and release.status == "reversed" and release.reversal_entry.reversal_of == release.journal_entry
    assert balance(w.alm, "4610") == D("0.00") and balance(w.alm, "1241") == D("-3600.00")
    with payroll_port.collecting(run, w.c, mode="reverse"), pytest.raises(InvoicingError) as exc:
        port.reverse_bai_ajel_profit(c, source_ref=ref, user=w.c)
    assert exc.value.code == "nothing_to_reverse"
    with payroll_port.collecting(run, w.h):
        again = port.recognise_bai_ajel_profit(c, amount=D("1610.00"), on=date(2027, 5, 25), source_ref=ref, user=w.h)
    assert again.profit_amount == D("150.00")
    from ledger import services as ledger

    with pytest.raises(PermissionDenied):  # never reversed behind invoicing's back (reverse_guard)
        g = access.require(w.c, w.alm, "payroll.reverse_run",
                           document=DeferredProfitRelease.objects.get(source_ref=ref, status="posted"))
        ledger.reverse_entry(DeferredProfitRelease.objects.get(source_ref=ref, status="posted").journal_entry, g)


def test_a_profit_entry_posts_only_through_invoicing(sold):
    w, s1, c, doc = sold
    collect(w, c, "1610.00", on=date(2027, 4, 25))
    release = DeferredProfitRelease.objects.get()
    with pytest.raises(ValidationError):
        inv._deferred_profit_lines(release)  # outside an invoicing posting
    with pytest.raises(ValidationError):
        release.save()  # service-written only
    with pytest.raises(ValidationError):
        DeferredSale.objects.filter(pk=release.deferred_sale_id).update(cost=D("1.00"))
    with pytest.raises(ValidationError):
        DeferredSale._base_manager.filter(pk=release.deferred_sale_id).update(cost=D("1.00"))
    with pytest.raises(ValidationError):
        DeferredProfitRelease._base_manager.filter(pk=release.pk).delete()
    with pytest.raises(ValidationError):
        release.delete()


# --- access catalogue (seed V15) ------------------------------------------------------------------------------

@pytest.mark.django_db
def test_access_v15_bai_ajel_system_user_and_rights():
    create, post = catalogue.get("invoicing.create_deferred_sale"), catalogue.get("invoicing.post_deferred_profit")
    assert (create.system, post.system, post.posts, create.posts) == ("deferred_sale", "deferred_sale", True, False)
    assert "invoicing.post_deferred_profit" in catalogue.DOCUMENT_POST_PERMISSIONS
    assert catalogue.role_may_hold("human", "entity", None, create) == "system_only"
    role = Role.objects.get(code="system_deferred_sale")
    assert (role.kind, role.system_code) == ("system", "deferred_sale")
    assert role.permission_codes == frozenset(ROLE_SEED_V15["system_deferred_sale"][5]) == {
        "invoicing.create_deferred_sale", "invoicing.post_deferred_profit"}
    user = access.system_user("deferred_sale")
    assert user.username == SYSTEM_USERS_V2["deferred_sale"][0] and not user.has_usable_password()
    assert not any(p.startswith("payroll.") for p in role.permission_codes)  # payroll R38
    assert IncomeType.objects.get(code="deferred_sale").default_vat_category == "S"
    assert IncomeType.objects.get(code="deferred_sale").default_account_code == ""


# --- PostgreSQL guards -------------------------------------------------------------------------------------------

@pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL triggers")
def test_pg_bai_ajel_rows_are_locked_in_the_database(sold):
    from django.db import DatabaseError, transaction

    w, s1, c, doc = sold
    collect(w, c, "1610.00", on=date(2027, 4, 25))
    release = DeferredProfitRelease.objects.get()
    sale = DeferredSale.objects.get(sales_document=doc)
    for sql, params in (
        ("UPDATE invoicing_deferredsale SET cost = 1 WHERE id = %s", [sale.pk]),
        ("UPDATE invoicing_deferredsale SET active = true, profit_amount = 0 WHERE id = %s", [sale.pk]),
        ("DELETE FROM invoicing_deferredsale WHERE id = %s", [sale.pk]),
        ("UPDATE invoicing_deferredprofitrelease SET profit_amount = 1 WHERE id = %s", [release.pk]),
        ("UPDATE invoicing_deferredprofitrelease SET status = 'reversed', reversed_at = now(), reversed_by_id = "
         "created_by_id WHERE id = %s", [release.pk]),
        ("DELETE FROM invoicing_deferredprofitrelease WHERE id = %s", [release.pk]),
    ):
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(sql, params)
    # Review fix 9 (invoicing 0031): deactivation only once ZATCA rejected the sale invoice; this one is final.
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("UPDATE invoicing_deferredsale SET active = false WHERE id = %s", [sale.pk])


@pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL triggers")
def test_pg_review9_a_sale_deactivates_only_when_its_invoice_is_rejected(bw, django_capture_on_commit_callbacks):
    from django.db import DatabaseError, transaction

    c = approve(bw, contract(bw, saudi(bw)), django_capture_on_commit_callbacks)
    sale = DeferredSale.objects.get(source_id=c.pk, active=True)
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:  # issued, not rejected
        cursor.execute("UPDATE invoicing_deferredsale SET active = false WHERE id = %s", [sale.pk])
    bw.zatca.reject(sale_doc(c))
    with transaction.atomic(), connection.cursor() as cursor:  # the one change allowed: a rejected invoice
        cursor.execute("UPDATE invoicing_deferredsale SET active = false WHERE id = %s", [sale.pk])
    assert DeferredSale.objects.get(pk=sale.pk).active is False
