"""Fixtures for the VAT slice of tax-returns (tax-returns.md §8). Anonymised entities and made-up VAT numbers only
(never the setup-data values, R70); amounts are Decimal; nothing calls ZATCA (fake port from invoicing.testing).

Entities: ALM (parent, quarterly, VAT-registered), SUB (100% ALM, monthly), NOV (no VAT number).
Users: a1, a2 (accountant), c1 (approver, also approves the invoices and bills), v1 (viewer), clerk / ap (invoicing
clerks), sub_a (accountant in SUB only).
"""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from core import access
from core.access.testing import make_user, member
from invoicing import services as invoicing
from invoicing.conftest import approve as approve_sales  # noqa: F401  (re-exported for the tests)
from invoicing.conftest import final, invoice, make_entity
from invoicing.testing import fake_zatca
from ledger import services as ledger
from ledger.models import FiscalPeriod
from ledger.services_setup import create_monthly_periods
from tax import services

D = Decimal
PDF = b"%PDF-1.4 test receipt"


def pdf(name="receipt.pdf"):
    return SimpleUploadedFile(name, PDF, content_type="application/pdf")


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)


@pytest.fixture
def zatca():
    with fake_zatca() as port:
        yield port


@pytest.fixture
def today(monkeypatch):
    """Move the tax clock: ``today.set(date)``; default 2027-04-10 (after Q1 2027)."""
    state = SimpleNamespace(value=date(2027, 4, 10))
    monkeypatch.setattr(services, "_today", lambda: state.value)
    state.set = lambda d: setattr(state, "value", d)
    return state


@pytest.fixture
def t(db, zatca, today):
    alm = make_entity("ALM", "parent", vat="300000000000003", cr="1010000001", unified="7000000001")
    sub = make_entity("SUB", "full", alm, vat="311111111100003", cr="1010000002", vat_return_period="monthly")
    nov = make_entity("NOV", "full", alm, vat="", cr="1010000003", vat_return_period="")
    for e in (alm, sub):
        create_monthly_periods(e, 2026)
    users = {}
    for name, role, entities in (
        ("a1", "accountant", (alm,)), ("a2", "accountant", (alm,)), ("c1", "approver", (alm,)),
        ("v1", "viewer", (alm,)), ("clerk", "receivables_clerk", (alm,)), ("ap", "payables_clerk", (alm,)),
        ("sub_a", "accountant", (sub,)), ("sub_c", "approver", (sub,)), ("nov_a", "accountant", (nov,)),
        ("mp", "managing_partner", (alm,)),
    ):
        user = make_user(name)
        for e in entities:
            member(user, e, role)
        users[name] = user
    queue = access.system_user("zatca_queue")
    for e in (alm, sub):
        member(queue, e, "system_zatca_queue")
    ns = SimpleNamespace(alm=alm, sub=sub, nov=nov, queue=queue, zatca=zatca, today=today, **users)
    for e, approver in ((alm, ns.c1), (sub, ns.sub_c)):  # G4: VAT returns in the ERP start on 2027-01-01
        services.change_tax_settings(e, user=approver, vat_golive_date=date(2027, 1, 1))
    ns.cfo = ns.c1  # invoicing helpers approve with w.cfo
    ns.customer = invoicing.create_customer(
        alm, user=ns.clerk, code="C001", name_en="Test Buyer LLC", name_ar="شركة المشتري التجريبية",
        is_vat_registered=True, vat_number="310000000000003", street_en="Buyer Street", street_ar="شارع المشتري",
        building_no="2345", district_en="Buyer District", district_ar="حي المشتري", city_en="Jeddah",
        city_ar="جدة", postal_code="23456", country="SA",
    )
    ns.vendor = invoicing.create_vendor(
        alm, user=ns.ap, vendor_kind="business", code="V001", name_en="Test Supplier", name_ar="المورد التجريبي",
        is_vat_registered=True, vat_number="320000000000003",
    )
    ns.unregistered = invoicing.create_vendor(
        alm, user=ns.ap, vendor_kind="business", code="V002", name_en="Small Supplier", name_ar="مورد صغير",
        is_vat_registered=False,
    )
    ns.foreign = invoicing.create_vendor(
        alm, user=ns.ap, vendor_kind="business", code="V003", name_en="Foreign Supplier", name_ar="مورد أجنبي",
        is_resident=False, residence_country="GB",
    )
    return ns


