"""Fixtures for payroll tests (payroll.md §8): anonymised entities ALM (parent, SA, SAR, Sharia enforced), SUB (SA),
EGY (EG, EGP); users per role; employees S1 (Saudi, legacy_m33) and N1 (non-Saudi) as E1. All names and IDs are made
up; amounts are Decimal strings; nothing external is called (the invoicing port is a fake).
"""
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.utils import timezone

from core import access
from core.access.testing import make_agent, make_user, member
from hr import services as hr
from hr.conftest import contract, employee, hired, make_entity  # noqa: F401  (re-exported for the tests)
from ledger.models import ExchangeRate
from ledger.services_setup import create_monthly_periods
from payroll import services
from payroll.models import PayrollRun
from payroll.testing import fake_invoicing

D = Decimal
E1_S1 = {"BASIC": "10000.00", "HOUSING": "2500.00", "TRANSPORT": "1000.00"}
E1_N1 = {"BASIC": "6000.00", "HOUSING": "1500.00", "TRANSPORT": "500.00"}


@pytest.fixture
def clock(monkeypatch):
    """The HR clock (2027-06-01, contracts) and the payroll clock (``pclock.set(datetime)``; default 2027-03-20)."""
    state = SimpleNamespace(day=date(2027, 6, 1))
    monkeypatch.setattr(hr, "_today", lambda: state.day)
    state.set = lambda d: setattr(state, "day", d)
    return state


@pytest.fixture
def pclock(monkeypatch):
    state = SimpleNamespace(at=timezone.make_aware(datetime(2027, 3, 20, 12, 0)))
    monkeypatch.setattr(services, "_now", lambda: state.at)
    monkeypatch.setattr(services, "_today", lambda: timezone.localtime(state.at).date())

    def set_at(value):
        state.at = timezone.make_aware(value) if isinstance(value, datetime) else timezone.make_aware(
            datetime(value.year, value.month, value.day, 12, 0))

    state.set = set_at
    return state


@pytest.fixture
def invoicing():
    with fake_invoicing() as port:
        yield port


@pytest.fixture
def w(db, clock, pclock, invoicing):
    if True:
        alm = make_entity("ALM", "parent")
        alm.sharia_enforced = True
        alm.save()
        sub = make_entity("SUB", "full", alm)
        egy = make_entity("EGY", "full", alm, country="EG", currency="EGP")
        for e in (alm, sub):
            create_monthly_periods(e, 2027)
        users = {}
        for name, role, entity in (
            ("h", "hr_officer", alm), ("h2", "hr_officer", alm), ("p", "payroll_officer", alm),
            ("p2", "payroll_officer", alm), ("c", "approver", alm), ("c2", "approver", sub), ("a", "accountant", alm),
            ("v", "viewer", alm), ("mp", "managing_partner", alm), ("hs", "hr_officer", sub),
            ("ps", "payroll_officer", sub), ("he", "hr_officer", egy), ("ce", "approver", egy),
            ("pe", "payroll_officer", egy),
        ):
            user = make_user(name)
            member(user, entity, role)
            users[name] = user
        ns = SimpleNamespace(alm=alm, sub=sub, egy=egy, clock=clock, pclock=pclock, invoicing=invoicing, **users)
        ns.agent = make_agent("payroll_reader")
        ns.fin = hr.create_department(alm, user=ns.h, code="FIN", name_en="Finance", name_ar="المالية")
        ns.staff = hr.create_grade(alm, user=ns.h, code="G-STAFF", name_en="Staff", name_ar="موظف", overtime_eligible=True)
        ns.mgmt = hr.create_grade(alm, user=ns.h, code="G-MGMT", name_en="Management", name_ar="الإدارة",
                                  overtime_eligible=False)
        ns.sub_dept = hr.create_department(sub, user=ns.hs, code="OPS", name_en="Operations", name_ar="العمليات")
        ns.sub_grade = hr.create_grade(sub, user=ns.hs, code="G-STAFF", name_en="Staff", name_ar="موظف",
                                       overtime_eligible=True)
        for e, approver in ((alm, ns.c), (sub, ns.c2), (egy, ns.ce)):
            services.configure_settings(e, user=approver, enabled=True)
        yield ns


