"""Fixtures for billing tests (billing.md §8): anonymised entities ALM (parent, Sharia enforced, VAT number) and SUB
(child, own VAT number), template chart, 2027 periods open, users per role, the billing and ZATCA queue system
users with memberships, the invoicing fake (``billing.testing``) and the ZATCA fake (``invoicing.testing``).

All identifiers are made up (never real values, R70); amounts are Decimal strings.
"""
from datetime import date, datetime, time
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.db import connection

from billing import services
from billing.testing import fake_invoicing
from core import access
from core.access.models import Membership, Role, RolePermission
from core.access.testing import make_agent, make_user, member
from invoicing import services as inv
from invoicing.conftest import ADDRESS, make_entity
from invoicing.models import InvoicingSettings, SalesDocument
from invoicing.testing import fake_zatca

D = Decimal
RIYADH = services.RIYADH

FAKE_CFO_APPROVAL = ("fake_cfo_approval: posting assertions on auto-issued invoices that hold only because "
                     "billing.testing.FakeInvoicing approves them as the CFO; the real hook must not (port contract "
                     "item 2), so with the real hooks they hold only once billing.md R53 is built. Deselect with "
                     "-m 'not fake_cfo_approval' when running against the real invoicing hooks.")


def pytest_configure(config):
    config.addinivalue_line("markers", FAKE_CFO_APPROVAL)


def fake_cfo_approval(test):
    """Mark ``test`` ``fake_cfo_approval``. Every clear() of an auto-issued invoice depends on the same fake approval;
    the tests marked are those whose assertions are about that posting (journal entry, approved_by / posted_by of
    the invoice). A function, so the mark is made when the test module is imported, after pytest_configure above
    registered it."""
    return pytest.mark.fake_cfo_approval(test)


@pytest.fixture
def clock(monkeypatch):
    """One clock for billing (today, the run date) and invoicing (the issue date of what it approves)."""
    state = SimpleNamespace(day=date(2027, 1, 1))
    monkeypatch.setattr(services, "_today", lambda: state.day)
    monkeypatch.setattr(inv, "_riyadh_now", lambda: datetime.combine(state.day, time(10, 0), tzinfo=RIYADH))

    def set_day(day):
        state.day = day

    state.set = set_day
    return state


@pytest.fixture
def w(db, clock):
    with fake_zatca() as zatca, fake_invoicing() as fake:
        alm = make_entity("ALM", "parent", vat="300000000000003", cr="1010000001", unified="7000000001")
        sub = make_entity("SUB", "full", alm, vat="311111111100003", cr="1010000002")
        users = {}
        for name, role, entity in (
            ("s", "accountant", alm), ("s2", "accountant", alm), ("c", "approver", alm), ("c_b", "approver", alm),
            ("c2", "approver", sub), ("sub_s", "accountant", sub), ("v", "viewer", alm), ("h", "hr_officer", alm),
            ("r", "receivables_clerk", alm),
        ):
            user = make_user(name)
            member(user, entity, role)
            users[name] = user
        billing_user, queue = access.system_user("billing"), access.system_user("zatca_queue")
        for e in (alm, sub):
            member(billing_user, e, "system_billing")
            member(queue, e, "system_zatca_queue")
        ns = SimpleNamespace(alm=alm, sub=sub, clock=clock, zatca=zatca, fake=fake, sys=billing_user, queue=queue,
                             **users)
        ns.customer = inv.create_customer(
            alm, user=ns.s, code="C001", name_en="Test Tenant LLC", name_ar="شركة المستأجر التجريبية",
            is_vat_registered=True, vat_number="310000000000003", **ADDRESS)
        ns.ic_customer = inv.create_customer(
            alm, user=ns.s, code="IC-SUB", name_en="SUB Co", name_ar="شركة SUB", is_vat_registered=True,
            vat_number="311111111100003", group_entity=sub, **ADDRESS)
        ns.sub_customer = inv.create_customer(
            sub, user=ns.sub_s, code="C900", name_ar="عميل SUB", is_vat_registered=True,
            vat_number="320000000000003", **ADDRESS)
        yield ns