def sale(t, lines=(("rent_commercial", "1", "10000.00"),), on=date(2027, 1, 10), **fields):
    """A final (cleared) sales document of ALM."""
    return final(t, invoice(t, lines, issue_date=on, **fields))


def bill(t, lines=(("5300", "1", "10000.00"),), *, vendor=None, number=None, on=date(2027, 1, 15), **fields):
    """A posted bill of ALM: lines of (account, qty, price[, kwargs]); approved by c1."""
    vendor = vendor or t.vendor
    number = number or f"B-{on:%m%d}-{vendor.code}-{len(lines)}-{lines[0][2]}"
    doc = invoicing.create_purchase_document(t.alm, user=t.ap, vendor=vendor, vendor_document_number=number,
                                             vendor_document_date=on, accounting_date=on, **fields)
    for account, qty, price, *extra in lines:
        invoicing.add_purchase_line(doc, user=t.ap, account=account, quantity=qty, unit_price=price,
                                    description_ar="بند مشتريات", **(extra[0] if extra else {}))
    doc.refresh_from_db()
    _, net, charged, _, _, _, _ = invoicing._purchase_amounts(doc, list(doc.lines.select_related("vat_rate")))
    invoicing.update_purchase_document(doc, user=t.ap, vendor_total_vat=charged, vendor_total_gross=net + charged)
    invoicing.submit_purchase_document(doc, user=t.ap)
    return invoicing.approve_purchase_document(doc, user=t.c1)


def journal(t, on, lines, entity=None):
    """A posted manual journal (drafted by a1, posted by c1)."""
    entity = entity or t.alm
    entry = ledger.create_entry(entity, on, "Manual VAT journal / قيد يدوي", lines, user=t.a1)
    return ledger.post_entry(entry, access.require(t.c1, entity, "ledger.post_entry", document=entry))


def close(t, *names, entity=None):
    entity = entity or t.alm
    for name in names:
        ledger.close_period(FiscalPeriod.objects.get(entity=entity, name=name), user=t.c1)


def close_q1(t):
    close(t, "2027-01", "2027-02", "2027-03")


def prepared(t, period_start=date(2027, 1, 1), entity=None, user=None):
    """Computed and submitted for review by a1."""
    ret = services.compute_draft(entity or t.alm, period_start, user=user or t.a1)
    return services.submit_for_review(ret, user=user or t.a1)


def confirm_tax_points(t, ret, reviewer=None):
    """C10: invoicing stores no tax point yet, so the invoices of a return are put in the period of their issue
    date and listed in one ``tax_point_fallback`` exception; the reviewer confirms it."""
    for e in services._exceptions(ret)[0]:
        if e["kind"] == "tax_point_fallback" and e["ack"] is None:
            services.acknowledge_exception(ret, user=reviewer or t.a2, kind=e["kind"], reference=e["reference"],
                                           decision="tax_point_confirmed", reason_ar="تاريخ التوريد هو تاريخ الإصدار",
                                           reason_en="Supply date is the issue date")
    return ret


def reviewed(t, ret, reviewer=None):
    confirm_tax_points(t, ret, reviewer)
    return services.complete_review(ret, user=reviewer or t.a2)


def imported(t, period_start, boxes=None, *, entity=None, ref=None, filed_on=None, **fields):
    """R12 (review item 3): a legacy return entered by a1, reviewed by a2 and approved by c1 (filed)."""
    entity = entity or t.alm
    ret = services.import_filed_return(entity, user=t.a1, period_start=period_start, boxes=boxes or {},
                                       zatca_filing_reference=ref or f"FALCON-{period_start}",
                                       filed_on=filed_on or date(2026, 12, 20), **fields)
    ret = services.submit_for_review(ret, user=t.a1)
    ret = services.complete_review(ret, user=t.a2)
    return services.approve_return(ret, user=t.c1)


def approved(t, period_start=date(2027, 1, 1)):
    """Prepared by a1, reviewed by a2, approved by c1 (Q1 periods must be closed by the caller)."""
    return services.approve_return(reviewed(t, prepared(t, period_start)), user=t.c1)


def filed(t, ret, on=date(2027, 4, 20), ref="ZATCA-TEST-1"):
    return services.record_filing(ret, user=t.a1, zatca_filing_reference=ref, filed_on=on, acknowledgement=pdf())


def box(ret, n):
    b = ret.boxes.get(box_no=n)
    return (b.amount_sar, b.adjustment_sar, b.vat_sar)
