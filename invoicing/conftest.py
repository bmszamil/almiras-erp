"""Fixtures for invoicing tests (invoicing.md §8): anonymised entities ALM and SUB, users per role.

All identifiers are made up (never the D10 values, R70); amounts are Decimal.
"""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from core import access
from core.access.testing import make_user, member
from core.models import Entity
from invoicing import services
from invoicing.testing import fake_zatca
from ledger.models import ExchangeRate
from ledger.services_setup import create_monthly_periods, install_chart_of_accounts

D = Decimal
SELLER = dict(
    legal_form="llc", building_no="1234", street_en="Test Street", street_ar="شارع الاختبار",
    district_en="Test District", district_ar="حي الاختبار", postal_code="12345", city_en="Riyadh", city_ar="الرياض",
    country="SA", vat_effective_date=date(2024, 1, 1), vat_return_period="quarterly",
)


def make_entity(code, consolidation="full", parent=None, vat="", cr="", unified="", **extra):
    fields = {**SELLER, **extra}
    e = Entity.objects.create(
        code=code, name_en=f"{code} Co", name_ar=f"شركة {code}", legal_name_en=f"{code} Test Company",
        legal_name_ar=f"شركة {code} للاختبار", consolidation=consolidation, parent=parent, vat_number=vat,
        cr_number=cr, unified_number=unified, **fields,
    )
    install_chart_of_accounts(e)
    create_monthly_periods(e, 2027)
    return e


@pytest.fixture
def zatca():
    with fake_zatca() as port:
        yield port


@pytest.fixture
def w(db, zatca):
    alm = make_entity("ALM", "parent", vat="300000000000003", cr="1010000001", unified="7000000001")
    sub = make_entity("SUB", "full", alm, vat="311111111100003", cr="1010000002")
    users = {}
    for name, role, entities in (
        ("clerk", "receivables_clerk", (alm,)), ("clerk2", "receivables_clerk", (alm,)),
        ("ap", "payables_clerk", (alm,)), ("accountant", "accountant", (alm,)), ("cfo", "approver", (alm,)),
        ("sub_clerk", "receivables_clerk", (sub,)), ("sub_cfo", "approver", (sub,)), ("viewer", "viewer", (alm,)),
    ):
        user = make_user(name)
        for e in entities:
            member(user, e, role)
        users[name] = user
    queue = access.system_user("zatca_queue")
    member(queue, alm, "system_zatca_queue")
    member(queue, sub, "system_zatca_queue")
    ns = SimpleNamespace(alm=alm, sub=sub, queue=queue, zatca=zatca, **users)
    ns.customer = services.create_customer(
        alm, user=ns.clerk, code="C001", name_en="Test Buyer LLC", name_ar="شركة المشتري التجريبية",
        is_vat_registered=True, vat_number="310000000000003", street_en="Buyer Street", street_ar="شارع المشتري",
        building_no="2345", district_en="Buyer District", district_ar="حي المشتري", city_en="Jeddah",
        city_ar="جدة", postal_code="23456", country="SA",
    )
    ns.vendor = services.create_vendor(
        alm, user=ns.ap, vendor_kind="business", code="V001", name_en="Test Supplier", name_ar="المورد التجريبي", is_vat_registered=True,
        vat_number="320000000000003",
    )
    return ns


def invoice(w, lines=(("rent_commercial", "1", "1000.00"),), *, customer=None, user=None, issue_date=date(2027, 1, 10),
            entity=None, **fields):
    """A submitted sales document with ``lines`` of (income type, quantity, unit price[, extra kwargs])."""
    entity = entity or w.alm
    user = user or w.clerk
    fields.setdefault("supply_date", issue_date)
    doc = services.create_sales_document(entity, user=user, customer=customer or w.customer, issue_date=issue_date,
                                         **fields)
    for spec in lines:
        income_type, qty, price, *extra = spec
        services.add_sales_line(doc, user=user, income_type=income_type, quantity=qty, unit_price=price,
                                description_ar="بند اختبار", description_en="Test line", **(extra[0] if extra else {}))
    return services.submit_sales_document(doc, user=user)


def approve(w, doc, user=None, django_capture_on_commit_callbacks=None):
    """CFO approval; the ZATCA hand-off runs after commit (R40)."""
    from django.db import connection

    from invoicing.models import SalesDocument
    from invoicing.testing import approval_day

    user = user or w.cfo
    callbacks_before = len(connection.run_on_commit)
    with approval_day(SalesDocument.objects.get(pk=doc.pk).issue_date):  # approved on the day the draft asks for
        result = services.approve_sales_document(doc, user=user)
    for _, callback, _ in connection.run_on_commit[callbacks_before:]:  # what commit would run
        callback()
    del connection.run_on_commit[callbacks_before:]
    stored = SalesDocument.objects.get(pk=result.pk)
    stored.warnings = result.warnings
    return stored


def final(w, doc, approver=None):
    issued = approve(w, doc, approver)
    return w.zatca.clear(issued) if issued.zatca_profile == "standard" else w.zatca.report(issued)


ADDRESS = dict(street_en="Buyer Street", street_ar="شارع المشتري", building_no="2345", district_en="Buyer District",
               district_ar="حي المشتري", city_en="Jeddah", city_ar="جدة", postal_code="23456", country="SA")


def usd_rate(w, rate="3.75", on=date(2027, 1, 10), entity=None, source="sama", currency="USD"):
    """A group-wide rate row; by default a SAMA row, so it is both the booking rate (R12) and the VAT SAR rate on
    the tax point (R100, G7)."""
    from core.access.testing import member as m

    treasurer = make_user(f"treasury{ExchangeRate.objects.count()}")
    m(treasurer, w.alm, "group_treasury")
    from ledger import services as ledger

    return ledger.create_rate(user=treasurer, from_currency=currency, to_currency="SAR", on=on, rate=D(rate),
                              group=w.alm, source=source,
                              source_reference=f"SAMA test bulletin {on}" if source == "sama" else "")