def saudi(w, entity=None, components=None, cohort="legacy_m33", hire_date=date(2027, 1, 1), contract_kwargs=None,
          **fields):
    values = dict(gosi_cohort=cohort, gosi_first_coverage_date=date(2015, 1, 1), gosi_number="G-1",
                  iban="SA0380000000608010167519", bank_name="Al Rajhi")
    values.update(fields)
    return hired(w, entity, components or dict(E1_S1), contract_kwargs, hire_date=hire_date, **values)


def expat(w, entity=None, components=None, hire_date=date(2027, 1, 1), contract_kwargs=None, **fields):
    values = dict(saudi=False, gosi_number="G-2")
    values.update(fields)
    return hired(w, entity, components or dict(E1_N1), contract_kwargs, hire_date=hire_date, **values)


def run_for(w, period="2027-03", entity=None, user=None, **kw):
    return services.create_run(entity or w.alm, user=user or w.p, period=period, **kw)


def calculated(w, period="2027-03", entity=None, user=None, **kw):
    return services.calculate_run(run_for(w, period, entity, user, **kw), user=user or w.p)


def submitted(w, period="2027-03", entity=None, user=None, **kw):
    return services.submit_run(calculated(w, period, entity, user, **kw), user=user or w.p)


def approved(w, period="2027-03", entity=None, approver=None, **kw):
    run = submitted(w, period, entity, **kw)
    return services.approve_run(run, user=approver or w.h)


def slip(run, emp):
    return run.payslips.get(employee=emp)


def lines(run, emp, line_type=None):
    qs = slip(run, emp).lines.all()
    return qs.filter(line_type=line_type) if line_type else qs


def amount(run, emp, line_type, **filters):
    return sum((l.amount for l in lines(run, emp, line_type).filter(**filters)), D("0"))


def entry_lines(run):
    out = {}
    for l in run.journal_entry.lines.select_related("account"):
        out[l.account.code] = out.get(l.account.code, D("0")) + l.debit - l.credit
    return out


def rate(f, t, d, r):
    return ExchangeRate.objects.create(entity=None, from_currency_id=f, to_currency_id=t, date=d, rate=D(r),
                                       source="manual", rate_type="spot", created_by=make_user())


def asset_bill(w, cost, on=date(2027, 2, 15)):
    """A posted vendor bill of ALM debiting 1250 (bai' ajel asset bought in the company's name, E9): test data."""
    from invoicing import services as inv
    from ledger.models import Account

    if not hasattr(w, "ap"):
        Account.objects.get_or_create(entity=w.alm, code="1250", defaults=dict(
            name_en="Assets acquired for employee deferred sale", name_ar="أصول مشتراة للبيع الآجل للموظفين",
            type="asset", parent=Account.objects.get(entity=w.alm, code="1000"), is_postable=True))
        w.ap = make_user("ap")
        member(w.ap, w.alm, "payables_clerk")
        w.vendor = inv.create_vendor(w.alm, user=w.ap, vendor_kind="business", code="V-BAJ", name_en="Test Car Dealer",
                                     name_ar="معرض سيارات تجريبي", is_vat_registered=True, vat_number="320000000000003")
        w.bills = 0
    w.bills += 1
    doc = inv.create_purchase_document(w.alm, user=w.ap, vendor=w.vendor, vendor_document_number=f"CAR-{w.bills}",
                                       vendor_document_date=on, accounting_date=on)
    inv.add_purchase_line(doc, user=w.ap, account="1250", quantity="1", unit_price=cost, description_ar="أصل للبيع الآجل")
    doc.refresh_from_db()
    _, net, charged, _, _, _, _ = inv._purchase_amounts(doc, list(doc.lines.select_related("vat_rate")))
    inv.update_purchase_document(doc, user=w.ap, vendor_total_vat=charged, vendor_total_gross=net + charged)
    return inv.approve_purchase_document(inv.submit_purchase_document(doc, user=w.ap), user=w.c)


def approve_input(w, inp, user=None):
    return services.approve_input(inp, user=user or w.p2)


def grant(user, entity, perm, document=None):
    return access.require(user, entity, perm, document=document)


__all__ = ["D", "PayrollRun"]