@pytest.fixture
def agent(w):
    role = Role.objects.create(code="agent_contract_drafter", name_en="Agent: contract drafter",
                               name_ar="وكيل مسودات العقود", scope="entity", kind="agent")
    for codename in ("billing.edit_contract_draft", "billing.view"):
        RolePermission.objects.create(role=role, codename=codename)
    user = make_agent("contract_drafter")
    Membership.objects.create(user=user, entity=w.alm, role=role, granted_by=w.c, reason="pilot",
                              valid_from=access.services.now() - __import__("datetime").timedelta(hours=1))
    return user


# --- builders ------------------------------------------------------------------------------------------

LEASE = dict(contract_type="lease", currency="SAR", billing_frequency="monthly", billing_timing="advance",
             title_ar="عقد إيجار تجاري", title_en="Commercial lease", description_ar="مكتب في المبنى التجريبي",
             lease_use="commercial", property_ref="TEST-BLDG-1", unit_ref="U-101")


def contract(w, *, entity=None, customer=None, user=None, lines=(("rent_commercial", "10000.00"),),
             start=date(2027, 1, 1), end=date(2027, 12, 31), **fields):
    """A draft contract with ``lines`` of (income type, amount[, extra kwargs])."""
    entity = entity or w.alm
    user = user or (w.s if entity == w.alm else w.sub_s)
    values = {**LEASE, **fields}
    c = services.create_contract(entity, user=user, customer=customer or w.customer, start_date=start, end_date=end,
                                 **values)
    for spec in lines:
        income_type, amount, *extra = spec
        services.add_line(c, user=user, income_type=income_type, amount=amount, description_ar="إيجار شهري",
                          description_en="Monthly rent", **(extra[0] if extra else {}))
    return c


def submitted(c, user=None):
    """The draft submitted for approval by ``user`` (default: its creator), as R8 requires before approval."""
    return services.submit_contract(c, user=user or c.created_by)


def approved(w, approver=None, **kwargs):
    c = contract(w, **kwargs)
    approver = approver or (w.c if c.entity == w.alm else w.c2)
    return services.approve_contract(submitted(c), user=approver)


def run(w, day, entity=None):
    """Run billing on ``day`` as the scheduler would; the clock moves to that day first."""
    w.clock.set(day)
    runs = services.run_billing(day, entity=entity or w.alm, user=w.sys)
    return runs[0] if entity is not None or len(runs) == 1 else runs


def clear(w, doc):
    """ZATCA clears (or reports) an issued document; invoicing posts it (B1...)."""
    doc = SalesDocument.objects.get(pk=doc.pk)
    return w.zatca.clear(doc) if doc.zatca_profile == "standard" else w.zatca.report(doc)


def period(c, start):
    return c.periods.get(period_start=start)


def version(w, c, *, kind="amendment", effective, reason_ar="تعديل القيمة الإيجارية", amount=None, user=None,
            submit=True, **terms):
    user = user or w.s
    v = services.create_version(c, user=user, kind=kind, effective_date=effective, reason_ar=reason_ar,
                                reason_en="Contract change", **terms)
    if amount is not None:
        for line in v.lines.all():
            services.update_line(line, user=user, amount=amount)
    if submit:
        v = services.submit_version(v, user=user)
    return v


def approve_note(w, doc, *, reviewer=None, approver=None, day=None):
    """The reviewer completes and submits the draft note; the CFO approves it; ZATCA clears it."""
    reviewer, approver = reviewer or w.s, approver or w.c
    if day:
        w.clock.set(day)
    inv.update_sales_document(doc, user=reviewer, payment_means_code="30")
    inv.submit_sales_document(doc, user=reviewer)
    issued = inv.approve_sales_document(SalesDocument.objects.get(pk=doc.pk), user=approver)
    return clear(w, issued)


def set_auto_issue(entity, value):
    s, _ = InvoicingSettings.objects.get_or_create(entity=entity)
    InvoicingSettings.objects.filter(pk=s.pk).update(auto_issue_from_approved_contract=value)


postgres_only = pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL triggers and locks")
