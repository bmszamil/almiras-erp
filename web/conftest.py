"""Fixtures for the web UI tests (web.md §8): anonymised entities and users only, made-up identifiers.

Entities: ALM (group parent), SUB (``full``), PART (``full``, 60 %), MIN (``equity``), INACT (inactive ``full`` child).
Users: accountant in ALM (``acc``); accountant in SUB + viewer in MIN (``u``); accountants in PART (``part_acc``) and
MIN (``min_acc``); group viewer on ALM (``g``); viewer in ALM (``viewer``); HR officer in ALM (``hr``, no
``invoicing.view``); receivables clerk (``clerk``) and approver (``cfo``) in ALM; accountant in INACT (``inact_acc``);
a superuser without memberships (``su``); an agent and the billing system user. Data: journal entries in ALM, SUB,
PART and MIN; sales documents in ALM (business and individual buyer), SUB and PART. Every record outside ALM carries
"<CODE>-ONLY-SECRET", so a leak into another entity's page shows.
"""
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pyotp
import pytest
from django.utils import timezone

from core import access
from core.access.testing import make_agent, make_user, member
from core.models import Entity
from invoicing import services as invoicing
from invoicing.conftest import ADDRESS, make_entity
from invoicing.testing import fake_zatca
from ledger import services as ledger
from web import mfa as web_mfa
from web.models import MfaDevice

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
    part = make_entity("PART", "full", alm, vat="322222222200003", ownership_pct=Decimal("60"))
    minority = make_entity("MIN", "equity", alm, ownership_pct=Decimal("20"))
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
    ns.part_acc = person("part_acc", (part, "accountant"))
    ns.min_acc = person("min_acc", (minority, "accountant"))
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
    # PART and MIN have records too, so a leak into another entity's pages would show (review item 11).
    ns.part_entry = entry(part, ns.part_acc, "PART-ONLY-SECRET description")
    ns.min_entry = entry(minority, ns.min_acc, "MIN-ONLY-SECRET description")

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
    ns.part_customer = invoicing.create_customer(
        part, user=ns.part_acc, code="PT01", name_en="PART-ONLY-SECRET Buyer", name_ar="مشتري خاص بالشركة الجزئية",
        is_vat_registered=True, vat_number="310000000300003", **ADDRESS,
    )
    ns.part_invoice = draft_invoice(part, ns.part_acc, ns.part_customer, price="777.00")
    return ns


class Clock:
    """The MFA clock of a test (``web.mfa._now``): starts now, moves only when a test moves it."""

    def __init__(self):
        self.at = timezone.now()

    def __call__(self):
        return self.at

    def tick(self, seconds=30):
        self.at += timedelta(seconds=seconds)


_CLOCK = []


@pytest.fixture(autouse=True)
def mfa_clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(web_mfa, "_now", clock)
    _CLOCK[:] = [clock]
    yield clock
    _CLOCK.clear()


def next_code(user):
    """A right code for ``user``'s device in the next 30-second step of the test clock (each step is usable once)."""
    clock = _CLOCK[0]
    clock.tick(30)
    return pyotp.TOTP(MfaDevice.objects.get(user=user).secret).at(clock.at)


def enrol(user):
    """Give ``user`` a confirmed device through the MFA service (as the enrolment page does); returns the codes."""
    web_mfa.start_enrolment(user)
    _device, codes = web_mfa.confirm_enrolment(user, next_code(user))
    return codes


@pytest.fixture
def periods_now(w):
    """Approving a sales document issues it today (invoicing): open fiscal periods for this year too."""
    from ledger.services_setup import create_monthly_periods

    year = timezone.localdate().year
    if year != 2027:
        for entity in (w.alm, w.sub, w.part):
            create_monthly_periods(entity, year)
    return year


@pytest.fixture
def signed_in(client):
    """``signed_in(user)``: the test client signed in through the real sign-in form (R12, R17) and, for a user who
    needs MFA (R13), enrolled if needed and verified through the real verification form (R14)."""

    def sign_in(user):
        response = client.post("/login/", {"username": user.username, "password": PASSWORD})
        assert response.status_code == 302, response.content[:500]
        if web_mfa.must_verify(user):
            if web_mfa.device_of(user) is None:
                enrol(user)
            response = client.post("/mfa/verify/", {"code": next_code(user)})
            assert response.status_code == 302, response.content[:500]
        return client

    return sign_in

