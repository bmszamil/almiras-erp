"""Fixtures for the web UI tests (web.md §8): anonymised entities and users only, made-up identifiers.

Entities: ALM (group parent), SUB (``full``), PART (``full``, 60 %), MIN (``equity``), INACT (inactive ``full`` child).
Users: accountant in ALM (``acc``), accountant in SUB + viewer in MIN (``u``), group viewer on ALM (``g``), viewer in
ALM (``viewer``), HR officer in ALM (``hr``, no ``invoicing.view``), receivables clerk (``clerk``) and approver
(``cfo``) in ALM, accountant in INACT (``inact``), a superuser without memberships (``su``), an agent and the
billing system user. Data: journal entries in ALM and SUB, sales documents in ALM (business and individual buyer)
and SUB.
"""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from core import access
from core.access.testing import make_agent, make_user, member
from core.models import Entity
from invoicing import services as invoicing
from invoicing.conftest import ADDRESS, make_entity
from invoicing.testing import fake_zatca
from ledger import services as ledger

PASSWORD = "made-up-test-password-7Q!"  # set on fixture users only; never a real credential
CASH = [{"account": "1100", "debit": Decimal("1250.50")}, {"account": "3100", "credit": Decimal("1250.50")}]


def person(username, *roles, language="ar", password=True):
    user = make_user(username, language=language)
    for entity, role in roles:
        member(user, entity, role)
    if password:
        user.set_password(PASSWORD)
        user.save(update_fields=["password"])
    return user


def entry(entity, user, description="Manual journal", lines=CASH, on=date(2027, 1, 10)):
    return ledger.create_entry(entity, on, description, lines, user=user)


def draft_invoice(entity, user, customer, price="1000.00", on=date(2027, 1, 10), submit=False):
    doc = invoicing.create_sales_document(entity, user=user, customer=customer, issue_date=on, supply_date=on)
    invoicing.add_sales_line(doc, user=user, income_type="rent_commercial", quantity="1", unit_price=price,
                             description_ar="بند اختبار", description_en="Test line")
    return invoicing.submit_sales_document(doc, user=user) if submit else doc


@pytest.fixture
def zatca():
    with fake_zatca() as port:
        yield port


@pytest.fixture
def w(db, zatca):
    alm = make_entity("ALM", "parent", vat="300000000000003", cr="1010000001", unified="7000000001")
    sub = make_entity("SUB", "full", alm, vat="311111111100003", cr="1010000002")
    # No records in PART and MIN: no chart of accounts needed (keeps the fixture fast).
    part = Entity.objects.create(code="PART", name_en="Part Owned Co", name_ar="شركة مملوكة جزئيًا",
                                 consolidation="full", parent=alm, ownership_pct=Decimal("60"))
    minority = Entity.objects.create(code="MIN", name_en="Minority Co", name_ar="شركة أقلية", consolidation="equity",
                                     parent=alm, ownership_pct=Decimal("20"))
    inact = Entity.objects.create(code="INACT", name_en="Dormant Co", name_ar="شركة خاملة", consolidation="full",
                                  parent=alm, is_active=False)
    ns = SimpleNamespace(alm=alm, sub=sub, part=part, min=minority, inact=inact, zatca=zatca)
    ns.acc = person("acc", (alm, "accountant"))
    ns.u = person("u", (sub, "accountant"), (minority, "viewer"))
    ns.g = person("g", (alm, "group_viewer"))
    ns.viewer = person("viewer", (alm, "viewer"))
    ns.hr = person("hr", (alm, "hr_officer"))
    ns.clerk = person("clerk", (alm, "receivables_clerk"))
    ns.cfo = person("cfo", (alm, "approver"))
    ns.inact_acc = person("inact_acc", (inact, "accountant"))
    ns.sub_clerk = person("sub_clerk", (sub, "receivables_clerk"))
    ns.su = person("su")
    ns.su.is_superuser = ns.su.is_staff = True
    ns.su.save(update_fields=["is_superuser", "is_staff"])
    ns.agent = make_agent("invoice_reader")
    ns.billing = access.system_user("billing")
    queue = access.system_user("zatca_queue")
    for e in (alm, sub):
        member(queue, e, "system_zatca_queue")

    # Journal entries: ALM has two drafts and one posted entry; SUB has one draft.
    ns.alm_draft = entry(alm, ns.acc, "ALM rent accrual")
    ns.alm_draft2 = entry(alm, ns.acc, "ALM second draft")
    to_post = entry(alm, ns.acc, "ALM posted entry")
    ns.alm_posted = ledger.post_entry(to_post, access.require(ns.cfo, alm, "ledger.post_entry", document=to_post))
    ns.sub_entry = entry(sub, ns.u, "SUB-ONLY-SECRET description")

    # Sales documents: a business buyer and an individual buyer in ALM; one in SUB.
    ns.customer = invoicing.create_customer(
        alm, user=ns.clerk, code="C001", name_en="Test Buyer LLC", name_ar="شركة المشتري التجريبية",
        is_vat_registered=True, vat_number="310000000000003", **ADDRESS,
    )
    ns.individual = invoicing.create_customer(
        alm, user=ns.clerk, code="P001", name_en="Made Up Person", name_ar="شخص مختلق", customer_kind="individual",
        **ADDRESS,
    )
    ns.sub_customer = invoicing.create_customer(
        sub, user=ns.sub_clerk, code="S001", name_en="SUB-ONLY-SECRET Buyer", name_ar="مشتري خاص بالتابعة",
        is_vat_registered=True, vat_number="310000000100003", **ADDRESS,
    )
    ns.alm_invoice = draft_invoice(alm, ns.clerk, ns.customer, submit=True)
    ns.alm_person_invoice = draft_invoice(alm, ns.clerk, ns.individual, price="250.00")
    ns.sub_invoice = draft_invoice(sub, ns.sub_clerk, ns.sub_customer, price="999.00")
    return ns


@pytest.fixture
def signed_in(client):
    """``signed_in(user)``: the test client signed in through the real sign-in form (R12, R17)."""

    def sign_in(user):
        response = client.post("/login/", {"username": user.username, "password": PASSWORD})
        assert response.status_code == 302, response.content[:500]
        return client

    return sign_in

